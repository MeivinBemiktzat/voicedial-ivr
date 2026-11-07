"""
api/index.py
============
מודול ה-API הראשי של מערכת החיוג הקולי (IVR) לימות המשיח.

מתפקד כ-endpoint יחיד (type=api) שימות המשיח פונה אליו לאורך כל השיחה.
המערכת היא stateless מצד הקוד שלנו - כל המידע הדרוש לכל שלב מגיע עם
כל בקשה (ApiPhone, ApiCallId, ופרמטרים שנאספו בשלבים קודמים דרך read),
ומצב המשתמש (אנשי קשר, היסטוריה) נשמר ב-JSON ב-Hugging Face Storage
(דרך storage.py).

חשוב - תמלול: שום שאלה כאן אינה מסוג 'voice' (שהיה גורם לימות לתמלל
בעצמה). כל קלט קולי נאסף כ-'record' (הקלטה גולמית בלבד), ואז אנחנו
מורידים את הקובץ ומתמללים אותו בעצמנו עם transcription.py (הקוד שסופק
על ידי המשתמש), כפי שנדרש במפורש.

זרימת השיחה (ר' README.md להרחבה):
1. חיוג ישיר (ברירת מחדל) - הקלטת כוונה חופשית -> ניתוח -> חיוג/תפריט
2. "שיחות אחרונות" - תפריט ממוספר מתוך ה-history
3. "אנשי קשר" - תפריט ממוספר מתוך ה-contacts
4. "הוסף איש קשר" - דו-שיח דו-שלבי (שם ואז טלפון) דרך read
"""

"""
api/index.py
============
מודול ה-API של מערכת החיוג הקולי לימות המשיח.
"""

import os
import re
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# הוספת תיקיית השורש ותיקיית api לנתיבי החיפוש
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, '..'))
for path in (current_dir, parent_dir):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    import storage
    import transcription
except ImportError:
    from api import storage, transcription

YEMOT_TOKEN_PARAM = 'ApiYemotToken'

# מילות מפתח לזיהוי
KEYWORDS_RECENT_CALLS = ('שיחות אחרונות', 'שיחה אחרונה', 'אחרונות')
KEYWORDS_CONTACTS = ('אנשי קשר', 'איש קשר', 'אנשי הקשר')
KEYWORDS_ADD_CONTACT = ('הוסף איש קשר', 'הוספת איש קשר', 'הוסף')
KEYWORDS_VERIFICATION_CODE = ('קוד אימות', 'קוד האימות', 'אימות')

PHONE_DIGITS_RE = re.compile(r'\d+')

HEBREW_DIGIT_WORDS = {
    'אפס': '0', 'אחת': '1', 'אחד': '1', 'שתיים': '2', 'שתים': '2',
    'שניים': '2', 'שנים': '2', 'שלוש': '3', 'שלש': '3', 'ארבע': '4',
    'חמש': '5', 'שש': '6', 'שבע': '7', 'שמונה': '8', 'תשע': '9',
}


def _get_clean_extension(params: dict) -> str:
    """שולף את נתיב השלוחה מתוך הבקשה של ימות (למשל 'voicedial') ומנרמל אותו."""
    ext = params.get('ApiExtension', ['voicedial'])[0]
    ext = ext.strip().strip('/')
    return ext or 'voicedial'


def _transcribe_step_recording(token: str, extension: str, recorded_filename: str) -> str:
    """מוריד ומתמלל קובץ הקלטה לפי הנתיב הדינמי שלו בימות המשיח."""
    if not token:
        raise RuntimeError(f'חסר טוקן ימות בבקשה ({YEMOT_TOKEN_PARAM})')

    # אם ימות החזירה כבר שם עם .wav או בלי
    clean_name = recorded_filename.replace('.wav', '')
    ivr_path = f'/{extension}/{clean_name}.wav'
    return transcription.transcribe_yemot_recording(token, ivr_path)


def _words_to_digits(text: str) -> str:
    words = re.findall(r'[֐-׿a-zA-Z0-9]+', text or '')
    converted = [HEBREW_DIGIT_WORDS.get(w, w) for w in words]
    return ' '.join(converted)


def _extract_phone_digits(text: str) -> str:
    with_digit_words = _words_to_digits(text)
    return ''.join(PHONE_DIGITS_RE.findall(with_digit_words))


def _looks_like_phone(text: str) -> bool:
    digits = _extract_phone_digits(text)
    if len(digits) < 7:
        return False
    converted = _words_to_digits(text)
    non_digits = len(re.sub(r'[\d\s\-]', '', converted))
    return non_digits <= 1


def _contains_keyword(text: str, keywords) -> bool:
    normalized = (text or '').strip()
    return any(kw in normalized for kw in keywords)


def _resp_routing(phone_digits: str) -> str:
    return f'routing={phone_digits}'


def _resp_message_and_hangup(text: str) -> str:
    return f'id_list_message=t-{text}'


def _resp_read_record(prompt_text: str, param_name: str, extension: str,
                       max_sec: int = 10) -> str:
    """
    יוצר פקודת read להקלטה:
    - תיקייה: השלוחה הנוכחית (למשל /voicedial)
    - שם קובץ: ריק (ימות מייצרת אוטומטית 000, 001...)
    - סיום בסולמית: no (מסתיים אוטומטית בשקט או בסולמית)
    - שמירה בניתוק: yes
    - מינימום שניות: 0 (מונע 'הודעה קצרה מדי')
    """
    folder = f'/{extension}'
    second_part = f'{param_name},,record,{folder},,no,yes,0,{max_sec}'
    return f'read=t-{prompt_text}={second_part}'


def _handle_recent_calls(params: dict, user_data: dict, token: str, extension: str) -> str:
    history = user_data.get('history', [])
    choice_val = params.get('RecentCallChoice', [None])[0]

    if choice_val is not None:
        choice_text = _transcribe_step_recording(token, extension, choice_val)
        digits = _extract_phone_digits(choice_text)
        if not digits:
            return _resp_message_and_hangup('לא זיהיתי את הבחירה.')

        index = int(digits) - 1
        if index < 0 or index >= len(history):
            return _resp_message_and_hangup('בחירה לא קיימת.')

        target = history[index]
        user_data = storage.add_history_entry(user_data, target['target'], target.get('name'))
        storage.save_user(user_data)
        return _resp_routing(target['target'])

    if not history:
        return _resp_message_and_hangup('אין שיחות אחרונות.')

    lines = [f'לשיחה ל{entry.get("name") or entry["target"]}, אמרו {i}.' for i, entry in enumerate(history, 1)]
    prompt = ' '.join(lines) + ' אמרו את המספר.'
    return _resp_read_record(prompt, 'RecentCallChoice', extension, max_sec=5)


def _handle_contacts_menu(params: dict, user_data: dict, token: str, extension: str) -> str:
    contacts = user_data.get('contacts', [])
    choice_val = params.get('ContactChoice', [None])[0]

    if choice_val is not None:
        spoken = _transcribe_step_recording(token, extension, choice_val)
        contact = storage.find_contact(user_data, spoken)
        if not contact:
            return _resp_message_and_hangup(f'לא מצאתי איש קשר בשם {spoken}.')

        user_data = storage.add_history_entry(user_data, contact['phone'], contact['name'])
        storage.save_user(user_data)
        return _resp_routing(contact['phone'])

    if not contacts:
        return _resp_message_and_hangup('אין אנשי קשר שמורים.')

    names = ', '.join(c['name'] for c in contacts)
    prompt = f'אנשי הקשר הם: {names}. למי תרצו להתקשר?'
    return _resp_read_record(prompt, 'ContactChoice', extension, max_sec=8)


def _handle_add_contact(params: dict, user_data: dict, token: str, extension: str) -> str:
    name_val = params.get('NewContactName', [None])[0]
    phone_val = params.get('NewContactPhone', [None])[0]

    if name_val is None:
        return _resp_read_record('אנא אמרו את שם איש הקשר להוספה.', 'NewContactName', extension, max_sec=6)

    if phone_val is None:
        return _resp_read_record('אנא אמרו או הקישו את מספר הטלפון.', 'NewContactPhone', extension, max_sec=10)

    name_text = _transcribe_step_recording(token, extension, name_val)
    phone_text = _transcribe_step_recording(token, extension, phone_val)
    phone_digits = _extract_phone_digits(phone_text)

    if not name_text.strip() or not phone_digits:
        return _resp_message_and_hangup('לא הצלחתי לזהות את הפרטים. נסו שוב.')

    user_data = storage.add_contact(user_data, name_text.strip(), phone_digits)
    storage.save_user(user_data)
    return _resp_message_and_hangup(f'{name_text} נוסף בהצלחה.')


def _handle_verification_code(caller_phone: str) -> str:
    record = storage.get_verification_code(caller_phone)
    if not record:
        return _resp_message_and_hangup(
            'לא נמצא קוד אימות ממתין עבור מספר זה. '
            'הזינו קודם את המספר באתר ולחצו שלח קוד, ואז התקשרו שוב.'
        )

    code = record['code']
    spoken_code = ' '.join(code)
    return _resp_message_and_hangup(f'קוד האימות שלכם הוא: {spoken_code}. שוב: {spoken_code}.')


def _handle_main_intent(params: dict, user_data: dict, token: str, extension: str, recorded_filename: str) -> str:
    utterance = _transcribe_step_recording(token, extension, recorded_filename)

    if not utterance.strip():
        return _resp_message_and_hangup('לא שמעתי אתכם בבירור. אנא נסו שוב.')

    if _contains_keyword(utterance, KEYWORDS_VERIFICATION_CODE):
        caller_phone = params.get('ApiPhone', [None])[0]
        return _handle_verification_code(caller_phone)

    if _contains_keyword(utterance, KEYWORDS_ADD_CONTACT):
        return _handle_add_contact({}, user_data, token, extension)

    if _contains_keyword(utterance, KEYWORDS_RECENT_CALLS):
        return _handle_recent_calls({}, user_data, token, extension)

    if _contains_keyword(utterance, KEYWORDS_CONTACTS):
        return _handle_contacts_menu({}, user_data, token, extension)

    if _looks_like_phone(utterance):
        digits = _extract_phone_digits(utterance)
        user_data = storage.add_history_entry(user_data, digits, None)
        storage.save_user(user_data)
        return _resp_routing(digits)

    contact = storage.find_contact(user_data, utterance)
    if contact:
        user_data = storage.add_history_entry(user_data, contact['phone'], contact['name'])
        storage.save_user(user_data)
        return _resp_routing(contact['phone'])

    return _resp_message_and_hangup(f'לא הבנתי את הבקשה "{utterance}". נסו שוב.')


def handle_request(params: dict) -> str:
    phone = params.get('ApiPhone', [None])[0]
    if not phone:
        return _resp_message_and_hangup('שגיאה: לא התקבל מספר מתקשר.')

    token = params.get(YEMOT_TOKEN_PARAM, [None])[0]
    if not token:
        return _resp_message_and_hangup(f'שגיאה: חסר {YEMOT_TOKEN_PARAM} בהגדרות השלוחה.')

    user_data = storage.load_user(phone)
    extension = _get_clean_extension(params)

    # שלבי הוספת איש קשר
    if params.get('NewContactName', [None])[0] is not None or \
       params.get('NewContactPhone', [None])[0] is not None:
        return _handle_add_contact(params, user_data, token, extension)

    # שיחות אחרונות
    if params.get('RecentCallChoice', [None])[0] is not None:
        return _handle_recent_calls(params, user_data, token, extension)

    # אנשי קשר
    if params.get('ContactChoice', [None])[0] is not None:
        return _handle_contacts_menu(params, user_data, token, extension)

    # האם המשתמש כבר הקליט את הבקשה הראשית?
    main_spoken = params.get('MainSpoken', [None])[0]
    if main_spoken is not None:
        return _handle_main_intent(params, user_data, token, extension, main_spoken)

    # כניסה ראשונית לשלוחה: משמיע ביפ ומקליט לקובץ ממוספר
    return _resp_read_record(
        prompt_text='אנא אמרו את הבקשה לאחר הצליל.',
        param_name='MainSpoken',
        extension=extension,
        max_sec=8
    )


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        try:
            params = self._collect_params()
            response_text = handle_request(params)
        except Exception as exc:
            response_text = f'id_list_message=t-אירעה שגיאה זמנית: {exc}'

        body = response_text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _collect_params(self) -> dict:
        parsed_url = urlparse(self.path)
        params = parse_qs(parsed_url.query)

        content_length = int(self.headers.get('Content-Length', 0) or 0)
        if content_length:
            body_bytes = self.rfile.read(content_length)
            body_params = parse_qs(body_bytes.decode('utf-8'))
            for key, values in body_params.items():
                params.setdefault(key, []).extend(values)

        return params

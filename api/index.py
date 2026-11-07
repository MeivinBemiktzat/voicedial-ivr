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

import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import storage
import transcription

# --- טוקן ה-Management API של ימות (עבור הורדת הקלטות) ---
# בכוונה **אין** כאן משתני סביבה עם מספר מערכת/סיסמה. הטוקן מגיע בכל
# בקשה כפרמטר ApiYemotToken, שמוגדר פעם אחת בהגדרת השלוחה (ext.ini) -
# ר' ext.ini.example. מומלץ להשתמש בטוקן קבוע שהונפק דרך אתר הניהול של
# ימות המשיח (אינו פג תוקף), ולא בפורמט הישן "מספר_מערכת:סיסמה".
YEMOT_TOKEN_PARAM = 'ApiYemotToken'

# תיקיית ה-IVR שבה המערכת שומרת/מחפשת את קובצי ההקלטה הזמניים.
# צריכה להתאים להגדרת api_dir / לנתיב שמוגדר בכל שאלת record ב-ext.ini
RECORDING_DIR = os.environ.get('YEMOT_RECORDING_DIR', '/5')

# מילות מפתח לזיהוי כוונה (ניתן להרחיב/להתאים לפי הצורך)
KEYWORDS_RECENT_CALLS = ('שיחות אחרונות', 'שיחה אחרונה', 'אחרונות')
KEYWORDS_CONTACTS = ('אנשי קשר', 'איש קשר', 'אנשי הקשר')
KEYWORDS_ADD_CONTACT = ('הוסף איש קשר', 'הוספת איש קשר', 'הוסף')

PHONE_DIGITS_RE = re.compile(r'\d+')

# מילון בסיסי להמרת ספרות שנאמרות במילים בעברית לספרות - כדי שתמלול כמו
# "אפס חמש שתיים אחת אחת אחת אחת אחת אחת" יזוהה כמספר טלפון (ולא רק הקשה
# ישירה של ספרות). ניתן להרחיב בהתאם לצורך.
HEBREW_DIGIT_WORDS = {
    'אפס': '0',
    'אחת': '1', 'אחד': '1',
    'שתיים': '2', 'שתים': '2', 'שניים': '2', 'שנים': '2',
    'שלוש': '3', 'שלש': '3',
    'ארבע': '4',
    'חמש': '5',
    'שש': '6',
    'שבע': '7',
    'שמונה': '8',
    'תשע': '9',
}


# --------------------------------------------------------------------------
# עזרי תמלול / הורדה
# --------------------------------------------------------------------------

def _transcribe_step_recording(token: str, filename: str) -> str:
    """מוריד ומתמלל קובץ הקלטה ששמור בתיקיית ה-IVR שלנו.

    token - טוקן ה-API של ימות, שמגיע עם כל בקשה מהגדרת השלוחה (ולא
            ממשתני סביבה ב-Vercel) - ר' YEMOT_TOKEN_PARAM לעיל.
    """
    if not token:
        raise RuntimeError(
            f'חסר טוקן ימות בבקשה - יש להגדיר {YEMOT_TOKEN_PARAM} בהגדרת השלוחה (ext.ini)'
        )

    ivr_path = f'{RECORDING_DIR}/{filename}.wav'
    return transcription.transcribe_yemot_recording(token, ivr_path)


# --------------------------------------------------------------------------
# עזרי ניתוח כוונה
# --------------------------------------------------------------------------

def _words_to_digits(text: str) -> str:
    """ממיר מילות ספרות בעברית ("אפס", "חמש" וכו') לספרות, מילה-במילה.
    מילים שאינן ספרות מוחלפות ברווח (ולא נמחקות) כדי לא לדבק שתי ספרות
    שלא נאמרו ברצף ישיר."""
    words = re.findall(r'[֐-׿a-zA-Z0-9]+', text or '')
    converted = []
    for word in words:
        if word in HEBREW_DIGIT_WORDS:
            converted.append(HEBREW_DIGIT_WORDS[word])
        else:
            converted.append(word)
    return ' '.join(converted)


def _extract_phone_digits(text: str) -> str:
    """מחלץ רצף ספרות ממחרוזת (לזיהוי מספר טלפון שנאמר/הוכתב) - תומך הן
    בספרות שהוקשו/נכתבו והן בספרות שנאמרו במילים בעברית."""
    with_digit_words_converted = _words_to_digits(text)
    digits = ''.join(PHONE_DIGITS_RE.findall(with_digit_words_converted))
    return digits


def _looks_like_phone(text: str) -> bool:
    digits = _extract_phone_digits(text)
    # מספר טלפון ישראלי סביר - בין 7 ל-10 ספרות
    if len(digits) < 7:
        return False

    # ברוב התוכן צריכות להיות ספרות (לאחר המרת מילות-ספרות) - כדי לא
    # להתבלבל עם משפט רגיל שבו מופיע מספר קטן באמצע (כמו "שיחה מספר 2")
    converted = _words_to_digits(text)
    non_digit_chars = len(re.sub(r'[\d\s\-]', '', converted))
    return non_digit_chars <= 1


def _contains_keyword(text: str, keywords) -> bool:
    normalized = (text or '').strip()
    return any(kw in normalized for kw in keywords)


# --------------------------------------------------------------------------
# בוני תשובה (Yemot response actions) - טקסט פשוט, לפי הפרוטוקול של ימות
# --------------------------------------------------------------------------

def _resp_routing(phone_digits: str) -> str:
    return f'routing={phone_digits}'


def _resp_message_and_hangup(text: str) -> str:
    # id_list_message עם t- מקריא טקסט (TTS) ואז מנתק/חוזר לפי api_end_goto
    return f'id_list_message=t-{text}'


def _resp_message_and_goto(text: str, folder: str) -> str:
    return f'id_list_message=t-{text}&go_to_folder={folder}'


def _resp_read_record(prompt_text: str, param_name: str, folder: str,
                       filename: str, min_sec: int = 1, max_sec: int = 20) -> str:
    """בונה פעולת read מסוג הקלטה: משמיע prompt_text, מקליט לפרמטר param_name,
    שומר בנתיב folder/filename.wav.

    חלק שני (read) עבור הקלטה, לפי הסדר התיעודי:
    param_name,<use_existing>,record,<folder>,<filename>,<end_on_hash>,<save_on_hangup>,<min_sec>,<max_sec>
    """
    second_part = (
        f'{param_name},,record,{folder},{filename},no,yes,{min_sec},{max_sec}'
    )
    return f'read=t-{prompt_text}={second_part}'


# --------------------------------------------------------------------------
# זרימות משנה
# --------------------------------------------------------------------------

def _handle_recent_calls(params: dict, user_data: dict, token: str) -> str:
    history = user_data.get('history', [])

    # האם כבר נבחרה אפשרות מתוך תפריט השיחות האחרונות? (שלב שני בדו-שיח)
    choice_text = params.get('RecentCallChoice', [None])[0]
    if choice_text is not None:
        choice_text = _transcribe_step_recording(token, 'recent_choice')
        digits = _extract_phone_digits(choice_text)
        if not digits:
            return _resp_message_and_hangup('לא זיהיתי את הבחירה. אנא התקשרו שוב.')

        index = int(digits) - 1
        if index < 0 or index >= len(history):
            return _resp_message_and_hangup('בחירה לא קיימת. אנא התקשרו שוב.')

        target = history[index]
        user_data = storage.add_history_entry(
            user_data, target['target'], target.get('name')
        )
        storage.save_user(user_data)
        return _resp_routing(target['target'])

    if not history:
        return _resp_message_and_hangup('אין עדיין שיחות אחרונות.')

    # בניית הודעת התפריט: "לשיחה ל-X אמרו 1, לשיחה ל-Y אמרו 2..."
    lines = []
    for i, entry in enumerate(history, start=1):
        label = entry.get('name') or entry['target']
        lines.append(f'לשיחה ל{label}, אמרו {i}.')
    prompt = ' '.join(lines) + ' אנא אמרו את מספר הבחירה.'

    return _resp_read_record(
        prompt_text=prompt,
        param_name='RecentCallChoice',
        folder=RECORDING_DIR,
        filename='recent_choice',
        min_sec=1,
        max_sec=10,
    )


def _handle_contacts_menu(params: dict, user_data: dict, token: str) -> str:
    contacts = user_data.get('contacts', [])

    choice_text = params.get('ContactChoice', [None])[0]
    if choice_text is not None:
        spoken = _transcribe_step_recording(token, 'contact_choice')
        contact = storage.find_contact(user_data, spoken)
        if not contact:
            return _resp_message_and_hangup(f'לא מצאתי איש קשר בשם {spoken}.')

        user_data = storage.add_history_entry(
            user_data, contact['phone'], contact['name']
        )
        storage.save_user(user_data)
        return _resp_routing(contact['phone'])

    if not contacts:
        return _resp_message_and_hangup('אין עדיין אנשי קשר שמורים.')

    names = ', '.join(c['name'] for c in contacts)
    prompt = f'אנשי הקשר השמורים הם: {names}. אנא אמרו את שם איש הקשר אליו תרצו להתקשר.'

    return _resp_read_record(
        prompt_text=prompt,
        param_name='ContactChoice',
        folder=RECORDING_DIR,
        filename='contact_choice',
        min_sec=1,
        max_sec=10,
    )


def _handle_add_contact(params: dict, user_data: dict, token: str) -> str:
    has_name_step = params.get('NewContactName', [None])[0] is not None
    has_phone_step = params.get('NewContactPhone', [None])[0] is not None

    if not has_name_step:
        # שלב א: בקשת שם איש הקשר
        return _resp_read_record(
            prompt_text='אנא אמרו את שם איש הקשר להוספה.',
            param_name='NewContactName',
            folder=RECORDING_DIR,
            filename='new_contact_name',
            min_sec=1,
            max_sec=10,
        )

    if not has_phone_step:
        # שלב ב: בקשת מספר הטלפון
        return _resp_read_record(
            prompt_text='אנא אמרו או הקישו את מספר הטלפון של איש הקשר.',
            param_name='NewContactPhone',
            folder=RECORDING_DIR,
            filename='new_contact_phone',
            min_sec=1,
            max_sec=15,
        )

    # שני השלבים הושלמו - תמלול ושמירה
    name_text = _transcribe_step_recording(token, 'new_contact_name')
    phone_text = _transcribe_step_recording(token, 'new_contact_phone')
    phone_digits = _extract_phone_digits(phone_text)

    if not name_text.strip() or not phone_digits:
        return _resp_message_and_hangup(
            'לא הצלחתי לזהות את הפרטים. אנא נסו שוב להוסיף איש קשר.'
        )

    user_data = storage.add_contact(user_data, name_text.strip(), phone_digits)
    storage.save_user(user_data)

    return _resp_message_and_hangup(
        f'{name_text} נוסף בהצלחה לאנשי הקשר שלכם.'
    )


def _handle_main_intent(params: dict, user_data: dict, token: str) -> str:
    """השלב הראשון בשיחה: תמלול הכוונה החופשית וניתוב בהתאם."""
    utterance = _transcribe_step_recording(token, 'recording')

    if not utterance.strip():
        return _resp_message_and_hangup('לא הצלחתי לשמוע אתכם בבירור. אנא התקשרו שוב.')

    if _contains_keyword(utterance, KEYWORDS_ADD_CONTACT):
        return _handle_add_contact({}, user_data, token)

    if _contains_keyword(utterance, KEYWORDS_RECENT_CALLS):
        return _handle_recent_calls({}, user_data, token)

    if _contains_keyword(utterance, KEYWORDS_CONTACTS):
        return _handle_contacts_menu({}, user_data, token)

    if _looks_like_phone(utterance):
        digits = _extract_phone_digits(utterance)
        user_data = storage.add_history_entry(user_data, digits, None)
        storage.save_user(user_data)
        return _resp_routing(digits)

    # ניסיון להתאים לשם איש קשר
    contact = storage.find_contact(user_data, utterance)
    if contact:
        user_data = storage.add_history_entry(
            user_data, contact['phone'], contact['name']
        )
        storage.save_user(user_data)
        return _resp_routing(contact['phone'])

    return _resp_message_and_hangup(
        f'לא הבנתי את הבקשה "{utterance}". אנא נסו שוב.'
    )


# --------------------------------------------------------------------------
# ניתוב הבקשה הנכנסת לפי מה שכבר נאסף (state-from-params)
# --------------------------------------------------------------------------

def handle_request(params: dict) -> str:
    """params: dict שמגיע מ-parse_qs (כל ערך הוא list של strings).
    מחזיר מחרוזת טקסט פשוט - תשובת ה-API לימות המשיח."""

    phone = params.get('ApiPhone', [None])[0]
    if not phone:
        return _resp_message_and_hangup('שגיאה: לא התקבל מספר מתקשר.')

    # טוקן ה-API של ימות מגיע עם כל בקשה (מוגדר פעם אחת בהגדרת השלוחה,
    # ext.ini) - ולא נקרא יותר ממשתני סביבה ב-Vercel. ר' YEMOT_TOKEN_PARAM.
    token = params.get(YEMOT_TOKEN_PARAM, [None])[0]
    if not token:
        return _resp_message_and_hangup(
            f'שגיאת הגדרה: חסר הפרמטר {YEMOT_TOKEN_PARAM} בהגדרת השלוחה.'
        )

    user_data = storage.load_user(phone)

    # דו-שיח הוספת איש קשר פעיל?
    if params.get('NewContactName', [None])[0] is not None or \
       params.get('NewContactPhone', [None])[0] is not None:
        return _handle_add_contact(params, user_data, token)

    # דו-שיח תפריט שיחות אחרונות פעיל?
    if params.get('RecentCallChoice', [None])[0] is not None:
        return _handle_recent_calls(params, user_data, token)

    # דו-שיח תפריט אנשי קשר פעיל?
    if params.get('ContactChoice', [None])[0] is not None:
        return _handle_contacts_menu(params, user_data, token)

    # ברירת מחדל: זוהי הבקשה הראשונה בשיחה (אחרי הקלטת הכוונה החופשית)
    return _handle_main_intent(params, user_data, token)


# --------------------------------------------------------------------------
# Vercel Python serverless handler
# --------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        try:
            params = self._collect_params()
            response_text = handle_request(params)
        except Exception as exc:  # noqa: BLE001 - חובה להחזיר תמיד טקסט תקין לימות
            response_text = f'id_list_message=t-אירעה שגיאה זמנית: {exc}'

        body = response_text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _collect_params(self) -> dict:
        """אוסף פרמטרים הן מה-query string (GET) והן מה-body (POST),
        כך שהמודול יעבוד בין אם api_url_post=yes מוגדר ובין אם לא."""
        parsed_url = urlparse(self.path)
        params = parse_qs(parsed_url.query)

        content_length = int(self.headers.get('Content-Length', 0) or 0)
        if content_length:
            body_bytes = self.rfile.read(content_length)
            body_params = parse_qs(body_bytes.decode('utf-8'))
            for key, values in body_params.items():
                params.setdefault(key, []).extend(values)

        return params

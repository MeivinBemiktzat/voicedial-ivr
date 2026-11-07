"""
api/web.py
==========
ה-API שמאחורי אתר יבוא אנשי הקשר (https://voicedial-ivr.vercel.app/).

כל הנתיבים כאן הם תת-נתיבים של /api/web (ר' vercel.json), ומזוהים לפי
פרמטר 'action' (ב-query string או בגוף הבקשה):

  action=request_code   (POST) - { phone } -> יוצר קוד אימות חדש
                                   עבור המספר (לא מחזיר את הקוד עצמו!
                                   הוא נמסר רק דרך שיחה לשלוחת ה-API,
                                   ר' index.py / auth.py).
  action=verify_code    (POST) - { phone, code } -> אם תואם ולא פג
                                   תוקף: מוחק את הקוד ומחזיר
                                   { session } (טוקן חתום לשמירה
                                   כעוגיית דפדפן).
  action=check_session   (GET) - בודק עוגיית session קיימת, מחזיר
                                   { authenticated, phone }.
  action=logout          (GET) - מוחק את עוגיית ה-session.
  action=upload_contacts (POST) - multipart/form-data עם קובץ
                                   אנשי-קשר (VCF/CSV/TXT) + עוגיית
                                   session תקפה -> מפרסר ומוסיף/מעדכן
                                   את אנשי הקשר של המשתמש המאומת.
                                   מחזיר { added, updated, total }.

אבטחה - עקרונות מפתח:
- מספר הטלפון ש"מאושר" לפעולות upload_contacts/ נגזר **אך ורק** מתוך
  session token חתום (auth.verify_session), ולעולם לא ממה שהלקוח שולח
  בטופס/פרמטר 'phone' בבקשות מאומתות. כך גם אם מישהו ישלח בקשה
  עם phone מזויף, הוא לא יוכל לשנות אנשי קשר של מספר שאינו שלו.
- request_code/verify_code *כן* מקבלים phone מהלקוח - אבל אלו בדיוק
  השלבים שלפני האימות, ואינם חושפים/משנים נתוני אנשי קשר. הקוד עצמו
  לעולם לא מוחזר ב-HTTP response - הוא *רק* מושמע בשיחה למספר שממנו
  מתקשרים בפועל (ApiPhone, לא ניתן לזיוף).
- עוגיית ה-session מסומנת HttpOnly+Secure+SameSite=Lax כדי לצמצם סיכוני
  גניבת עוגייה (XSS/CSRF).
"""

import json
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import auth
import contacts_import
import storage

SESSION_COOKIE_NAME = 'voicedial_session'

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB - מגבלה סבירה לקובץ אנשי קשר


def _normalize_phone_digits(phone: str) -> str:
    return ''.join(ch for ch in (phone or '') if ch.isdigit())


def _json_response(status: int, payload: dict, set_cookie: str = None, clear_cookie: bool = False):
    return status, payload, set_cookie, clear_cookie


def _get_session_phone(headers) -> str:
    """שולף ומאמת את עוגיית ה-session מתוך בקשת HTTP, ומחזיר את מספר
    הטלפון המאומת (או מחרוזת ריקה אם אין session תקף)."""
    cookie_header = headers.get('Cookie', '')
    if not cookie_header:
        return ''

    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:  # noqa: BLE001 - עוגייה לא תקינה
        return ''

    morsel = cookie.get(SESSION_COOKIE_NAME)
    if not morsel:
        return ''

    phone = auth.verify_session(morsel.value)
    return phone or ''


def _build_set_cookie(session_token: str) -> str:
    return (
        f'{SESSION_COOKIE_NAME}={session_token}; '
        f'Max-Age={auth.SESSION_TTL_SECONDS}; Path=/; HttpOnly; Secure; SameSite=Lax'
    )


def _build_clear_cookie() -> str:
    return f'{SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax'


# --------------------------------------------------------------------------
# פעולות
# --------------------------------------------------------------------------

def _action_request_code(params: dict) -> tuple:
    phone = _normalize_phone_digits(params.get('phone', [''])[0])
    if not phone or len(phone) < 7:
        return _json_response(400, {'error': 'מספר טלפון לא תקין.'})

    code = auth.generate_code()
    storage.create_verification_code(phone, code)

    return _json_response(200, {
        'ok': True,
        'message': 'קוד אימות נוצר. יש להתקשר כעת לשלוחת המערכת מהמספר שהוזן, ולומר "קוד אימות".',
    })


def _action_verify_code(params: dict) -> tuple:
    phone = _normalize_phone_digits(params.get('phone', [''])[0])
    submitted_code = (params.get('code', [''])[0] or '').strip()

    if not phone or not submitted_code:
        return _json_response(400, {'error': 'יש להזין מספר טלפון וקוד אימות.'})

    record = storage.get_verification_code(phone)
    if not record:
        return _json_response(400, {
            'error': 'לא נמצא קוד אימות בתוקף עבור מספר זה. יש לבקש קוד חדש.'
        })

    if not _constant_time_eq(record['code'], submitted_code):
        return _json_response(400, {'error': 'הקוד שהוזן שגוי.'})

    # הקוד תקין - חד-פעמי, מוחקים אותו כדי שלא ישמש שוב
    storage.delete_verification_code(phone)

    session_token = auth.sign_session(phone)
    return _json_response(
        200,
        {'ok': True, 'phone': phone},
        set_cookie=_build_set_cookie(session_token),
    )


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def _action_check_session(headers) -> tuple:
    phone = _get_session_phone(headers)
    if phone:
        return _json_response(200, {'authenticated': True, 'phone': phone})
    return _json_response(200, {'authenticated': False})


def _action_logout() -> tuple:
    return _json_response(200, {'ok': True}, clear_cookie=True)


def _parse_multipart_file_field(content_type: str, body_bytes: bytes, field_name: str):
    """מפרסר גוף multipart/form-data (ללא תלות במודול cgi המיושן/
    שהוסר ב-Python 3.13+) ומחזיר (filename, raw_bytes) עבור שדה הקובץ
    המבוקש, או (None, None) אם לא נמצא.

    משתמש במודול email התקני (לא מיושן) לפענוח ה-MIME, על ידי בניית
    "הודעת" מייל מלאכותית מכותרת ה-Content-Type שהתקבלה + הגוף."""
    header_bytes = f'Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n'.encode('ascii')
    msg = BytesParser(policy=policy.default).parsebytes(header_bytes + body_bytes)

    if not msg.is_multipart():
        return None, None

    for part in msg.iter_parts():
        disposition = part.get('Content-Disposition', '') or ''
        if f'name="{field_name}"' not in disposition and f"name={field_name}" not in disposition:
            continue
        filename = part.get_filename() or 'contacts'
        payload = part.get_payload(decode=True) or b''
        return filename, payload

    return None, None


def _action_upload_contacts(headers, body_bytes: bytes) -> tuple:
    phone = _get_session_phone(headers)
    if not phone:
        return _json_response(401, {'error': 'יש להתחבר (לעבור אימות טלפון) לפני העלאת אנשי קשר.'})

    content_type = headers.get('Content-Type', '')
    if 'multipart/form-data' not in content_type:
        return _json_response(400, {'error': 'יש לשלוח את הקובץ כ-multipart/form-data.'})

    if len(body_bytes) > MAX_UPLOAD_BYTES:
        return _json_response(400, {'error': 'הקובץ גדול מדי (מגבלה: 5MB).'})

    filename, raw_bytes = _parse_multipart_file_field(content_type, body_bytes, 'file')

    if not filename or not raw_bytes:
        return _json_response(400, {'error': 'לא נמצא קובץ בבקשה (שדה file), או שהוא ריק.'})

    try:
        parsed_contacts = contacts_import.parse_contacts_file(filename, raw_bytes)
    except ValueError as exc:
        return _json_response(400, {'error': str(exc)})

    if not parsed_contacts:
        return _json_response(400, {'error': 'לא נמצאו אנשי קשר תקינים בקובץ.'})

    user_data = storage.load_user(phone)
    user_data, added, updated = storage.merge_contacts(user_data, parsed_contacts)
    storage.save_user(user_data)

    return _json_response(200, {
        'ok': True,
        'added': added,
        'updated': updated,
        'total': len(user_data.get('contacts', [])),
    })


# --------------------------------------------------------------------------
# Vercel Python serverless handler
# --------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_OPTIONS(self):
        # תמיכה ב-CORS preflight (לא בשימוש בדרך כלל כשהאתר וה-API
        # באותו דומיין, אך לא מזיק להשאיר)
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _handle(self):
        try:
            parsed_url = urlparse(self.path)
            query_params = parse_qs(parsed_url.query)
            action = query_params.get('action', [''])[0]

            content_length = int(self.headers.get('Content-Length', 0) or 0)
            body_bytes = self.rfile.read(content_length) if content_length else b''

            content_type = self.headers.get('Content-Type', '')

            if action == 'upload_contacts':
                status, payload, set_cookie, clear_cookie = _action_upload_contacts(
                    self.headers, body_bytes
                )
            elif action == 'check_session':
                status, payload, set_cookie, clear_cookie = _action_check_session(self.headers)
            elif action == 'logout':
                status, payload, set_cookie, clear_cookie = _action_logout()
            elif action in ('request_code', 'verify_code'):
                if 'application/x-www-form-urlencoded' in content_type or not content_type:
                    form_params = parse_qs(body_bytes.decode('utf-8')) if body_bytes else {}
                elif 'application/json' in content_type:
                    json_body = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                    form_params = {k: [str(v)] for k, v in json_body.items()}
                else:
                    form_params = parse_qs(body_bytes.decode('utf-8')) if body_bytes else {}

                if action == 'request_code':
                    status, payload, set_cookie, clear_cookie = _action_request_code(form_params)
                else:
                    status, payload, set_cookie, clear_cookie = _action_verify_code(form_params)
            else:
                status, payload, set_cookie, clear_cookie = _json_response(
                    404, {'error': 'פעולה לא מוכרת.'}
                )
        except Exception as exc:  # noqa: BLE001 - חובה להחזיר תמיד JSON תקין
            status, payload, set_cookie, clear_cookie = _json_response(
                500, {'error': f'אירעה שגיאה זמנית: {exc}'}
            )

        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self._send_cors_headers()
        if clear_cookie:
            self.send_header('Set-Cookie', _build_clear_cookie())
        elif set_cookie:
            self.send_header('Set-Cookie', set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self):
        # האתר וה-API משרתים מאותו דומיין (vercel.app) כך ש-CORS אינו
        # קריטי כאן, אך נשאר לגמישות עתידית.
        self.send_header('Access-Control-Allow-Origin', 'null')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

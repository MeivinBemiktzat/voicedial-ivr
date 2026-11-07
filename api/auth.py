"""
auth.py
=======
אימות בעלות על מספר טלפון עבור ממשק האתר (העלאת אנשי קשר), וניהול
session חתום בדפדפן לאחר אימות מוצלח.

עקרון האימות:
----------------
1. באתר המשתמש מקליד מספר טלפון ולוחץ "שלח קוד". נוצר קוד אימות בן 6
   ספרות (storage.create_verification_code) - אך הוא **לא** מוצג
   באתר.
2. המשתמש מתקשר לשלוחת ה-API (אותה שלוחה המשמשת לחיוג הקולי הרגיל)
   מהמספר שהקליד, ואומר את מילת המפתח להפקת הקוד (ר' KEYWORD_GET_CODE
   ב-api/index.py). ימות המשיח מזהה את מספר המתקשר **בעצמו**
   (ApiPhone) - ולכן אי אפשר "להזמין" קוד למספר שאינו שלך: הקוד
   נקרא בקול רק למי שבאמת מתקשר מאותו מספר.
3. המשתמש מקליד את הקוד שנשמע באתר. אם הוא תואם ולא פג תוקף
   (storage.get_verification_code) - נוצר session token חתום
   (sign_session) ונשמר כעוגיית דפדפן. מרגע זה האתר "זוכר" את
   הדפדפן/המספר, עד לחיצה על "יציאה" (שמוחקת את העוגייה).

חשוב: אין כאן "להאמין" למספר טלפון שמגיע מהדפדפן בשום שלב אחרי
האימות הראשוני - כל בקשה מאומתת מול ה-session token החתום (HMAC),
ולא מול מספר טלפון שנשלח מהלקוח בטופס.

כל המפתחות/סודות נטענים אך ורק ממשתני סביבה - אין סודות מוטבעים כאן.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Optional

# --- מפתח חתימת ה-session (HMAC) - נטען ממשתנה סביבה ---
# יש להגדיר ב-Vercel ערך אקראי וסודי משלכם (לדוגמה: פלט של
# `python -c "import secrets; print(secrets.token_hex(32))"`).
SESSION_SECRET = os.environ.get('SESSION_SECRET', '')

SESSION_TTL_SECONDS = 180 * 24 * 60 * 60  # 180 יום - "לא יצטרכו אימות נוסף"
CODE_LENGTH = 6


def generate_code() -> str:
    """מייצר קוד אימות אקראי בן 6 ספרות (כולל אפסים מובילים אפשריים)."""
    return f'{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}'


def _b64url_encode(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(text: str) -> bytes:
    padding = '=' * (-len(text) % 4)
    return urlsafe_b64decode(text + padding)


def sign_session(phone: str) -> str:
    """יוצר session token חתום (HMAC-SHA256) עבור מספר טלפון שעבר
    אימות. הטוקן כולל את מספר הטלפון וזמן תפוגה, וחתום כך שלקוח לא
    יכול לזייף מספר טלפון שלא עבר דרך תהליך האימות בפועל."""
    if not SESSION_SECRET:
        raise RuntimeError('חסר משתנה הסביבה SESSION_SECRET')

    payload = {
        'phone': phone,
        'exp': int(time.time()) + SESSION_TTL_SECONDS,
    }
    payload_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    payload_b64 = _b64url_encode(payload_bytes)

    signature = hmac.new(
        SESSION_SECRET.encode('utf-8'), payload_b64.encode('ascii'), hashlib.sha256
    ).digest()
    signature_b64 = _b64url_encode(signature)

    return f'{payload_b64}.{signature_b64}'


def verify_session(token: str) -> Optional[str]:
    """מאמת session token ומחזיר את מספר הטלפון המאומת אם תקין ולא פג
    תוקף - אחרת None. לעולם אין לסמוך על מספר טלפון שמגיע מהלקוח
    בדרך אחרת מאשר דרך הפונקציה הזו."""
    if not SESSION_SECRET or not token or '.' not in token:
        return None

    payload_b64, _, signature_b64 = token.partition('.')

    expected_signature = hmac.new(
        SESSION_SECRET.encode('utf-8'), payload_b64.encode('ascii'), hashlib.sha256
    ).digest()

    try:
        provided_signature = _b64url_decode(signature_b64)
    except Exception:  # noqa: BLE001 - קלט לקוח לא תקין, ייכשל בבדיקה
        return None

    if not hmac.compare_digest(expected_signature, provided_signature):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode('utf-8'))
    except Exception:  # noqa: BLE001 - קלט לקוח לא תקין
        return None

    if not isinstance(payload, dict):
        return None

    if time.time() > payload.get('exp', 0):
        return None

    phone = payload.get('phone')
    if not phone or not isinstance(phone, str):
        return None

    return phone

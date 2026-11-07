"""
storage.py
==========
ניהול קריאה/כתיבה של קובצי JSON (אחד לכל מספר מחייג) מול Hugging Face
Storage, דרך ממשק תואם S3 (boto3).

מבנה קובץ לדוגמה (0501234567.json):
{
  "phone": "0501234567",
  "contacts": [
    {"name": "בעלי", "phone": "0521111111"},
    {"name": "דוד", "phone": "0532222222"}
  ],
  "history": [
    {"target": "0521111111", "name": "בעלי", "timestamp": "2024-01-01T12:00:00"}
  ]
}

כל הפרטים הנדרשים לחיבור (namespace, bucket, מפתחות) נטענים אך ורק
ממשתני סביבה - אין מפתחות מוטבעים בקוד.

הערה חשובה על ה-endpoint של Hugging Face:
לפי התיעוד הרשמי (https://huggingface.co/docs/hub/storage-buckets-s3),
ה-endpoint הוא בפורמט https://s3.hf.co/<namespace> (namespace = שם
המשתמש או הארגון שלכם ב-Hugging Face), ה-region הנדרש הוא קבוע
("us-east-1"), ונדרש addressing_style="path". כל אלה מוגדרים כאן
בהתאם לתיעוד - ר' README.md להרחבה על הפקת מפתחות.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# --- הגדרות חיבור - נטענות ממשתני הסביבה ב-Vercel ---
# HF_S3_NAMESPACE - שם המשתמש/הארגון שלכם ב-Hugging Face (ה-namespace)
#                   משמש לבניית ה-endpoint: https://s3.hf.co/<namespace>
HF_S3_NAMESPACE = os.environ.get('HF_S3_NAMESPACE')
HF_S3_ACCESS_KEY = os.environ.get('HF_S3_ACCESS_KEY')      # מתחיל ב-HFAK...
HF_S3_SECRET_KEY = os.environ.get('HF_S3_SECRET_KEY')
HF_S3_BUCKET = os.environ.get('HF_S3_BUCKET')               # שם ה-bucket בלבד (ללא namespace/)

MAX_HISTORY_ENTRIES = 10  # כמה שיחות אחרונות לשמור/להציג


def _client():
    missing = [
        name for name, val in (
            ('HF_S3_NAMESPACE', HF_S3_NAMESPACE),
            ('HF_S3_ACCESS_KEY', HF_S3_ACCESS_KEY),
            ('HF_S3_SECRET_KEY', HF_S3_SECRET_KEY),
            ('HF_S3_BUCKET', HF_S3_BUCKET),
        ) if not val
    ]
    if missing:
        raise RuntimeError(
            f'חסרים משתני סביבה לחיבור ל-Hugging Face Storage: {", ".join(missing)}'
        )

    endpoint_url = f'https://s3.hf.co/{HF_S3_NAMESPACE}'

    return boto3.client(
        's3',
        endpoint_url=endpoint_url,
        aws_access_key_id=HF_S3_ACCESS_KEY,
        aws_secret_access_key=HF_S3_SECRET_KEY,
        config=Config(
            region_name='us-east-1',
            s3={'addressing_style': 'path'},
            request_checksum_calculation='when_required',
            response_checksum_validation='when_required',
        ),
    )


def _normalize_phone(phone: str) -> str:
    """מנרמל מספר טלפון לשימוש כשם קובץ: רק ספרות."""
    return ''.join(ch for ch in (phone or '') if ch.isdigit())


def _key_for(phone: str) -> str:
    return f'{_normalize_phone(phone)}.json'


def _empty_user(phone: str) -> dict:
    return {
        'phone': _normalize_phone(phone),
        'contacts': [],
        'history': [],
    }


def load_user(phone: str) -> dict:
    """טוען את קובץ המשתמש לפי מספר טלפון. אם לא קיים - מחזיר מבנה ריק
    (אינו נכתב למחסן עד לשמירה מפורשת)."""
    client = _client()
    key = _key_for(phone)

    try:
        obj = client.get_object(Bucket=HF_S3_BUCKET, Key=key)
        data = json.loads(obj['Body'].read().decode('utf-8'))
        data.setdefault('contacts', [])
        data.setdefault('history', [])
        data.setdefault('phone', _normalize_phone(phone))
        return data
    except ClientError as exc:
        error_code = exc.response.get('Error', {}).get('Code', '')
        if error_code in ('NoSuchKey', '404', 'NotFound'):
            return _empty_user(phone)
        raise


def save_user(data: dict) -> None:
    """שומר את קובץ המשתמש בשלמותו (כתיבה מלאה - לא עדכון חלקי)."""
    client = _client()
    key = _key_for(data['phone'])
    body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')

    client.put_object(
        Bucket=HF_S3_BUCKET,
        Key=key,
        Body=body,
        ContentType='application/json; charset=utf-8',
    )


def find_contact(data: dict, name_query: str) -> Optional[dict]:
    """מאתר איש קשר לפי שם (התאמה מקורבת - "מכיל" בשני הכיוונים,
    כדי להתמודד עם אי-דיוקים בתמלול)."""
    if not name_query:
        return None

    normalized_query = name_query.strip()
    if not normalized_query:
        return None

    # התאמה מדויקת קודם
    for contact in data.get('contacts', []):
        if contact.get('name', '').strip() == normalized_query:
            return contact

    # התאמה חלקית (containment) בשני הכיוונים
    for contact in data.get('contacts', []):
        contact_name = contact.get('name', '').strip()
        if not contact_name:
            continue
        if contact_name in normalized_query or normalized_query in contact_name:
            return contact

    return None


def add_contact(data: dict, name: str, phone: str) -> dict:
    """מוסיף איש קשר חדש (או מעדכן אם השם כבר קיים), ומחזיר את האובייקט
    המעודכן. אינו שומר בעצמו - יש לקרוא ל-save_user בנפרד."""
    normalized_phone = _normalize_phone(phone)
    contacts = data.setdefault('contacts', [])

    for contact in contacts:
        if contact.get('name', '').strip() == name.strip():
            contact['phone'] = normalized_phone
            return data

    contacts.append({'name': name.strip(), 'phone': normalized_phone})
    return data


def merge_contacts(data: dict, new_contacts: list) -> dict:
    """ממזג רשימת אנשי קשר חדשה (כפי שחולצה מקובץ שהועלה דרך האתר) אל
    תוך אנשי הקשר הקיימים. new_contacts: רשימת dict עם 'name' ו-'phone'.
    התנהגות המיזוג: לפי שם זהה (strip, השוואה מדויקת) - מעדכן מספר;
    אחרת - מוסיף כאיש קשר חדש. מחזיר (data, added_count, updated_count).
    אינו שומר בעצמו - יש לקרוא ל-save_user בנפרד."""
    contacts = data.setdefault('contacts', [])
    by_name = {c.get('name', '').strip(): c for c in contacts if c.get('name', '').strip()}

    added = 0
    updated = 0
    for entry in new_contacts:
        name = (entry.get('name') or '').strip()
        phone = _normalize_phone(entry.get('phone') or '')
        if not name or not phone:
            continue

        existing = by_name.get(name)
        if existing:
            if existing.get('phone') != phone:
                existing['phone'] = phone
                updated += 1
        else:
            new_contact = {'name': name, 'phone': phone}
            contacts.append(new_contact)
            by_name[name] = new_contact
            added += 1

    return data, added, updated


def add_history_entry(data: dict, target_phone: str, name: Optional[str] = None) -> dict:
    """מוסיף רשומת היסטוריה חדשה בתחילת הרשימה, ושומר רק על
    MAX_HISTORY_ENTRIES הרשומות האחרונות."""
    history = data.setdefault('history', [])

    entry = {
        'target': _normalize_phone(target_phone),
        'name': name,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    history.insert(0, entry)
    data['history'] = history[:MAX_HISTORY_ENTRIES]
    return data


# --------------------------------------------------------------------------
# קודי אימות (לאימות בעלות על מספר טלפון דרך שיחה לשלוחת ה-API)
# --------------------------------------------------------------------------
# נשמרים בקובץ נפרד לגמרי מקובץ אנשי הקשר של המשתמש (תחת תיקיית verify/
# ב-bucket), כך שאין שום חפיפה/סיכון לדריסת נתוני אנשי קשר. כל רשומה
# כוללת את הקוד עצמו ואת זמן התפוגה שלה (JSON בלבד - ללא קבצים נוספים).

VERIFICATION_CODE_TTL_SECONDS = 5 * 60  # 5 דקות תוקף לקוד שנוצר


def _verify_key_for(phone: str) -> str:
    return f'verify/{_normalize_phone(phone)}.json'


def create_verification_code(phone: str, code: str) -> None:
    """שומר קוד אימות חדש עבור מספר טלפון (דורס קוד קודם אם קיים)."""
    client = _client()
    key = _verify_key_for(phone)
    expires_at = datetime.now(timezone.utc).timestamp() + VERIFICATION_CODE_TTL_SECONDS
    body = json.dumps({
        'phone': _normalize_phone(phone),
        'code': code,
        'expires_at': expires_at,
    }, ensure_ascii=False).encode('utf-8')

    client.put_object(
        Bucket=HF_S3_BUCKET,
        Key=key,
        Body=body,
        ContentType='application/json; charset=utf-8',
    )


def get_verification_code(phone: str) -> Optional[dict]:
    """טוען את רשומת קוד האימות הפעילה עבור מספר טלפון, אם קיימת ולא
    פגה - אחרת מחזיר None."""
    client = _client()
    key = _verify_key_for(phone)

    try:
        obj = client.get_object(Bucket=HF_S3_BUCKET, Key=key)
        record = json.loads(obj['Body'].read().decode('utf-8'))
    except ClientError as exc:
        error_code = exc.response.get('Error', {}).get('Code', '')
        if error_code in ('NoSuchKey', '404', 'NotFound'):
            return None
        raise

    if datetime.now(timezone.utc).timestamp() > record.get('expires_at', 0):
        return None

    return record


def delete_verification_code(phone: str) -> None:
    """מוחק את קוד האימות עבור מספר טלפון (לאחר שימוש מוצלח, או כדי
    לאפשר יצירת קוד חדש)."""
    client = _client()
    key = _verify_key_for(phone)
    try:
        client.delete_object(Bucket=HF_S3_BUCKET, Key=key)
    except ClientError:
        pass

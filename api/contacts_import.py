"""
contacts_import.py
===================
פרסור קבצי אנשי קשר שהועלו דרך האתר, בכמה פורמטים נפוצים, והחזרתם
כרשימת dict אחידה: [{'name': ..., 'phone': ...}, ...]

פורמטים נתמכים:
- VCF / vCard (גרסאות 2.1 / 3.0 / 4.0) - הפורמט הנפוץ ביותר ליצוא/יבוא
  אנשי קשר מטלפון (iPhone, אנדרואיד, Outlook ועוד). ממומש כאן פרסור
  עצמאי מבוסס regex/stdlib בלבד (ללא תלות חיצונית) - תומך ב-unfolding
  של שורות (RFC 6350), בפרמטרים אחרי נקודה-פסיק (למשל TYPE=CELL),
  ובקידוד QUOTED-PRINTABLE הנפוץ בקבצי vCard ישנים יותר (2.1).
- CSV - פורמט יצוא נפוץ (Google Contacts, Outlook ועוד). מזהה עמודות
  שם/טלפון לפי כותרות נפוצות בעברית/אנגלית, ובהיעדר כותרת מתאימה
  מנסה לנחש לפי תוכן העמודות (עמודה עם רוב ערכים מספריים = טלפון).
- TXT - קובץ טקסט פשוט, שורה לכל איש קשר, בפורמט "שם,טלפון" או
  "שם: טלפון" או "שם - טלפון" (מופרד בפסיק/נקודתיים/מקף/טאב).

כל הפונקציות כאן הן parsing בלבד - אינן ניגשות לאחסון/רשת.
"""

import csv
import io
import re
from typing import List, Tuple

PHONE_CHARS_RE = re.compile(r'[\d+]')
NON_PHONE_RE = re.compile(r'[^\d+]')


# --------------------------------------------------------------------------
# נרמול/זיהוי טלפון
# --------------------------------------------------------------------------

def _clean_phone(raw: str) -> str:
    """משאיר רק ספרות (ו-+ מוביל אם קיים) ממחרוזת טלפון גולמית."""
    raw = (raw or '').strip()
    if not raw:
        return ''
    has_plus = raw.startswith('+')
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ''
    return f'+{digits}' if has_plus else digits


def _looks_like_phone_value(value: str) -> bool:
    """הערכה גסה: האם מחרוזת כלשהי 'נראית' כמו מספר טלפון (רוב
    התווים ספרות, אורך סביר)."""
    value = (value or '').strip()
    if not value:
        return False
    digits = sum(1 for ch in value if ch.isdigit())
    return digits >= 6 and digits >= len(value) * 0.6


# --------------------------------------------------------------------------
# VCF / vCard
# --------------------------------------------------------------------------

def _unfold_vcard_lines(text: str) -> List[str]:
    """איחוד שורות מקופלות (RFC 6350: שורת המשך מתחילה ברווח/טאב)."""
    raw_lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    unfolded: List[str] = []
    for line in raw_lines:
        if line.startswith((' ', '\t')) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _decode_quoted_printable(value: str) -> str:
    """פענוח בסיסי של QUOTED-PRINTABLE (נפוץ ב-vCard 2.1 ישן), ללא
    תלות בספריות חיצוניות."""
    try:
        return value.encode('ascii', errors='ignore').decode('quopri_codec')
    except Exception:  # noqa: BLE001 - אם הפענוח נכשל, נחזיר כפי שהתקבל
        import quopri
        try:
            return quopri.decodestring(value.encode('ascii', errors='ignore')).decode(
                'utf-8', errors='replace'
            )
        except Exception:  # noqa: BLE001
            return value


def _parse_vcard_line(line: str) -> Tuple[str, dict, str]:
    """מפרק שורת vCard לשלושה חלקים: (property_name, params_dict, value).
    לדוגמה: 'TEL;TYPE=CELL:052-1234567' -> ('TEL', {'TYPE': 'CELL'}, '052-1234567')
    """
    if ':' not in line:
        return '', {}, ''

    head, _, value = line.partition(':')
    parts = head.split(';')
    prop_name = parts[0].strip().upper()

    params = {}
    for param_part in parts[1:]:
        if '=' in param_part:
            key, _, val = param_part.partition('=')
            params[key.strip().upper()] = val.strip().upper()
        else:
            # פרמטר ללא מפתח (vCard 2.1 ישן, למשל "TEL;CELL:...")
            params.setdefault('TYPE', param_part.strip().upper())

    return prop_name, params, value


def parse_vcf(text: str) -> List[dict]:
    """מפרסר טקסט VCF שלם (יכול להכיל כמה כרטיסי VCARD ברצף) ומחזיר
    רשימת אנשי קשר. איש קשר עם כמה מספרי טלפון -> שורה נפרדת לכל
    מספר (עם אותו שם, או עם סיומת מספרית אם יש יותר ממספר אחד)."""
    contacts: List[dict] = []
    lines = _unfold_vcard_lines(text)

    current_name = ''
    current_fn = ''
    current_phones: List[str] = []
    in_card = False

    def _flush():
        nonlocal current_name, current_fn, current_phones
        name = (current_name or current_fn).strip()
        if name and current_phones:
            if len(current_phones) == 1:
                contacts.append({'name': name, 'phone': current_phones[0]})
            else:
                for idx, phone in enumerate(current_phones, start=1):
                    contacts.append({'name': f'{name} {idx}', 'phone': phone})
        current_name = ''
        current_fn = ''
        current_phones = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        upper = stripped.upper()
        if upper == 'BEGIN:VCARD':
            in_card = True
            current_name = ''
            current_fn = ''
            current_phones = []
            continue
        if upper == 'END:VCARD':
            if in_card:
                _flush()
            in_card = False
            continue
        if not in_card:
            continue

        prop_name, params, value = _parse_vcard_line(stripped)
        if not prop_name:
            continue

        encoding = params.get('ENCODING', '')
        if encoding == 'QUOTED-PRINTABLE':
            value = _decode_quoted_printable(value)

        if prop_name == 'N':
            # N:שם_משפחה;שם_פרטי;שם_אמצעי;תואר;סיומת
            name_parts = [p for p in value.split(';') if p]
            if name_parts:
                # סדר קריא: שם פרטי ואז שם משפחה (אם יש שניהם)
                if len(name_parts) >= 2 and name_parts[1]:
                    current_name = f'{name_parts[1]} {name_parts[0]}'.strip()
                else:
                    current_name = name_parts[0].strip()
        elif prop_name == 'FN':
            current_fn = value.strip()
        elif prop_name == 'TEL':
            phone = _clean_phone(value)
            if phone:
                current_phones.append(phone)

    # קובץ VCF לא תקני שלא נסגר כראוי ב-END:VCARD
    if in_card:
        _flush()

    return contacts


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

NAME_HEADER_HINTS = (
    'name', 'שם', 'full name', 'display name', 'שם מלא', 'contact name',
)
PHONE_HEADER_HINTS = (
    'phone', 'טלפון', 'mobile', 'נייד', 'tel', 'number', 'phone 1 - value',
    'phone number',
)


def parse_csv(text: str) -> List[dict]:
    """מפרסר CSV ומנסה לזהות עמודות שם/טלפון לפי כותרות נפוצות, ואם
    לא נמצאה התאמה - לפי ניחוש תוכן (עמודה עם ערכים שנראים כמו מספרי
    טלפון)."""
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = [cell.strip().lower() for cell in rows[0]]
    data_rows = rows[1:]

    name_idx = None
    phone_idx = None
    for idx, col in enumerate(header):
        if name_idx is None and any(hint in col for hint in NAME_HEADER_HINTS):
            name_idx = idx
        if phone_idx is None and any(hint in col for hint in PHONE_HEADER_HINTS):
            phone_idx = idx

    if name_idx is None or phone_idx is None:
        # לא נמצאה כותרת מתאימה - ייתכן שאין שורת כותרת כלל. ננחש לפי
        # תוכן: העמודה הראשונה שרוב ערכיה נראים כמו טלפון = טלפון,
        # והעמודה הראשונה האחרת עם ערך טקסטואלי לא ריק = שם.
        rows_for_guess = rows  # כולל שורה 0, למקרה שאין כותרת בפועל
        col_count = max(len(r) for r in rows_for_guess)
        phone_scores = [0] * col_count
        for row in rows_for_guess[:20]:
            for idx in range(len(row)):
                if _looks_like_phone_value(row[idx]):
                    phone_scores[idx] += 1

        if phone_idx is None and phone_scores:
            best_idx = max(range(col_count), key=lambda i: phone_scores[i])
            if phone_scores[best_idx] > 0:
                phone_idx = best_idx

        if name_idx is None:
            for idx in range(col_count):
                if idx != phone_idx:
                    name_idx = idx
                    break

        data_rows = rows_for_guess  # אין כותרת אמיתית לדלג עליה

    if name_idx is None or phone_idx is None:
        return []

    contacts = []
    for row in data_rows:
        if len(row) <= max(name_idx, phone_idx):
            continue
        name = row[name_idx].strip()
        phone = _clean_phone(row[phone_idx])
        if name and phone:
            contacts.append({'name': name, 'phone': phone})

    return contacts


# --------------------------------------------------------------------------
# TXT (שורה לכל איש קשר)
# --------------------------------------------------------------------------

TXT_SEPARATOR_RE = re.compile(r'\t|,|:|-')


def parse_txt(text: str) -> List[dict]:
    """מפרסר קובץ טקסט פשוט - שורה לכל איש קשר, בפורמט
    'שם<מפריד>טלפון' כאשר המפריד הוא טאב/פסיק/נקודתיים/מקף."""
    contacts = []
    for raw_line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw_line.strip()
        if not line:
            continue

        parts = TXT_SEPARATOR_RE.split(line, maxsplit=1)
        if len(parts) != 2:
            continue

        first, second = parts[0].strip(), parts[1].strip()
        if _looks_like_phone_value(second) and first:
            contacts.append({'name': first, 'phone': _clean_phone(second)})
        elif _looks_like_phone_value(first) and second:
            contacts.append({'name': second, 'phone': _clean_phone(first)})

    return contacts


# --------------------------------------------------------------------------
# נקודת כניסה אחידה
# --------------------------------------------------------------------------

def parse_contacts_file(filename: str, raw_bytes: bytes) -> List[dict]:
    """מזהה את פורמט הקובץ לפי סיומת (ובהיעדר זיהוי - לפי תוכן) ומחזיר
    רשימת אנשי קשר אחידה. זורק ValueError אם לא ניתן לפרסר כלל."""
    lower_name = (filename or '').lower()

    # ניסיון פענוח טקסטואלי (UTF-8, ואם נכשל - עם טיפול בשגיאות)
    try:
        text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        text = raw_bytes.decode('utf-8', errors='replace')

    if lower_name.endswith('.vcf') or lower_name.endswith('.vcard') or 'BEGIN:VCARD' in text.upper():
        contacts = parse_vcf(text)
        if contacts:
            return contacts
        raise ValueError('לא נמצאו אנשי קשר תקינים בקובץ ה-VCF')

    if lower_name.endswith('.csv'):
        contacts = parse_csv(text)
        if contacts:
            return contacts
        raise ValueError('לא נמצאו אנשי קשר תקינים בקובץ ה-CSV')

    if lower_name.endswith('.txt'):
        contacts = parse_txt(text)
        if contacts:
            return contacts
        raise ValueError('לא נמצאו אנשי קשר תקינים בקובץ הטקסט')

    # סיומת לא מוכרת - ננסה לפי תוכן, מהפורמט הקפדני ביותר לגמיש ביותר
    for parser in (parse_vcf, parse_csv, parse_txt):
        contacts = parser(text)
        if contacts:
            return contacts

    raise ValueError(
        'לא זוהה פורמט קובץ נתמך. הפורמטים הנתמכים הם VCF (vCard), CSV וטקסט פשוט.'
    )

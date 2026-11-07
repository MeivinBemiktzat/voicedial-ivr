"""
transcription.py
=================
תמלול קול - מותאם מהקובץ ששותף על ידי המשתמש (transcribe.py).

חשוב: זהו בדיוק אותו קוד התמלול שסופק - הוא שונה (adapted) כאן רק כדי
לפעול כפונקציה פנימית בתוך api/index.py, במקום כ-endpoint Vercel נפרד
(handler עם do_POST). הלוגיקה של התמלול עצמו (recognize_google, ריפוד
השקט) לא שונתה כלל.

בכוונה **לא** נעשה שימוש במנוע התמלול המובנה של ימות המשיח (סוג שאלה
'voice' בהגדרות api_XXX). השאלות במודול ה-IVR שלנו מוגדרות כסוג 'record'
בלבד (שמירת הקלטה, ללא תמלול מצד ימות) - ולאחר מכן אנחנו מורידים את
קובץ ה-wav שנשמר דרך ה-Management API (DownloadFile) ומתמללים אותו כאן,
בעזרת הפונקציה הזו בלבד.
"""

import io

import requests
import speech_recognition as sr
from pydub import AudioSegment

# שפת התמלול - עברית. ניתן לשנות ל-'en-US' וכו' אם יידרש בעתיד.
TRANSCRIBE_LANGUAGE = 'he-IL'

# משך ריפוד השקט לפני/אחרי ההקלטה (מילישניות) - כפי שהתבקש במקור
SILENCE_PADDING_MS = 500


def pad_with_silence(wav_bytes: bytes) -> bytes:
    """מוסיף שקט לפני ואחרי קובץ wav, ומחזיר בייטים של wav חדש.

    (זהה לחלוטין לפונקציה שסופקה על ידי המשתמש בקובץ transcribe.py)
    """
    audio = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    silence = AudioSegment.silent(duration=SILENCE_PADDING_MS, frame_rate=audio.frame_rate)
    padded = silence + audio + silence
    out = io.BytesIO()
    padded.export(out, format='wav')
    return out.getvalue()


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
    """מתמלל בייטי wav לטקסט. מחזיר מחרוזת ריקה אם לא זוהה דיבור.

    (זהה לחלוטין לפונקציה שסופקה על ידי המשתמש בקובץ transcribe.py -
    כולל שיטת התמלול recognize_google() ללא API key חיצוני)
    """
    padded_bytes = pad_with_silence(wav_bytes)

    recognizer = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(padded_bytes)) as source:
        audio_data = recognizer.record(source)

    try:
        return recognizer.recognize_google(audio_data, language=TRANSCRIBE_LANGUAGE)
    except sr.UnknownValueError:
        # לא זוהה דיבור בהקלטה - לא שגיאה, פשוט אין תוצאה
        return ''
    except sr.RequestError as exc:
        raise RuntimeError(f'שירות התמלול של גוגל לא זמין כרגע: {exc}') from exc


# --- הורדת קובץ ההקלטה מימות המשיח (Management API) ---

YEMOT_MANAGEMENT_API_BASE = 'https://www.call2all.co.il/ym/api'


def download_recording(system_id: str, password: str, ivr_path: str, timeout: int = 15) -> bytes:
    """מוריד קובץ הקלטה (wav) מהמערכת של ימות המשיח דרך ה-Management API
    (פקודת DownloadFile), ומחזיר את בייטי ה-wav הגולמיים.

    system_id / password - פרטי ההתחברות של מערכת הימות (נטענים ממשתני סביבה
    בקוד הקורא, ולא מוטבעים כאן).
    ivr_path - נתיב הקובץ בתוך ivr2, לדוגמה: "/5/6/000.wav"
               (צריך להתחיל ב-/ ולא להסתיים ב-/)
    """
    token = f'{system_id}:{password}'
    path = f'ivr2:{ivr_path}'

    response = requests.get(
        f'{YEMOT_MANAGEMENT_API_BASE}/DownloadFile',
        params={'token': token, 'path': path},
        timeout=timeout,
    )
    response.raise_for_status()

    content_type = response.headers.get('Content-Type', '')
    if 'audio' not in content_type and 'wav' not in content_type:
        # ימות מחזירה JSON עם שגיאה (responseStatus) כאשר הקובץ לא נמצא/נכשל
        raise RuntimeError(
            f'הורדת קובץ ההקלטה מימות נכשלה (path={ivr_path}): {response.text[:300]}'
        )

    return response.content


def transcribe_yemot_recording(system_id: str, password: str, ivr_path: str) -> str:
    """פונקציית נוחות: מורידה הקלטה מימות ומתמללת אותה במקשה אחת."""
    wav_bytes = download_recording(system_id, password, ivr_path)
    return transcribe_wav_bytes(wav_bytes)

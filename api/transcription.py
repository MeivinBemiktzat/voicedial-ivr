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
"""
transcription.py
=================
תמלול קול מותאם ל-Vercel ללא צורך ב-ffmpeg או ב-pydub.
"""

import io
import requests
import speech_recognition as sr

TRANSCRIBE_LANGUAGE = 'he-IL'
SILENCE_PADDING_MS = 500


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
    """מתמלל בייטי wav לטקסט.
    מבצע ריפוד שקט ישירות על נתוני ה-PCM ללא צורך בתוכנות חיצוניות (ffmpeg)."""
    recognizer = sr.Recognizer()

    with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
        audio_data = recognizer.record(source)

    # הוספת 500ms שקט לפני ואחרי להגברת הדיוק של גוגל
    silence_samples = int(audio_data.sample_rate * (SILENCE_PADDING_MS / 1000.0))
    silence_bytes = b'\x00' * (audio_data.sample_width * silence_samples)

    padded_audio = sr.AudioData(
        silence_bytes + audio_data.frame_data + silence_bytes,
        audio_data.sample_rate,
        audio_data.sample_width,
    )

    try:
        return recognizer.recognize_google(padded_audio, language=TRANSCRIBE_LANGUAGE)
    except sr.UnknownValueError:
        return ''
    except sr.RequestError as exc:
        raise RuntimeError(f'שירות התמלול של גוגל לא זמין כרגע: {exc}') from exc


# --- הורדת קובץ ההקלטה מימות המשיח (Management API) ---

YEMOT_MANAGEMENT_API_BASE = 'https://www.call2all.co.il/ym/api'


def download_recording(token: str, ivr_path: str, timeout: int = 15) -> bytes:
    path = f'ivr2:{ivr_path}'

    response = requests.get(
        f'{YEMOT_MANAGEMENT_API_BASE}/DownloadFile',
        params={'token': token, 'path': path},
        timeout=timeout,
    )
    response.raise_for_status()

    content_type = response.headers.get('Content-Type', '')
    if 'audio' not in content_type and 'wav' not in content_type:
        raise RuntimeError(
            f'הורדת קובץ ההקלטה מימות נכשלה (path={ivr_path}): {response.text[:300]}'
        )

    return response.content


def transcribe_yemot_recording(token: str, ivr_path: str) -> str:
    wav_bytes = download_recording(token, ivr_path)
    return transcribe_wav_bytes(wav_bytes)

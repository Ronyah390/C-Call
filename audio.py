# E:\c-call-ivr\audio.py
"""Audio + phone-number helpers.

Generates TTS prompts (gTTS, ElevenLabs, Gemini), converts audio to 8kHz
mono mu-law (.ulaw) for RTP playback, and normalizes/formats phone numbers.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from gtts import gTTS
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
WORK_DIR = INSTANCE_DIR / "generated"
UPLOAD_DIR = INSTANCE_DIR / "uploads"
SOUNDS_DIR = INSTANCE_DIR / "sounds"
RECORDINGS_DIR = INSTANCE_DIR / "recordings"

# Gemini voices (hardcoded — no listing API yet)
GEMINI_VOICES = [
    "Aoede", "Charon", "Fenrir", "Kore", "Puck", "Zephyr",
    "Algieba", "Despina", "Erinome", "Gacrux", "Laomedeia",
    "Pulcherrima", "Rasalas", "Sadachbia", "Sadaltager",
    "Schedar", "Sulafat", "Umbriel", "Vindemiatrix",
    "Achernar", "Achird", "Algenib", "Alnilam", "Autonoe",
    "Callirrhoe", "Diphda", "Enceladus", "Iapetus", "Izar",
    "Zubenelgenubi",
]


def ensure_dirs() -> None:
    for path in (INSTANCE_DIR, WORK_DIR, UPLOAD_DIR, SOUNDS_DIR, RECORDINGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ffmpeg_path() -> str:
    return os.environ.get("FFMPEG_PATH", "ffmpeg")


def convert_to_call_audio(source_path: Path, audio_id: str) -> str:
    """Convert any audio file to an 8kHz mono .ulaw stream. Returns basename."""
    ensure_dirs()
    audio_name = f"ivr-{audio_id}"
    wav_path = SOUNDS_DIR / f"{audio_name}.wav"
    ulaw_path = SOUNDS_DIR / f"{audio_name}.ulaw"

    subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(source_path),
         "-ar", "8000", "-ac", "1", str(wav_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(wav_path),
         "-acodec", "pcm_mulaw", "-f", "mulaw", "-ar", "8000", "-ac", "1",
         str(ulaw_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return audio_name


# ---------------------------------------------------------------------------
# gTTS (default / fallback)
# ---------------------------------------------------------------------------

def create_gtts_audio(text: str, lang: str = "bn", audio_id: str | None = None) -> str:
    if not text.strip():
        raise ValueError("Prompt text is required.")
    ensure_dirs()
    audio_id = audio_id or uuid.uuid4().hex[:12]
    mp3_path = WORK_DIR / f"gtts-{audio_id}.mp3"
    gTTS(text=text, lang=lang).save(str(mp3_path))
    return convert_to_call_audio(mp3_path, audio_id)


# ---------------------------------------------------------------------------
# ElevenLabs v3
# ---------------------------------------------------------------------------

def list_elevenlabs_voices(api_key: str) -> list[dict]:
    """Return list of ElevenLabs voices [{voice_id, name, category, labels}]."""
    if not api_key:
        raise ValueError("ElevenLabs API key is not configured.")
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        return [
            {
                "voice_id": v.get("voice_id", ""),
                "name": v.get("name", ""),
                "category": v.get("category", ""),
                "labels": v.get("labels", {}),
                "preview_url": v.get("preview_url", ""),
            }
            for v in data.get("voices", [])
        ]
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"ElevenLabs voice list failed ({exc.code}): {body_text}") from exc
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs voice list failed: {exc}") from exc


def create_elevenlabs_audio(
    text: str,
    voice_id: str,
    api_key: str,
    model_id: str = "eleven_v3",
    audio_id: str | None = None,
) -> str:
    """Generate speech via ElevenLabs v3 API. Returns ulaw basename."""
    if not text.strip():
        raise ValueError("Prompt text is required.")
    if not api_key:
        raise ValueError("ElevenLabs API key is not configured.")
    if not voice_id:
        raise ValueError("ElevenLabs voice ID is not selected.")
    ensure_dirs()
    audio_id = audio_id or uuid.uuid4().hex[:12]
    mp3_path = WORK_DIR / f"elevenlabs-{audio_id}.mp3"

    body = json.dumps({
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode()

    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            mp3_path.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"ElevenLabs API error {exc.code}: {body_text}") from exc
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs request failed: {exc}") from exc

    return convert_to_call_audio(mp3_path, audio_id)


# ---------------------------------------------------------------------------
# Google Gemini TTS
# ---------------------------------------------------------------------------

def list_gemini_voices() -> list[dict]:
    """Return hardcoded list of Gemini TTS voices."""
    return [{"voice_name": v, "language": "multi"} for v in GEMINI_VOICES]


def create_gemini_audio(
    text: str,
    voice_name: str,
    api_key: str,
    audio_id: str | None = None,
    model_id: str = "gemini-2.5-flash-preview-tts",
) -> str:
    """Generate speech via Google Gemini 2.5 Flash TTS. Returns ulaw basename."""
    if not text.strip():
        raise ValueError("Prompt text is required.")
    if not api_key:
        raise ValueError("Gemini API key is not configured.")
    ensure_dirs()
    audio_id = audio_id or uuid.uuid4().hex[:12]
    voice_name = voice_name or "Puck"

    body = json.dumps({
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice_name}
                }
            },
        },
    }).encode()

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:generateContent?key={api_key}"
    )
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"Gemini API error {exc.code}: {body_text}") from exc
    except Exception as exc:
        raise RuntimeError(f"Gemini request failed: {exc}") from exc

    try:
        part = data["candidates"][0]["content"]["parts"][0]
        inline_audio = part.get("inlineData") or part.get("inline_data")
        audio_b64 = inline_audio["data"]
        mime_type = inline_audio.get("mimeType") or inline_audio.get("mime_type") or ""
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response structure: {data}") from exc

    raw_bytes = base64.b64decode(audio_b64)
    rate_match = re.search(r"rate=(\d+)", mime_type)
    channels_match = re.search(r"channels=(\d+)", mime_type)
    sample_rate = rate_match.group(1) if rate_match else "24000"
    channels = channels_match.group(1) if channels_match else "1"

    # Gemini returns raw PCM (usually 24kHz, 16-bit, mono) — save as .raw then convert
    raw_path = WORK_DIR / f"gemini-{audio_id}.raw"
    raw_path.write_bytes(raw_bytes)

    # Convert raw PCM → call audio (ffmpeg handles the sample-rate conversion)
    audio_name = f"ivr-{audio_id}"
    ulaw_path = SOUNDS_DIR / f"{audio_name}.ulaw"
    subprocess.run(
        [
            ffmpeg_path(), "-y",
            "-f", "s16le", "-ar", sample_rate, "-ac", channels,
            "-i", str(raw_path),
            "-acodec", "pcm_mulaw", "-f", "mulaw", "-ar", "8000", "-ac", "1",
            str(ulaw_path),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return audio_name


# ---------------------------------------------------------------------------
# Unified TTS dispatcher
# ---------------------------------------------------------------------------

def create_tts_audio(
    text: str,
    provider: str = "gtts",
    lang: str = "bn",
    voice_id: str = "",
    voice_name: str = "",
    elevenlabs_api_key: str = "",
    gemini_api_key: str = "",
    elevenlabs_model: str = "eleven_v3",
    gemini_model: str = "gemini-2.5-flash-preview-tts",
    audio_id: str | None = None,
) -> str:
    """Dispatch TTS to the right provider. Falls back to gTTS on missing config."""
    if provider == "elevenlabs" and elevenlabs_api_key and voice_id:
        return create_elevenlabs_audio(
            text, voice_id, elevenlabs_api_key,
            model_id=elevenlabs_model or "eleven_v3",
            audio_id=audio_id,
        )
    if provider == "gemini" and gemini_api_key:
        return create_gemini_audio(
            text, voice_name or "Puck", gemini_api_key,
            audio_id=audio_id,
            model_id=gemini_model or "gemini-2.5-flash-preview-tts",
        )
    return create_gtts_audio(text, lang, audio_id)


# ---------------------------------------------------------------------------
# Upload helper
# ---------------------------------------------------------------------------

def create_uploaded_audio(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("Audio upload is required.")
    ensure_dirs()
    audio_id = uuid.uuid4().hex[:12]
    filename = secure_filename(file_storage.filename) or f"upload-{audio_id}"
    source_path = UPLOAD_DIR / f"{audio_id}-{filename}"
    file_storage.save(source_path)
    return convert_to_call_audio(source_path, audio_id)


def ulaw_path_for(audio_name: str) -> Path:
    return SOUNDS_DIR / f"{audio_name}.ulaw"


def ulaw_to_wav_bytes(audio_name: str) -> bytes:
    """Convert a generated .ulaw prompt to WAV bytes for in-browser playback."""
    ensure_dirs()
    ulaw = ulaw_path_for(audio_name)
    if not ulaw.exists():
        raise FileNotFoundError(f"Audio {audio_name}.ulaw was not generated.")
    wav = WORK_DIR / f"preview-{audio_name}.wav"
    subprocess.run(
        [ffmpeg_path(), "-y", "-f", "mulaw", "-ar", "8000", "-ac", "1",
         "-i", str(ulaw), str(wav)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    data = wav.read_bytes()
    try:
        wav.unlink()
    except OSError:
        pass
    return data


def audio_duration_seconds(audio_name: str) -> int:
    path = ulaw_path_for(audio_name)
    if not path.exists():
        return 0
    return max(1, math.ceil(path.stat().st_size / 8000))


# ---------------------------------------------------------------------------
# Phone number helpers (BD-focused)
# ---------------------------------------------------------------------------

def normalize_phone_number(raw_number: str) -> str:
    number = (raw_number or "").strip()
    if not number:
        raise ValueError("Phone number is required.")
    has_plus = number.startswith("+")
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        raise ValueError("Phone number must contain digits.")
    if number.startswith("00") and len(digits) > 2:
        return f"+{digits[2:]}"
    if has_plus:
        return f"+{digits}"
    if digits.startswith("880"):
        return f"+{digits}"
    if digits.startswith("0"):
        return f"+880{digits[1:]}"
    if digits.startswith("1") and len(digits) == 10:
        return f"+880{digits}"
    return f"+880{digits}"


def format_dial_number(normalized_number: str, selected_format: str) -> str:
    number = normalize_phone_number(normalized_number)
    digits = "".join(ch for ch in number if ch.isdigit())
    selected = (selected_format or "local_bd").strip().lower()
    if selected in {"e164", "plus", "plus_e164"}:
        return f"+{digits}"
    if selected in {"local_bd", "bd_local"} and digits.startswith("880"):
        return f"0{digits[3:]}"
    if selected in {"raw", "normalized"}:
        return number
    return digits


def mask_number(number: str) -> str:
    visible = "".join(ch for ch in number if ch.isdigit())
    if len(visible) <= 4:
        return "****"
    return f"{'*' * (len(visible) - 4)}{visible[-4:]}"


def number_match_key(number: str) -> str:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if digits.startswith("880") and len(digits) > 3:
        digits = digits[3:]
    if digits.startswith("0") and len(digits) > 1:
        digits = digits[1:]
    return digits[-10:]

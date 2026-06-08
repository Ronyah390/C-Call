import csv
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import StringIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, flash, g, redirect, render_template, request, url_for
from gtts import gTTS
from werkzeug.utils import secure_filename


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
WORK_DIR = INSTANCE_DIR / "generated"
UPLOAD_DIR = INSTANCE_DIR / "uploads"
SOUNDS_DIR = INSTANCE_DIR / "sounds"
DB_PATH = Path(os.environ.get("CALLBOT_DB", INSTANCE_DIR / "callbot.db"))

CALL_MAX_DURATION_SECONDS = max(1, int(os.environ.get("CALL_MAX_DURATION_SECONDS", "60")))
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024


def ensure_dirs() -> None:
    for path in (INSTANCE_DIR, WORK_DIR, UPLOAD_DIR, SOUNDS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    ensure_dirs()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 0,
                number TEXT NOT NULL,
                audio_mode TEXT NOT NULL,
                credits_charged INTEGER NOT NULL DEFAULT 0,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS cdr_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '',
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                cost TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manual',
                cdr_date TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return os.environ.get(key, default)
    return row["value"]


def set_setting(key: str, value: str) -> None:
    get_db().execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        (key, value),
    )


def sip_config() -> dict[str, str]:
    return {
        "SIP_DOMAIN": get_setting("SIP_DOMAIN"),
        "SIP_PORT": get_setting("SIP_PORT", "5060"),
        "SIP_USER": get_setting("SIP_USER"),
        "SIP_PASSWORD": get_setting("SIP_PASSWORD"),
    }


def dial_format() -> str:
    return get_setting("ASTERISK_DIAL_FORMAT", os.environ.get("ASTERISK_DIAL_FORMAT", "local_bd"))


def sip_config_ready() -> bool:
    config = sip_config()
    return all(config.get(name) for name in ("SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD"))


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


def format_dial_number(normalized_number: str) -> str:
    number = normalize_phone_number(normalized_number)
    digits = "".join(ch for ch in number if ch.isdigit())
    selected_format = dial_format().strip().lower()
    if selected_format in {"e164", "plus", "plus_e164"}:
        return f"+{digits}"
    if selected_format in {"local_bd", "bd_local"} and digits.startswith("880"):
        return f"0{digits[3:]}"
    if selected_format in {"raw", "normalized"}:
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


def ffmpeg_path() -> str:
    return os.environ.get("FFMPEG_PATH", "ffmpeg")


def convert_to_call_audio(source_path: Path, audio_id: str) -> str:
    ensure_dirs()
    audio_name = f"callbot-{audio_id}"
    wav_path = SOUNDS_DIR / f"{audio_name}.wav"
    ulaw_path = SOUNDS_DIR / f"{audio_name}.ulaw"
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(source_path), "-ar", "8000", "-ac", "1", str(wav_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            ffmpeg_path(),
            "-y",
            "-i",
            str(wav_path),
            "-acodec",
            "pcm_mulaw",
            "-f",
            "mulaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            str(ulaw_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return audio_name


def create_gtts_audio(text: str, audio_id: str, lang: str = "bn") -> str:
    if not text.strip():
        raise ValueError("Message text is required.")
    ensure_dirs()
    mp3_path = WORK_DIR / f"gtts-{audio_id}.mp3"
    gTTS(text=text, lang=lang).save(str(mp3_path))
    return convert_to_call_audio(mp3_path, audio_id)


def create_uploaded_audio(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("Audio upload is required.")
    ensure_dirs()
    audio_id = uuid.uuid4().hex[:12]
    filename = secure_filename(file_storage.filename) or f"upload-{audio_id}"
    source_path = UPLOAD_DIR / f"{audio_id}-{filename}"
    file_storage.save(source_path)
    return convert_to_call_audio(source_path, audio_id)


def estimate_call_duration_seconds(audio_name: str, repeat_count: int = 1) -> int:
    ulaw_path = SOUNDS_DIR / f"{audio_name}.ulaw"
    audio_seconds = max(1, math.ceil(ulaw_path.stat().st_size / 8000))
    repeat_count = 2 if int(repeat_count) > 1 else 1
    return audio_seconds * repeat_count + max(0, repeat_count - 1)


def make_call(number: str, audio_name: str, repeat_count: int, max_seconds: int) -> None:
    config = sip_config()
    config["ASTERISK_DIAL_FORMAT"] = dial_format()
    missing = [name for name in ("SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD") if not config.get(name)]
    if missing:
        raise RuntimeError(f"Missing SIP config: {', '.join(missing)}")
    audio_path = SOUNDS_DIR / f"{audio_name}.ulaw"
    if not audio_path.exists():
        raise RuntimeError("Converted call audio is missing.")
    env = os.environ.copy()
    env.update(config)
    with (INSTANCE_DIR / "direct-sip.log").open("ab", buffering=0) as direct_log:
        result = subprocess.run(
            [
                sys.executable,
                str(BASE_DIR / "direct_sip_call.py"),
                format_dial_number(number),
                str(audio_path),
                str(2 if int(repeat_count) > 1 else 1),
                str(max(1, int(max_seconds))),
            ],
            env=env,
            stdout=direct_log,
            stderr=direct_log,
            timeout=max_seconds + int(os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90")) + 30,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError("Call failed or was not answered.")


def log_call(number: str, mode: str, seconds: int) -> None:
    get_db().execute(
        "INSERT INTO calls (user_id, number, audio_mode, credits_charged, duration_seconds) VALUES (0, ?, ?, 0, ?)",
        (mask_number(number), mode, seconds),
    )


def parse_bulk_numbers(raw_numbers: str) -> list[str]:
    candidates = [item.strip() for item in re.split(r"[\n,;]+", raw_numbers or "") if item.strip()]
    csv_file = request.files.get("numbers_csv")
    if csv_file and csv_file.filename:
        raw_csv = csv_file.read().decode("utf-8-sig", errors="ignore")
        for row in csv.reader(StringIO(raw_csv)):
            candidates.extend(cell.strip() for cell in row if cell.strip())
    normalized_numbers = []
    seen = set()
    for candidate in candidates:
        digits = "".join(ch for ch in candidate if ch.isdigit())
        if len(digits) < 10:
            continue
        number = normalize_phone_number(candidate)
        if number not in seen:
            normalized_numbers.append(number)
            seen.add(number)
    if not normalized_numbers:
        raise ValueError("At least one phone number is required.")
    return normalized_numbers


def create_call_audio_from_request() -> tuple[str, str]:
    mode = request.form.get("audio_mode", "tts")
    if mode == "upload":
        return create_uploaded_audio(request.files.get("audio_file")), "custom"
    return create_gtts_audio(request.form.get("message", ""), uuid.uuid4().hex[:12]), "tts"


def place_one_call(number: str, audio_name: str, audio_mode: str, repeat: int) -> int:
    seconds = estimate_call_duration_seconds(audio_name, repeat)
    make_call(number, audio_name, repeat, max(1, seconds))
    log_call(number, audio_mode, seconds)
    return seconds


def call_stats() -> dict[str, int]:
    row = get_db().execute(
        "SELECT COUNT(*) AS total_calls, COALESCE(SUM(duration_seconds), 0) AS total_seconds FROM calls"
    ).fetchone()
    return {
        "total_calls": int(row["total_calls"]),
        "total_minutes": math.ceil(int(row["total_seconds"]) / 60) if row["total_seconds"] else 0,
    }


def cdr_settings() -> dict[str, str]:
    return {
        "CDR_PROVIDER": get_setting("CDR_PROVIDER", "manual"),
        "AMARIP_BASE_URL": get_setting("AMARIP_BASE_URL", "https://amarip.net"),
        "AMARIP_USERNAME": get_setting("AMARIP_USERNAME"),
        "AMARIP_PASSWORD": get_setting("AMARIP_PASSWORD"),
    }


def find_cdr_for_number(number: str) -> sqlite3.Row | None:
    key = number_match_key(number)
    if not key:
        return None
    rows = get_db().execute("SELECT * FROM cdr_records ORDER BY COALESCE(cdr_date, created_at) DESC, id DESC").fetchall()
    for row in rows:
        if number_match_key(row["number"]) == key:
            return row
    return None


def dashboard_calls() -> list[dict]:
    rows = get_db().execute("SELECT * FROM calls ORDER BY created_at DESC LIMIT 12").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        cdr = find_cdr_for_number(row["number"])
        item["cdr_status"] = cdr["status"] if cdr else ""
        item["cdr_duration"] = cdr["duration_seconds"] if cdr else None
        item["cdr_cost"] = cdr["cost"] if cdr else ""
        result.append(item)
    return result


def first_value(data: dict, names: tuple[str, ...], default: str = "") -> str:
    lowered = {str(k).strip().lower(): v for k, v in data.items()}
    for name in names:
        if name in lowered and lowered[name] not in (None, ""):
            return str(lowered[name]).strip()
    return default


def parse_duration(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if ":" in text:
        parts = [int(float(part or 0)) for part in text.split(":")]
        total = 0
        for part in parts:
            total = total * 60 + part
        return total
    try:
        return int(float(text))
    except ValueError:
        return 0


def save_cdr_record(record: dict, source: str) -> bool:
    number = first_value(record, ("number", "callee", "callee_number", "destination", "dst", "to"))
    if not number:
        return False
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 10:
        return False
    status = first_value(record, ("status", "disposition", "hangup_cause", "sip_status_code"))
    duration = parse_duration(first_value(record, ("duration_seconds", "duration", "billable_seconds", "billable_duration", "seconds")))
    cost = first_value(record, ("cost", "call_cost", "price", "charge", "amount"))
    cdr_date = first_value(record, ("date", "start_time", "created_at", "time", "timestamp"), None)
    get_db().execute(
        """
        INSERT INTO cdr_records (number, status, duration_seconds, cost, source, cdr_date)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (number, status, duration, cost, source, cdr_date),
    )
    return True


def import_cdr_csv(file_storage) -> int:
    if not file_storage or not file_storage.filename:
        raise ValueError("CDR CSV file is required.")
    raw_csv = file_storage.read().decode("utf-8-sig", errors="ignore")
    imported = 0
    for row in csv.DictReader(StringIO(raw_csv)):
        if save_cdr_record(row, "manual_csv"):
            imported += 1
    if imported == 0:
        raise ValueError("No CDR rows were imported. Check the CSV headers.")
    return imported


def amarip_request(path: str, method: str = "GET", payload: dict | None = None, token: str | None = None) -> dict:
    settings = cdr_settings()
    base_url = settings["AMARIP_BASE_URL"].rstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AmarIP CDR request failed: HTTP {exc.code} {detail[:180]}") from exc


def fetch_amarip_cdr(limit: int = 100) -> int:
    settings = cdr_settings()
    if not settings["AMARIP_USERNAME"] or not settings["AMARIP_PASSWORD"]:
        raise ValueError("AmarIP username and password are required in Settings.")
    login = amarip_request(
        "/api/login",
        "POST",
        {"username": settings["AMARIP_USERNAME"], "password": settings["AMARIP_PASSWORD"]},
    )
    token = login.get("token") or login.get("access_token")
    if not token:
        raise RuntimeError("AmarIP login did not return a token.")
    data = amarip_request(f"/api/cdr?{urllib.parse.urlencode({'page': 1, 'per_page': min(limit, 100)})}", token=token)
    rows = data.get("data", data if isinstance(data, list) else [])
    imported = 0
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and save_cdr_record(row, "amarip"):
                imported += 1
    return imported


@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        calls=dashboard_calls(),
        stats=call_stats(),
        sip_ready=sip_config_ready(),
        cdr_settings=cdr_settings(),
    )


@app.route("/call", methods=["POST"])
def send_call():
    try:
        repeat = 2 if request.form.get("repeat_count") == "2" else 1
        number = normalize_phone_number(request.form.get("number", ""))
        audio_name, audio_mode = create_call_audio_from_request()
        place_one_call(number, audio_name, audio_mode, repeat)
        get_db().commit()
        flash(f"Call sent to {mask_number(number)}.", "success")
    except Exception as exc:
        get_db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/bulk-call", methods=["POST"])
def send_bulk_call():
    try:
        repeat = 2 if request.form.get("repeat_count") == "2" else 1
        numbers = parse_bulk_numbers(request.form.get("numbers", ""))
        audio_name, audio_mode = create_call_audio_from_request()
        successes = 0
        failures = []
        for number in numbers:
            try:
                place_one_call(number, audio_name, audio_mode, repeat)
                successes += 1
                get_db().commit()
            except Exception as exc:
                get_db().rollback()
                failures.append(f"{mask_number(number)}: {exc}")
        if successes:
            flash(f"Bulk call finished: {successes} sent, {len(failures)} failed.", "success")
        if failures:
            flash("Failed calls: " + " | ".join(failures[:5]), "error")
    except Exception as exc:
        get_db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        try:
            for key in (
                "SIP_DOMAIN",
                "SIP_PORT",
                "SIP_USER",
                "ASTERISK_DIAL_FORMAT",
                "CDR_PROVIDER",
                "AMARIP_BASE_URL",
                "AMARIP_USERNAME",
            ):
                set_setting(key, request.form.get(key, "").strip())
            password = request.form.get("SIP_PASSWORD", "")
            if password:
                set_setting("SIP_PASSWORD", password)
            amarip_password = request.form.get("AMARIP_PASSWORD", "")
            if amarip_password:
                set_setting("AMARIP_PASSWORD", amarip_password)
            get_db().commit()
            flash("Settings saved.", "success")
        except Exception as exc:
            get_db().rollback()
            flash(str(exc), "error")
        return redirect(url_for("settings"))

    config = sip_config()
    password_set = bool(config.pop("SIP_PASSWORD", ""))
    cdr = cdr_settings()
    amarip_password_set = bool(cdr.pop("AMARIP_PASSWORD", ""))
    return render_template(
        "settings.html",
        settings=config,
        password_set=password_set,
        dial_format=dial_format(),
        cdr_settings=cdr,
        amarip_password_set=amarip_password_set,
        sip_ready=sip_config_ready(),
    )


@app.route("/cdr/import", methods=["POST"])
def import_cdr():
    try:
        imported = import_cdr_csv(request.files.get("cdr_csv"))
        get_db().commit()
        flash(f"Imported {imported} CDR records.", "success")
    except Exception as exc:
        get_db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/cdr/fetch-amarip", methods=["POST"])
def fetch_cdr_amarip():
    try:
        imported = fetch_amarip_cdr()
        get_db().commit()
        flash(f"Fetched {imported} AmarIP CDR records.", "success")
    except Exception as exc:
        get_db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/health")
def health():
    return Response("ok\n", mimetype="text/plain")


if __name__ == "__main__":
    init_db()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=True)

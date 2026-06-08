import math
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request as flask_request,
    session,
    url_for,
)
from gtts import gTTS
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
WORK_DIR = INSTANCE_DIR / "generated"
UPLOAD_DIR = INSTANCE_DIR / "uploads"
SOUNDS_DIR = INSTANCE_DIR / "sounds"
DB_PATH = Path(os.environ.get("CALLBOT_DB", INSTANCE_DIR / "callbot.db"))

CALL_MAX_DURATION_SECONDS = max(1, int(os.environ.get("CALL_MAX_DURATION_SECONDS", "60")))
ASTERISK_DIAL_FORMAT = os.environ.get("ASTERISK_DIAL_FORMAT", "e164_noplus")

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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                credits INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                credits INTEGER NOT NULL,
                created_by INTEGER,
                used_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                number TEXT NOT NULL,
                audio_mode TEXT NOT NULL,
                credits_charged INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.com").strip().lower()
        admin_password = os.environ.get("ADMIN_PASSWORD", "change-me")
        row = conn.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (email, password_hash, name, credits, is_admin) VALUES (?, ?, ?, 0, 1)",
                (admin_email, generate_password_hash(admin_password), "Admin"),
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


def sip_config_ready() -> bool:
    config = sip_config()
    return all(config.get(name) for name in ("SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD"))


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


@app.before_request
def load_user() -> None:
    g.user = current_user()


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login"))
        if not g.user["is_admin"]:
            flash("Admin access is required.", "error")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)

    return wrapper


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
    dial_format = ASTERISK_DIAL_FORMAT.strip().lower()
    if dial_format in {"e164", "plus", "plus_e164"}:
        return f"+{digits}"
    if dial_format in {"local_bd", "bd_local"} and digits.startswith("880"):
        return f"0{digits[3:]}"
    if dial_format in {"raw", "normalized"}:
        return number
    return digits


def number_match_key(number: str) -> str:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if digits.startswith("880") and len(digits) > 3:
        digits = digits[3:]
    if digits.startswith("0") and len(digits) > 1:
        digits = digits[1:]
    return digits[-10:]


def mask_number(number: str) -> str:
    visible = "".join(ch for ch in number if ch.isdigit())
    if len(visible) <= 4:
        return "****"
    return f"{'*' * (len(visible) - 4)}{visible[-4:]}"


def credits_for_duration(duration_seconds: int) -> int:
    return max(1, math.ceil(max(1, duration_seconds) / CALL_MAX_DURATION_SECONDS))


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
    required = ["SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD"]
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise RuntimeError(f"Missing SIP config: {', '.join(missing)}")
    audio_path = SOUNDS_DIR / f"{audio_name}.ulaw"
    if not audio_path.exists():
        raise RuntimeError("Converted call audio is missing.")
    dial_number = format_dial_number(number)
    log_path = INSTANCE_DIR / "direct-sip.log"
    env = os.environ.copy()
    env.update(config)
    with log_path.open("ab", buffering=0) as direct_log:
        result = subprocess.run(
            [
                sys.executable,
                str(BASE_DIR / "direct_sip_call.py"),
                dial_number,
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


def require_user_credits(user_id: int, needed: int) -> None:
    row = get_db().execute("SELECT credits, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if row["is_admin"]:
        return
    if row["credits"] < needed:
        raise ValueError(f"Not enough credits. Needed {needed}, available {row['credits']}.")


def deduct_user_credits(user_id: int, amount: int) -> None:
    if amount <= 0:
        return
    row = get_db().execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row["is_admin"]:
        return
    get_db().execute("UPDATE users SET credits = credits - ? WHERE id = ?", (amount, user_id))


def log_call(user_id: int, number: str, mode: str, credits: int, seconds: int) -> None:
    get_db().execute(
        "INSERT INTO calls (user_id, number, audio_mode, credits_charged, duration_seconds) VALUES (?, ?, ?, ?, ?)",
        (user_id, mask_number(number), mode, credits, seconds),
    )


def run_user_call(user_id: int, number: str, audio_name: str, mode: str, repeat_count: int) -> dict:
    normalized = normalize_phone_number(number)
    seconds = estimate_call_duration_seconds(audio_name, repeat_count)
    credits = credits_for_duration(seconds)
    require_user_credits(user_id, credits)
    max_seconds = credits * CALL_MAX_DURATION_SECONDS
    make_call(normalized, audio_name, repeat_count, max_seconds)
    charge = 0 if g.user and g.user["is_admin"] else credits
    deduct_user_credits(user_id, charge)
    log_call(user_id, normalized, mode, charge, max_seconds)
    get_db().commit()
    return {"number": mask_number(normalized), "duration_seconds": seconds, "credits_charged": charge}


def create_redeem_code(created_by: int, credits: int) -> str:
    if credits <= 0:
        raise ValueError("Credit amount must be positive.")
    code = "-".join(
        "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
        for _ in range(3)
    )
    get_db().execute(
        "INSERT INTO redeem_codes (code, credits, created_by) VALUES (?, ?, ?)",
        (code, credits, created_by),
    )
    get_db().commit()
    return code


def redeem_code(user_id: int, code: str) -> int:
    clean = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if len(clean) == 12:
        clean = f"{clean[:4]}-{clean[4:8]}-{clean[8:]}"
    row = get_db().execute("SELECT * FROM redeem_codes WHERE code = ?", (clean,)).fetchone()
    if not row:
        raise ValueError("Redeem code was not found.")
    if row["used_by"]:
        raise ValueError("Redeem code has already been used.")
    get_db().execute("UPDATE users SET credits = credits + ? WHERE id = ?", (row["credits"], user_id))
    get_db().execute(
        "UPDATE redeem_codes SET used_by = ?, used_at = CURRENT_TIMESTAMP WHERE code = ?",
        (user_id, clean),
    )
    get_db().commit()
    return row["credits"]


def normalize_client_name(name: str) -> str:
    clean = " ".join((name or "").strip().split())
    if not clean:
        raise ValueError("Client name is required.")
    if len(clean) > 64:
        raise ValueError("Client name is too long.")
    return clean


@app.route("/")
def index():
    return redirect(url_for("dashboard" if g.user else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if flask_request.method == "POST":
        email = flask_request.form.get("email", "").strip().lower()
        password = flask_request.form.get("password", "")
        name = flask_request.form.get("name", "").strip()
        if not email or len(password) < 8:
            flash("Use a valid email and a password with at least 8 characters.", "error")
            return render_template("register.html")
        try:
            get_db().execute(
                "INSERT INTO users (email, password_hash, name, credits) VALUES (?, ?, ?, 1)",
                (email, generate_password_hash(password), name),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "error")
            return render_template("register.html")
        flash("Account created. You can sign in now.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if flask_request.method == "POST":
        email = flask_request.form.get("email", "").strip().lower()
        password = flask_request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    calls = get_db().execute(
        "SELECT * FROM calls WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (g.user["id"],),
    ).fetchall()
    total = get_db().execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(credits_charged), 0) AS credits FROM calls WHERE user_id = ?",
        (g.user["id"],),
    ).fetchone()
    sip_ready = sip_config_ready()
    return render_template("dashboard.html", calls=calls, total=total, sip_ready=sip_ready)


@app.route("/call", methods=["POST"])
@login_required
def send_call():
    try:
        repeat = 2 if flask_request.form.get("repeat_count") == "2" else 1
        number = flask_request.form.get("number", "")
        mode = flask_request.form.get("audio_mode", "tts")
        if mode == "upload":
            audio_name = create_uploaded_audio(flask_request.files.get("audio_file"))
            result = run_user_call(g.user["id"], number, audio_name, "custom", repeat)
        else:
            audio_id = uuid.uuid4().hex[:12]
            audio_name = create_gtts_audio(flask_request.form.get("message", ""), audio_id)
            result = run_user_call(g.user["id"], number, audio_name, "tts", repeat)
        flash(f"Call completed to {result['number']}. Charged {result['credits_charged']} credit.", "success")
    except Exception as exc:
        get_db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/redeem", methods=["POST"])
@login_required
def redeem():
    try:
        credits = redeem_code(g.user["id"], flask_request.form.get("code", ""))
        flash(f"Redeemed {credits} credits.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    db = get_db()
    if flask_request.method == "POST":
        action = flask_request.form.get("action")
        try:
            if action == "create_code":
                code = create_redeem_code(g.user["id"], int(flask_request.form.get("credits", "0")))
                flash(f"Redeem code created: {code}", "success")
            elif action == "add_user_credits":
                email = flask_request.form.get("email", "").strip().lower()
                credits = int(flask_request.form.get("credits", "0"))
                db.execute("UPDATE users SET credits = credits + ? WHERE email = ?", (credits, email))
                db.commit()
                flash("User credits updated.", "success")
            elif action == "sip_settings":
                for key in ("SIP_DOMAIN", "SIP_PORT", "SIP_USER"):
                    set_setting(key, flask_request.form.get(key, "").strip())
                password = flask_request.form.get("SIP_PASSWORD", "")
                if password:
                    set_setting("SIP_PASSWORD", password)
                db.commit()
                flash("SIP settings saved.", "success")
        except Exception as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("admin"))

    stats = {
        "users": db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"],
        "credits": db.execute("SELECT COALESCE(SUM(credits), 0) AS c FROM users").fetchone()["c"],
        "calls": db.execute("SELECT COUNT(*) AS c FROM calls").fetchone()["c"],
        "used": db.execute("SELECT COALESCE(SUM(credits_charged), 0) AS c FROM calls").fetchone()["c"],
    }
    users = db.execute("SELECT id, email, name, credits, is_admin, created_at FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
    settings = sip_config()
    settings["SIP_PASSWORD_SET"] = bool(settings.pop("SIP_PASSWORD", ""))
    return render_template("admin.html", stats=stats, users=users, settings=settings)


@app.route("/health")
def health():
    return Response("ok\n", mimetype="text/plain")


if __name__ == "__main__":
    init_db()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=True)

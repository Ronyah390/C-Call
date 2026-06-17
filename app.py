# E:\c-call-ivr\app.py
"""Unified C-Call & IVR Calling Web Dashboard."""
from __future__ import annotations

import csv
import json
import math
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import StringIO
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # Must run before any module that reads os.environ at import time

from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)

import call_runner
import db
from audio import (
    RECORDINGS_DIR,
    GEMINI_VOICES,
    create_gtts_audio,
    create_uploaded_audio,
    create_tts_audio,
    list_elevenlabs_voices,
    list_gemini_voices,
    mask_number,
    normalize_phone_number,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# TTS helpers
# ---------------------------------------------------------------------------

def tts_settings() -> dict:
    def setting_or_env(key: str, default: str = "") -> str:
        value = db.get_setting(key, "")
        return value or os.environ.get(key, default)

    return {
        "TTS_PROVIDER": db.get_setting("TTS_PROVIDER", "gtts"),
        "TTS_LANG": db.get_setting("TTS_LANG", "bn"),
        "ELEVENLABS_API_KEY": setting_or_env("ELEVENLABS_API_KEY"),
        "ELEVENLABS_VOICE_ID": db.get_setting("ELEVENLABS_VOICE_ID", ""),
        "ELEVENLABS_MODEL": db.get_setting("ELEVENLABS_MODEL", "eleven_v3"),
        "GEMINI_API_KEY": setting_or_env("GEMINI_API_KEY"),
        "GEMINI_VOICE_NAME": db.get_setting("GEMINI_VOICE_NAME", "Puck"),
        "GEMINI_MODEL": db.get_setting("GEMINI_MODEL", "gemini-2.5-flash-preview-tts"),
    }


def _make_tts(text: str, lang: str | None = None) -> str:
    """Generate TTS audio using the globally configured provider."""
    cfg = tts_settings()
    return create_tts_audio(
        text=text,
        provider=cfg["TTS_PROVIDER"],
        lang=lang or cfg["TTS_LANG"] or "bn",
        voice_id=cfg["ELEVENLABS_VOICE_ID"],
        voice_name=cfg["GEMINI_VOICE_NAME"],
        elevenlabs_api_key=cfg["ELEVENLABS_API_KEY"],
        gemini_api_key=cfg["GEMINI_API_KEY"],
        elevenlabs_model=cfg["ELEVENLABS_MODEL"],
        gemini_model=cfg["GEMINI_MODEL"],
    )


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def parse_bulk_numbers(raw_numbers: str) -> list[str]:
    candidates = [item.strip() for item in re.split(r"[\n,;]+", raw_numbers or "") if item.strip()]
    csv_file = request.files.get("numbers_csv")
    if csv_file and csv_file.filename:
        raw_csv = csv_file.read().decode("utf-8-sig", errors="ignore")
        for row in csv.reader(StringIO(raw_csv)):
            candidates.extend(cell.strip() for cell in row if cell.strip())
    numbers, seen = [], set()
    for candidate in candidates:
        digits = "".join(ch for ch in candidate if ch.isdigit())
        if len(digits) < 10:
            continue
        try:
            number = normalize_phone_number(candidate)
            if number not in seen:
                numbers.append(number)
                seen.add(number)
        except ValueError:
            continue
    if not numbers:
        raise ValueError("At least one phone number is required.")
    return numbers


def build_prompt_audio() -> str:
    """Create a node prompt from upload or TTS text. Returns basename or ''."""
    upload = request.files.get("prompt_file")
    if upload and upload.filename:
        return create_uploaded_audio(upload)
    text = request.form.get("prompt_text", "").strip()
    if text:
        lang = request.form.get("prompt_lang", "").strip() or None
        return _make_tts(text, lang)
    return ""


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
    db.execute(
        "INSERT INTO cdr_records (number, status, duration_seconds, cost, source, cdr_date) VALUES (?, ?, ?, ?, ?, ?)",
        (number, status, duration, cost, source, cdr_date),
    )
    return True


def find_cdr_for_number(number: str) -> dict | None:
    from audio import number_match_key
    key = number_match_key(number)
    if not key:
        return None
    rows = db.query("SELECT * FROM cdr_records ORDER BY COALESCE(cdr_date, created_at) DESC, id DESC")
    for row in rows:
        if number_match_key(row["number"]) == key:
            return row
    return None


def fetch_amarip_cdr(limit: int = 100) -> int:
    settings = cdr_settings()
    if not settings["AMARIP_USERNAME"] or not settings["AMARIP_PASSWORD"]:
        raise ValueError("AmarIP credentials not configured in Settings.")
    base_url = settings["AMARIP_BASE_URL"].rstrip("/")
    body = json.dumps({"username": settings["AMARIP_USERNAME"], "password": settings["AMARIP_PASSWORD"]}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/login", data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            login = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"AmarIP login failed: {exc}")
    token = login.get("token") or login.get("access_token")
    if not token:
        raise RuntimeError("AmarIP login did not return a valid auth token.")
    req_cdr = urllib.request.Request(
        f"{base_url}/api/cdr?page=1&per_page={min(limit, 100)}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req_cdr, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch AmarIP CDRs: {exc}")
    rows = data.get("data", data if isinstance(data, list) else [])
    imported = 0
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and save_cdr_record(row, "amarip"):
                imported += 1
    return imported


def cdr_settings() -> dict[str, str]:
    return {
        "CDR_PROVIDER": db.get_setting("CDR_PROVIDER", "manual"),
        "AMARIP_BASE_URL": db.get_setting("AMARIP_BASE_URL", "https://amarip.net"),
        "AMARIP_USERNAME": db.get_setting("AMARIP_USERNAME"),
        "AMARIP_PASSWORD": db.get_setting("AMARIP_PASSWORD"),
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    stats = db.query_one(
        """SELECT COUNT(*) AS total_calls,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN answered_at IS NOT NULL THEN 1 ELSE 0 END) AS answered,
            COALESCE(SUM(digits_pressed),0) AS total_digits,
            COALESCE(SUM(talk_seconds),0) AS talk_seconds,
            COUNT(DISTINCT contact_id) AS customers
           FROM calls"""
    ) or {}
    recent = db.query(
        "SELECT c.*, f.name AS flow_name FROM calls c LEFT JOIN flows f ON f.id=c.flow_id ORDER BY c.started_at DESC LIMIT 15"
    )
    flows = db.query("SELECT * FROM flows ORDER BY created_at DESC")
    for call in recent:
        call["masked"] = mask_number(call["number"])
        if call["call_type"] == "direct":
            cdr = find_cdr_for_number(call["number"])
            if cdr:
                call["cdr_status"] = cdr["status"]
                call["cdr_duration"] = cdr["duration_seconds"]
                call["cdr_cost"] = cdr["cost"]
    total_seconds = stats.get("talk_seconds") or 0
    stats["talk_minutes"] = math.ceil(total_seconds / 60)
    cfg = tts_settings()
    return render_template(
        "dashboard.html", stats=stats, recent=recent, flows=flows,
        sip_ready=call_runner.sip_ready(), cdr_settings=cdr_settings(),
        tts_provider=cfg["TTS_PROVIDER"],
    )


# ---------------------------------------------------------------------------
# Flow builder
# ---------------------------------------------------------------------------

@app.route("/flows")
def flows():
    rows = db.query(
        "SELECT f.*, (SELECT COUNT(*) FROM flow_nodes n WHERE n.flow_id=f.id) AS node_count FROM flows f ORDER BY f.created_at DESC"
    )
    return render_template("flows.html", flows=rows)


@app.route("/flows/new", methods=["POST"])
def flow_new():
    name = request.form.get("name", "").strip() or "Untitled flow"
    description = request.form.get("description", "").strip()
    flow_id = db.insert_returning_id(
        "INSERT INTO flows (name, description) VALUES (?, ?) RETURNING id",
        (name, description),
    )
    flash("Flow created. Add menu nodes below.", "success")
    return redirect(url_for("flow_edit", flow_id=flow_id))


def _add_demo_node(flow_id: int, name: str, node_type: str, prompt_text: str,
                   *, transfer_number: str = "", timeout: int = 8,
                   retries: int = 2, audio_errors: list[str] | None = None) -> int:
    prompt_audio = ""
    if prompt_text:
        try:
            prompt_audio = create_gtts_audio(prompt_text, "bn")
        except Exception as exc:
            if audio_errors is not None:
                audio_errors.append(f"{name}: {exc}")
    return db.insert_returning_id(
        """
        INSERT INTO flow_nodes
            (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang,
             transfer_number, gather_timeout_seconds, max_retries)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (flow_id, name, node_type, prompt_text, prompt_audio, "bn",
         transfer_number, timeout, retries),
    )


def _link_demo_node(node_id: int, digit: str, next_id: int, label: str) -> None:
    db.execute(
        """
        INSERT INTO flow_transitions (node_id, digit, next_node_id, label)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (node_id, digit) DO UPDATE
            SET next_node_id = excluded.next_node_id, label = excluded.label
        """,
        (node_id, digit, next_id, label),
    )


@app.route("/flows/demo/solar-bangla", methods=["POST"])
def flow_demo_solar_bangla():
    existing = db.query_one("SELECT id FROM flows WHERE name = ?", ("ডেমো সৌর প্যানেল ব্যবসা IVR",))
    if existing:
        flash("Solar panel demo flow already exists. You can edit it now.", "success")
        return redirect(url_for("flow_edit", flow_id=existing["id"]))

    audio_errors: list[str] = []
    flow_id = db.insert_returning_id(
        "INSERT INTO flows (name, description) VALUES (?, ?) RETURNING id",
        (
            "ডেমো সৌর প্যানেল ব্যবসা IVR",
            "বাংলা সোলার প্যানেল ব্যবসার নমুনা IVR: নতুন কোটেশন, প্যাকেজ, সার্ভিস, EMI এবং প্রতিনিধি।",
        ),
    )

    main = _add_demo_node(
        flow_id, "মূল মেনু", "menu",
        "সোলার স্মার্ট বাংলাদেশে আপনাকে স্বাগতম। নতুন সোলার প্যানেল কোটেশনের জন্য ১ চাপুন। "
        "বাসা ও ব্যবসার প্যাকেজ জানতে ২ চাপুন। ইনস্টলেশন বা সার্ভিস সাপোর্টের জন্য ৩ চাপুন। "
        "ই এম আই এবং পেমেন্ট তথ্য জানতে ৪ চাপুন। প্রতিনিধির সাথে কথা বলতে ০ চাপুন।",
        audio_errors=audio_errors,
    )
    quote = _add_demo_node(
        flow_id, "নতুন কোটেশন", "menu",
        "নতুন কোটেশনের জন্য ধন্যবাদ। বাসার সোলার সিস্টেমের জন্য ১ চাপুন। দোকান বা অফিসের জন্য ২ চাপুন। "
        "সেচ পাম্প বা কৃষি ব্যবহারের জন্য ৩ চাপুন। মূল মেনুতে ফিরতে ৯ চাপুন।",
        audio_errors=audio_errors,
    )
    packages = _add_demo_node(
        flow_id, "প্যাকেজ তথ্য", "menu",
        "আমাদের জনপ্রিয় প্যাকেজগুলো শুনুন। এক কিলোওয়াট হোম প্যাকেজ জানতে ১ চাপুন। "
        "তিন কিলোওয়াট ব্যবসা প্যাকেজ জানতে ২ চাপুন। ব্যাটারি ব্যাকআপ প্যাকেজ জানতে ৩ চাপুন। "
        "মূল মেনুতে ফিরতে ৯ চাপুন।",
        audio_errors=audio_errors,
    )
    support = _add_demo_node(
        flow_id, "সার্ভিস সাপোর্ট", "menu",
        "সার্ভিস সাপোর্ট বিভাগে স্বাগতম। ইনভার্টার সমস্যার জন্য ১ চাপুন। ব্যাটারি সমস্যার জন্য ২ চাপুন। "
        "প্যানেল পরিষ্কার বা মেইনটেন্যান্সের জন্য ৩ চাপুন। প্রতিনিধির সাথে কথা বলতে ০ চাপুন। মূল মেনুতে ফিরতে ৯ চাপুন।",
        audio_errors=audio_errors,
    )
    emi = _add_demo_node(
        flow_id, "EMI ও পেমেন্ট", "message",
        "আমাদের সোলার প্যাকেজে সহজ মাসিক কিস্তি সুবিধা আছে। সাধারণত ত্রিশ শতাংশ ডাউন পেমেন্ট দিয়ে কিস্তি শুরু করা যায়। "
        "বিস্তারিত জানতে আমাদের প্রতিনিধি আপনার সাথে যোগাযোগ করবে। ধন্যবাদ।",
        audio_errors=audio_errors,
    )
    agent = _add_demo_node(
        flow_id, "প্রতিনিধির কাছে ট্রান্সফার", "transfer",
        "আপনাকে একজন প্রতিনিধির সাথে সংযুক্ত করা হচ্ছে। অনুগ্রহ করে লাইনে থাকুন।",
        transfer_number="",
        audio_errors=audio_errors,
    )
    home_quote = _add_demo_node(
        flow_id, "বাসার কোটেশন", "message",
        "বাসার জন্য আমরা আপনার মাসিক বিদ্যুৎ বিল, ছাদের জায়গা এবং ব্যাকআপ প্রয়োজন দেখে কোটেশন দিই। "
        "আমাদের প্রতিনিধি শীঘ্রই আপনার সাথে যোগাযোগ করবে। ধন্যবাদ।",
        audio_errors=audio_errors,
    )
    business_quote = _add_demo_node(
        flow_id, "ব্যবসার কোটেশন", "message",
        "দোকান, অফিস বা কারখানার জন্য লোড ক্যালকুলেশন করে অন-গ্রিড অথবা হাইব্রিড সোলার সিস্টেম দেওয়া হয়। "
        "বিস্তারিত আলোচনার জন্য প্রতিনিধি আপনার সাথে যোগাযোগ করবে।",
        audio_errors=audio_errors,
    )
    pump_quote = _add_demo_node(
        flow_id, "সেচ পাম্প কোটেশন", "message",
        "সেচ পাম্পের জন্য পাম্পের হর্স পাওয়ার, দৈনিক চালানোর সময় এবং জমির দূরত্ব অনুযায়ী সিস্টেম ডিজাইন করা হয়। "
        "আমাদের টিম আপনাকে বিস্তারিত জানাবে।",
        audio_errors=audio_errors,
    )
    home_pkg = _add_demo_node(
        flow_id, "১ কিলোওয়াট হোম প্যাকেজ", "message",
        "এক কিলোওয়াট হোম প্যাকেজে প্যানেল, ইনভার্টার, স্ট্রাকচার এবং ইনস্টলেশন অন্তর্ভুক্ত থাকে। "
        "দাম জায়গা ও ব্র্যান্ড অনুযায়ী পরিবর্তন হতে পারে।",
        audio_errors=audio_errors,
    )
    business_pkg = _add_demo_node(
        flow_id, "৩ কিলোওয়াট ব্যবসা প্যাকেজ", "message",
        "তিন কিলোওয়াট ব্যবসা প্যাকেজ দোকান, অফিস এবং ছোট প্রতিষ্ঠানের জন্য উপযোগী। "
        "লোড অনুযায়ী কাস্টম ডিজাইন করা যায়।",
        audio_errors=audio_errors,
    )
    backup_pkg = _add_demo_node(
        flow_id, "ব্যাটারি ব্যাকআপ প্যাকেজ", "message",
        "ব্যাটারি ব্যাকআপ প্যাকেজে হাইব্রিড ইনভার্টার এবং লিথিয়াম অথবা টিউবুলার ব্যাটারি অপশন আছে। "
        "ব্যাকআপ সময় আপনার লোডের উপর নির্ভর করবে।",
        audio_errors=audio_errors,
    )
    inverter_support = _add_demo_node(
        flow_id, "ইনভার্টার সাপোর্ট", "message",
        "ইনভার্টারে অ্যালার্ম বা এরর দেখালে মেইন সুইচ বন্ধ করে পাঁচ মিনিট পরে চালু করুন। সমস্যা থাকলে সার্ভিস টিম যোগাযোগ করবে।",
        audio_errors=audio_errors,
    )
    battery_support = _add_demo_node(
        flow_id, "ব্যাটারি সাপোর্ট", "message",
        "ব্যাটারির ব্যাকআপ কমে গেলে কানেকশন, চার্জিং এবং লোড পরীক্ষা করা প্রয়োজন। আমাদের সার্ভিস টিম আপনাকে সাহায্য করবে।",
        audio_errors=audio_errors,
    )
    cleaning_support = _add_demo_node(
        flow_id, "প্যানেল মেইনটেন্যান্স", "message",
        "সোলার প্যানেল নিয়মিত পরিষ্কার রাখলে উৎপাদন ভালো থাকে। ধুলাবালি বেশি হলে মাসে অন্তত একবার পরিষ্কার করুন।",
        audio_errors=audio_errors,
    )

    _link_demo_node(main, "1", quote, "নতুন কোটেশন")
    _link_demo_node(main, "2", packages, "প্যাকেজ")
    _link_demo_node(main, "3", support, "সাপোর্ট")
    _link_demo_node(main, "4", emi, "EMI")
    _link_demo_node(main, "0", agent, "প্রতিনিধি")
    _link_demo_node(quote, "1", home_quote, "বাসা")
    _link_demo_node(quote, "2", business_quote, "ব্যবসা")
    _link_demo_node(quote, "3", pump_quote, "সেচ পাম্প")
    _link_demo_node(quote, "9", main, "মূল মেনু")
    _link_demo_node(packages, "1", home_pkg, "১ কিলোওয়াট")
    _link_demo_node(packages, "2", business_pkg, "৩ কিলোওয়াট")
    _link_demo_node(packages, "3", backup_pkg, "ব্যাকআপ")
    _link_demo_node(packages, "9", main, "মূল মেনু")
    _link_demo_node(support, "1", inverter_support, "ইনভার্টার")
    _link_demo_node(support, "2", battery_support, "ব্যাটারি")
    _link_demo_node(support, "3", cleaning_support, "মেইনটেন্যান্স")
    _link_demo_node(support, "0", agent, "প্রতিনিধি")
    _link_demo_node(support, "9", main, "মূল মেনু")
    db.execute("UPDATE flows SET root_node_id = ? WHERE id = ?", (main, flow_id))

    if audio_errors:
        flash("Demo flow created, but some prompt audio could not be generated. You can edit/upload audio per node.", "error")
    else:
        flash("Bangla solar panel demo IVR created with ready-to-play prompt audio.", "success")
    return redirect(url_for("flow_edit", flow_id=flow_id))


@app.route("/flows/<int:flow_id>/delete", methods=["POST"])
def flow_delete(flow_id: int):
    flow = db.query_one("SELECT name FROM flows WHERE id = ?", (flow_id,))
    if not flow:
        flash("Flow not found.", "error")
        return redirect(url_for("flows"))
    db.execute("UPDATE calls SET flow_id = NULL WHERE flow_id = ?", (flow_id,))
    db.execute("UPDATE app_settings SET value = '' WHERE key = 'ORDER_CALL_FLOW_ID' AND value = ?", (str(flow_id),))
    db.execute("DELETE FROM flows WHERE id = ?", (flow_id,))
    flash(f"Flow deleted: {flow['name']}", "success")
    return redirect(url_for("flows"))


@app.route("/flows/<int:flow_id>")
def flow_edit(flow_id: int):
    flow = db.query_one("SELECT * FROM flows WHERE id=?", (flow_id,))
    if not flow:
        flash("Flow not found.", "error")
        return redirect(url_for("flows"))
    nodes = db.query("SELECT * FROM flow_nodes WHERE flow_id=? ORDER BY id", (flow_id,))
    transitions = db.query(
        "SELECT t.*, n.name AS next_name FROM flow_transitions t LEFT JOIN flow_nodes n ON n.id=t.next_node_id WHERE t.node_id IN (SELECT id FROM flow_nodes WHERE flow_id=?) ORDER BY t.node_id, t.digit",
        (flow_id,),
    )
    by_node: dict[int, list] = {}
    for t in transitions:
        by_node.setdefault(t["node_id"], []).append(t)
    return render_template("flow_edit.html", flow=flow, nodes=nodes, transitions=by_node)


@app.route("/flows/<int:flow_id>/nodes", methods=["POST"])
def flow_add_node(flow_id: int):
    try:
        prompt_audio = build_prompt_audio()
        node_id = db.insert_returning_id(
            "INSERT INTO flow_nodes (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang, transfer_number, gather_timeout_seconds, max_retries) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                flow_id,
                request.form.get("name", "").strip() or "Node",
                request.form.get("node_type", "menu"),
                request.form.get("prompt_text", "").strip(),
                prompt_audio,
                request.form.get("prompt_lang", "bn").strip() or "bn",
                request.form.get("transfer_number", "").strip(),
                int(request.form.get("gather_timeout_seconds", "8") or 8),
                int(request.form.get("max_retries", "2") or 2),
            ),
        )
        flow = db.query_one("SELECT root_node_id FROM flows WHERE id=?", (flow_id,))
        if flow and not flow["root_node_id"]:
            db.execute("UPDATE flows SET root_node_id=? WHERE id=?", (node_id, flow_id))
        flash("Node added.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("flow_edit", flow_id=flow_id))


@app.route("/flows/<int:flow_id>/nodes/<int:node_id>/delete", methods=["POST"])
def flow_delete_node(flow_id: int, node_id: int):
    db.execute("DELETE FROM flow_nodes WHERE id=? AND flow_id=?", (node_id, flow_id))
    db.execute("UPDATE flows SET root_node_id=NULL WHERE root_node_id=? AND id=?", (node_id, flow_id))
    flash("Node deleted.", "success")
    return redirect(url_for("flow_edit", flow_id=flow_id))


@app.route("/flows/<int:flow_id>/nodes/<int:node_id>/update", methods=["POST"])
def flow_update_node(flow_id: int, node_id: int):
    node = db.query_one("SELECT * FROM flow_nodes WHERE id = ? AND flow_id = ?", (node_id, flow_id))
    if not node:
        flash("Node not found.", "error")
        return redirect(url_for("flow_edit", flow_id=flow_id))

    try:
        node_type = request.form.get("node_type", "menu").strip() or "menu"
        if node_type not in {"menu", "message", "transfer", "hangup"}:
            raise ValueError("Invalid node type.")

        prompt_text = request.form.get("prompt_text", "").strip()
        prompt_lang = request.form.get("prompt_lang", "bn").strip() or "bn"
        prompt_audio = node["prompt_audio"] or ""
        upload = request.files.get("prompt_file")
        regenerate = request.form.get("regenerate_audio") == "1"
        clear_audio = request.form.get("clear_audio") == "1"

        if clear_audio:
            prompt_audio = ""
        elif upload and upload.filename:
            prompt_audio = create_uploaded_audio(upload)
        elif regenerate and prompt_text:
            prompt_audio = create_tts_audio(
                text=prompt_text,
                provider=tts_settings()["TTS_PROVIDER"],
                lang=prompt_lang,
                voice_id=tts_settings()["ELEVENLABS_VOICE_ID"],
                voice_name=tts_settings()["GEMINI_VOICE_NAME"],
                elevenlabs_api_key=tts_settings()["ELEVENLABS_API_KEY"],
                gemini_api_key=tts_settings()["GEMINI_API_KEY"],
                elevenlabs_model=tts_settings()["ELEVENLABS_MODEL"],
                gemini_model=tts_settings()["GEMINI_MODEL"],
            )

        if node_type == "hangup":
            prompt_text = ""
            prompt_audio = ""

        db.execute(
            """
            UPDATE flow_nodes
               SET name = ?, node_type = ?, prompt_text = ?, prompt_audio = ?,
                   prompt_lang = ?, transfer_number = ?,
                   gather_timeout_seconds = ?, max_retries = ?
             WHERE id = ? AND flow_id = ?
            """,
            (
                request.form.get("name", "").strip() or "Node",
                node_type,
                prompt_text,
                prompt_audio,
                prompt_lang,
                request.form.get("transfer_number", "").strip(),
                int(request.form.get("gather_timeout_seconds", "8") or 8),
                int(request.form.get("max_retries", "2") or 2),
                node_id,
                flow_id,
            ),
        )
        flash("Node updated.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("flow_edit", flow_id=flow_id))


@app.route("/flows/<int:flow_id>/set-root", methods=["POST"])
def flow_set_root(flow_id: int):
    node_id = int(request.form.get("root_node_id"))
    db.execute("UPDATE flows SET root_node_id=? WHERE id=?", (node_id, flow_id))
    flash("Start node updated.", "success")
    return redirect(url_for("flow_edit", flow_id=flow_id))


@app.route("/flows/<int:flow_id>/transitions", methods=["POST"])
def flow_add_transition(flow_id: int):
    try:
        node_id = int(request.form.get("node_id"))
        next_node_id = int(request.form.get("next_node_id"))
        digit = request.form.get("digit", "").strip()
        if digit not in [str(d) for d in range(10)] + ["*", "#"]:
            raise ValueError("Digit must be 0-9, * or #.")
        db.execute(
            "INSERT INTO flow_transitions (node_id, digit, next_node_id, label) VALUES (?, ?, ?, ?) ON CONFLICT (node_id, digit) DO UPDATE SET next_node_id=excluded.next_node_id, label=excluded.label",
            (node_id, digit, next_node_id, request.form.get("label", "").strip()),
        )
        flash(f"Keypress {digit} mapped successfully.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("flow_edit", flow_id=flow_id))


@app.route("/flows/<int:flow_id>/transitions/<int:tid>/delete", methods=["POST"])
def flow_delete_transition(flow_id: int, tid: int):
    db.execute("DELETE FROM flow_transitions WHERE id=?", (tid,))
    return redirect(url_for("flow_edit", flow_id=flow_id))


# ---------------------------------------------------------------------------
# Call dispatch
# ---------------------------------------------------------------------------

@app.route("/call", methods=["POST"])
def send_call():
    try:
        number = request.form.get("number", "")
        name = request.form.get("name", "").strip()
        call_mode = request.form.get("call_mode", "ivr")
        flow_id_raw = request.form.get("flow_id")
        if call_mode == "ivr" and flow_id_raw:
            flow_id = int(flow_id_raw)
            call_id = call_runner.start_call(number, flow_id, name=name)
            flash(f"Call queued to {mask_number(number)}.", "success")
            return redirect(url_for("call_detail", call_id=call_id))
        else:
            repeat = int(request.form.get("repeat_count", "1"))
            audio_mode = request.form.get("audio_mode", "tts")
            message = request.form.get("message", "").strip()
            if audio_mode == "upload":
                audio_file = request.files.get("audio_file")
                audio_name = create_uploaded_audio(audio_file)
            else:
                audio_name = _make_tts(message)
            call_id = call_runner.start_direct_call(number, audio_name, repeat, message, audio_mode, name=name)
            flash(f"Direct voice call queued to {mask_number(number)}.", "success")
            return redirect(url_for("call_detail", call_id=call_id))
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/bulk-call", methods=["POST"])
def send_bulk_call():
    try:
        numbers = parse_bulk_numbers(request.form.get("numbers", ""))
        call_mode = request.form.get("call_mode", "ivr")
        flow_id_raw = request.form.get("flow_id")
        if call_mode == "ivr" and flow_id_raw:
            ids = call_runner.start_bulk(numbers, int(flow_id_raw))
            flash(f"Queued {len(ids)} bulk IVR calls.", "success")
        else:
            repeat = int(request.form.get("repeat_count", "1"))
            audio_mode = request.form.get("audio_mode", "tts")
            message = request.form.get("message", "").strip()
            if audio_mode == "upload":
                audio_file = request.files.get("audio_file")
                audio_name = create_uploaded_audio(audio_file)
            else:
                audio_name = _make_tts(message)
            ids = call_runner.start_direct_bulk(numbers, audio_name, repeat, message, audio_mode)
            flash(f"Queued {len(ids)} bulk direct calls.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Call journeys
# ---------------------------------------------------------------------------

@app.route("/calls/<int:call_id>")
def call_detail(call_id: int):
    call = db.query_one(
        "SELECT c.*, f.name AS flow_name, ct.name AS contact_name FROM calls c LEFT JOIN flows f ON f.id=c.flow_id LEFT JOIN contacts ct ON ct.id=c.contact_id WHERE c.id=?",
        (call_id,),
    )
    if not call:
        flash("Call not found.", "error")
        return redirect(url_for("dashboard"))
    events = db.query(
        "SELECT e.*, n.name AS node_name FROM call_events e LEFT JOIN flow_nodes n ON n.id=e.node_id WHERE e.call_id=? ORDER BY e.seq",
        (call_id,),
    )
    call["masked"] = mask_number(call["number"])
    recording = Path(call["recording_path"]).name if call["recording_path"] else ""
    if call["call_type"] == "direct":
        cdr = find_cdr_for_number(call["number"])
        if cdr:
            call["cdr_status"] = cdr["status"]
            call["cdr_duration"] = cdr["duration_seconds"]
            call["cdr_cost"] = cdr["cost"]
    return render_template("call_detail.html", call=call, events=events, recording=recording)


# ---------------------------------------------------------------------------
# Real-time polling API
# ---------------------------------------------------------------------------

_TERMINAL = {"completed", "failed", "no_answer", "busy", "canceled"}


def _call_json(call_id: int) -> dict | None:
    call = db.query_one(
        "SELECT c.*, f.name AS flow_name, ct.name AS contact_name FROM calls c LEFT JOIN flows f ON f.id=c.flow_id LEFT JOIN contacts ct ON ct.id=c.contact_id WHERE c.id=?",
        (call_id,),
    )
    if not call:
        return None
    events = db.query(
        "SELECT e.seq, e.event_type, e.digit, e.response_ms, e.detail, e.at_offset_ms, n.name AS node_name FROM call_events e LEFT JOIN flow_nodes n ON n.id=e.node_id WHERE e.call_id=? ORDER BY e.seq",
        (call_id,),
    )
    recording = Path(call["recording_path"]).name if call["recording_path"] else ""
    return {
        "id": call["id"],
        "number": mask_number(call["number"]),
        "contact_name": call["contact_name"],
        "flow_name": call["flow_name"] or ("One-way Call" if call["call_type"] == "direct" else "—"),
        "status": call["status"],
        "live": call["status"] not in _TERMINAL,
        "sip_final_status": call["sip_final_status"],
        "ring_seconds": call["ring_seconds"],
        "talk_seconds": call["talk_seconds"],
        "digits_pressed": call["digits_pressed"],
        "reached_terminal": bool(call["reached_terminal"]),
        "error": call["error"],
        "recording": recording,
        "events": [dict(e) for e in events],
    }


@app.route("/api/calls/<int:call_id>")
def api_call(call_id: int):
    data = _call_json(call_id)
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/active")
def api_active():
    rows = db.query(
        "SELECT c.id, c.number, c.status, c.digits_pressed, c.talk_seconds, c.call_type, f.name AS flow_name FROM calls c LEFT JOIN flows f ON f.id=c.flow_id ORDER BY c.started_at DESC LIMIT 25"
    )
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "number": mask_number(r["number"]),
            "status": r["status"],
            "live": r["status"] not in _TERMINAL,
            "digits_pressed": r["digits_pressed"],
            "talk_seconds": r["talk_seconds"],
            "flow_name": r["flow_name"] or ("One-way Call" if r["call_type"] == "direct" else "—"),
        })
    return jsonify({"calls": out})


@app.route("/api/calls/<int:call_id>/hangup", methods=["POST"])
def api_hangup(call_id: int):
    canceled = call_runner.cancel_call(call_id)
    return jsonify({"canceled": canceled})


# ---------------------------------------------------------------------------
# Voice list APIs
# ---------------------------------------------------------------------------

def _req_param(name: str, default: str = "") -> str:
    """Read a param from JSON body, form, or query string (in that order)."""
    body = request.get_json(silent=True) or {}
    val = body.get(name) if isinstance(body, dict) else None
    if val is None:
        val = request.values.get(name)
    return (val or default).strip()


@app.route("/api/voices/elevenlabs", methods=["GET", "POST"])
def api_voices_elevenlabs():
    # Use the key typed in the form (if any) so it can be tested before saving.
    api_key = _req_param("api_key") or tts_settings()["ELEVENLABS_API_KEY"]
    if not api_key:
        return jsonify({"error": "ElevenLabs API key not configured. Enter it above first."}), 400
    try:
        voices = list_elevenlabs_voices(api_key)
        return jsonify({"voices": voices, "count": len(voices)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/voices/gemini")
def api_voices_gemini():
    return jsonify({"voices": list_gemini_voices()})


@app.route("/api/test/elevenlabs", methods=["POST"])
def api_test_elevenlabs():
    """Validate the ElevenLabs API key by listing voices."""
    api_key = _req_param("api_key") or tts_settings()["ELEVENLABS_API_KEY"]
    if not api_key:
        return jsonify({"ok": False, "error": "Enter an ElevenLabs API key first."}), 400
    try:
        voices = list_elevenlabs_voices(api_key)
        return jsonify({
            "ok": True,
            "message": f"Connected — {len(voices)} voices available.",
            "voices": voices,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/test/gemini", methods=["POST"])
def api_test_gemini():
    """Validate the Gemini API key with a tiny TTS request."""
    from audio import create_gemini_audio
    cfg = tts_settings()
    api_key = _req_param("api_key") or cfg["GEMINI_API_KEY"]
    if not api_key:
        return jsonify({"ok": False, "error": "Enter a Gemini API key first."}), 400
    voice = _req_param("voice_name") or cfg["GEMINI_VOICE_NAME"] or "Puck"
    model = _req_param("model") or cfg["GEMINI_MODEL"] or "gemini-2.5-flash-preview-tts"
    try:
        create_gemini_audio("This is a test.", voice, api_key, model_id=model)
        return jsonify({"ok": True, "message": f"Connected — Gemini TTS works (voice: {voice})."})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/tts/preview", methods=["POST"])
def api_tts_preview():
    """Generate a short voice sample and return it as playable WAV audio."""
    from audio import create_elevenlabs_audio, create_gemini_audio, ulaw_to_wav_bytes
    provider = _req_param("provider", "gtts")
    text = _req_param("text") or "Hello, this is a C-Call IVR voice test. One, two, three."
    cfg = tts_settings()
    try:
        if provider == "elevenlabs":
            key = _req_param("api_key") or cfg["ELEVENLABS_API_KEY"]
            voice_id = _req_param("voice_id") or cfg["ELEVENLABS_VOICE_ID"]
            model = _req_param("model") or cfg["ELEVENLABS_MODEL"] or "eleven_v3"
            if not key:
                return jsonify({"error": "Enter an ElevenLabs API key first."}), 400
            if not voice_id:
                return jsonify({"error": "Select a voice first (use Fetch Voices)."}), 400
            name = create_elevenlabs_audio(text, voice_id, key, model_id=model)
        elif provider == "gemini":
            key = _req_param("api_key") or cfg["GEMINI_API_KEY"]
            voice_name = _req_param("voice_name") or cfg["GEMINI_VOICE_NAME"] or "Puck"
            model = _req_param("model") or cfg["GEMINI_MODEL"] or "gemini-2.5-flash-preview-tts"
            if not key:
                return jsonify({"error": "Enter a Gemini API key first."}), 400
            name = create_gemini_audio(text, voice_name, key, model_id=model)
        else:
            lang = _req_param("lang") or cfg["TTS_LANG"] or "bn"
            name = create_gtts_audio(text, lang)
        return Response(ulaw_to_wav_bytes(name), mimetype="audio/wav")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Ecommerce / Order Confirmation API
# ---------------------------------------------------------------------------

@app.route("/api/v1/order-call", methods=["POST"])
def api_order_call():
    """Public REST endpoint for ecommerce order confirmation calls.

    Accepts JSON body:
      { "api_key": "...", "phone": "+880...", "order_id": "ORD123",
        "customer_name": "Jane", "total_amount": "1200.00",
        "items": "2x Widget", "message": "optional override" }

    Or pass api_key in the X-API-Key header.
    """
    data = request.get_json(silent=True) or {}
    req_key = request.headers.get("X-API-Key") or data.get("api_key", "")
    stored_key = db.get_setting("ECOMMERCE_API_KEY", "")
    if not stored_key or req_key != stored_key:
        return jsonify({"success": False, "error": "Unauthorized — invalid or missing API key"}), 401

    phone = str(data.get("phone", "")).strip()
    order_id = str(data.get("order_id", "")).strip()
    customer_name = str(data.get("customer_name", "")).strip()
    total_amount = str(data.get("total_amount", "")).strip()
    items = str(data.get("items", "")).strip()
    custom_message = str(data.get("message", "")).strip()

    if not phone:
        return jsonify({"success": False, "error": "phone is required"}), 400

    template = db.get_setting(
        "ORDER_MESSAGE_TEMPLATE",
        "Hello {customer_name}, your order {order_id} worth {total_amount} has been confirmed. Thank you for shopping with us!",
    )
    try:
        message = custom_message or template.format(
            customer_name=customer_name or "Customer",
            order_id=order_id or "N/A",
            total_amount=total_amount or "N/A",
            items=items or "",
        )
    except KeyError:
        message = custom_message or template

    order_flow_id = db.get_setting("ORDER_CALL_FLOW_ID", "")
    try:
        if order_flow_id:
            call_id = call_runner.start_call(phone, int(order_flow_id), name=customer_name)
        else:
            audio_name = _make_tts(message)
            call_id = call_runner.start_direct_call(
                phone, audio_name, 1, message, "tts", name=customer_name
            )
        return jsonify({"success": True, "call_id": call_id,
                        "message": "Order confirmation call initiated"})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/v1/order-call/test", methods=["POST"])
def api_order_call_test():
    """Regenerate the ecommerce API key."""
    new_key = secrets.token_urlsafe(32)
    db.set_setting("ECOMMERCE_API_KEY", new_key)
    return jsonify({"api_key": new_key})


@app.route("/ecommerce")
def ecommerce():
    api_key = db.get_setting("ECOMMERCE_API_KEY", "")
    if not api_key:
        api_key = secrets.token_urlsafe(32)
        db.set_setting("ECOMMERCE_API_KEY", api_key)
    template = db.get_setting(
        "ORDER_MESSAGE_TEMPLATE",
        "Hello {customer_name}, your order {order_id} worth {total_amount} has been confirmed. Thank you for shopping with us!",
    )
    order_flow_id = db.get_setting("ORDER_CALL_FLOW_ID", "")
    flows = db.query("SELECT id, name FROM flows WHERE is_active = true ORDER BY name")
    cfg = tts_settings()
    return render_template(
        "ecommerce.html",
        api_key=api_key,
        order_template=template,
        order_flow_id=order_flow_id,
        flows=flows,
        sip_ready=call_runner.sip_ready(),
        tts_provider=cfg["TTS_PROVIDER"],
    )


@app.route("/ecommerce/save", methods=["POST"])
def ecommerce_save():
    db.set_setting("ORDER_MESSAGE_TEMPLATE", request.form.get("ORDER_MESSAGE_TEMPLATE", "").strip())
    db.set_setting("ORDER_CALL_FLOW_ID", request.form.get("ORDER_CALL_FLOW_ID", "").strip())
    if request.form.get("regenerate_key"):
        new_key = secrets.token_urlsafe(32)
        db.set_setting("ECOMMERCE_API_KEY", new_key)
        flash("New API key generated.", "success")
    else:
        flash("Ecommerce settings saved.", "success")
    return redirect(url_for("ecommerce"))


@app.route("/ecommerce/test", methods=["POST"])
def ecommerce_test():
    api_key = db.get_setting("ECOMMERCE_API_KEY", "")
    payload = {
        "api_key": api_key,
        "phone": request.form.get("phone", "").strip(),
        "order_id": request.form.get("order_id", "TEST-ORDER").strip() or "TEST-ORDER",
        "customer_name": request.form.get("customer_name", "Test Customer").strip() or "Test Customer",
        "total_amount": request.form.get("total_amount", "500 BDT").strip() or "500 BDT",
        "items": request.form.get("items", "Test item").strip() or "Test item",
        "message": request.form.get("message", "").strip(),
    }
    if not payload["phone"]:
        flash("Enter a phone number for the ecommerce test call.", "error")
        return redirect(url_for("ecommerce"))
    with app.test_request_context("/api/v1/order-call", method="POST", json=payload):
        response = api_order_call()
    data = response[0].get_json() if isinstance(response, tuple) else response.get_json()
    status = response[1] if isinstance(response, tuple) and len(response) > 1 else 200
    if status >= 400 or not data.get("success"):
        flash(data.get("error", "Ecommerce test failed."), "error")
    else:
        flash(f"Ecommerce test call queued. Call ID: {data.get('call_id')}", "success")
    return redirect(url_for("ecommerce"))


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@app.route("/analytics")
def analytics():
    outcomes = db.query("SELECT status, COUNT(*) AS n FROM calls GROUP BY status ORDER BY n DESC")
    totals = db.query_one(
        "SELECT COUNT(*) AS calls, SUM(CASE WHEN answered_at IS NOT NULL THEN 1 ELSE 0 END) AS answered, CAST(COALESCE(AVG(CASE WHEN talk_seconds>0 THEN talk_seconds ELSE NULL END),0) AS INTEGER) AS avg_talk, COALESCE(SUM(digits_pressed),0) AS digits FROM calls"
    ) or {}
    node_stats = db.query(
        "SELECT n.id, n.name, n.node_type, COUNT(DISTINCT e.call_id) AS reached, SUM(CASE WHEN e.event_type='dtmf' THEN 1 ELSE 0 END) AS keypresses, CAST(COALESCE(AVG(CASE WHEN e.response_ms>0 THEN e.response_ms ELSE NULL END),0) AS INTEGER) AS avg_response_ms, (SELECT COUNT(*) FROM calls c WHERE c.drop_node_id=n.id) AS dropoffs FROM flow_nodes n LEFT JOIN call_events e ON e.node_id=n.id GROUP BY n.id, n.name, n.node_type HAVING COUNT(DISTINCT e.call_id)>0 ORDER BY reached DESC"
    )
    digit_stats = db.query("SELECT digit, COUNT(*) AS n FROM call_events WHERE event_type='dtmf' AND digit<>'' GROUP BY digit ORDER BY n DESC")
    top_customers = db.query(
        "SELECT ct.id, ct.number, ct.name, COUNT(c.id) AS calls, COALESCE(SUM(c.digits_pressed),0) AS digits, MAX(c.started_at) AS last_call FROM contacts ct JOIN calls c ON c.contact_id=ct.id GROUP BY ct.id, ct.number, ct.name ORDER BY calls DESC, digits DESC LIMIT 20"
    )
    for c in top_customers:
        c["masked"] = mask_number(c["number"])
    return render_template(
        "analytics.html", outcomes=outcomes, totals=totals, node_stats=node_stats,
        digit_stats=digit_stats, top_customers=top_customers,
    )


@app.route("/contacts/<int:contact_id>")
def contact_detail(contact_id: int):
    contact = db.query_one("SELECT * FROM contacts WHERE id=?", (contact_id,))
    if not contact:
        flash("Customer not found.", "error")
        return redirect(url_for("analytics"))
    calls = db.query(
        "SELECT c.*, f.name AS flow_name FROM calls c LEFT JOIN flows f ON f.id=c.flow_id WHERE c.contact_id=? ORDER BY c.started_at DESC",
        (contact_id,),
    )
    contact["masked"] = mask_number(contact["number"])
    return render_template("contact_detail.html", contact=contact, calls=calls)


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------

@app.route("/recordings/<path:filename>")
def recording_file(filename: str):
    return send_from_directory(RECORDINGS_DIR, filename, as_attachment=False)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        try:
            # SIP
            for key in ("SIP_DOMAIN", "SIP_PORT", "SIP_USER", "ASTERISK_DIAL_FORMAT"):
                db.set_setting(key, request.form.get(key, "").strip())
            if request.form.get("SIP_PASSWORD"):
                db.set_setting("SIP_PASSWORD", request.form.get("SIP_PASSWORD"))

            # CDR
            for key in ("CDR_PROVIDER", "AMARIP_BASE_URL", "AMARIP_USERNAME"):
                db.set_setting(key, request.form.get(key, "").strip())
            if request.form.get("AMARIP_PASSWORD"):
                db.set_setting("AMARIP_PASSWORD", request.form.get("AMARIP_PASSWORD"))

            # TTS providers
            for key in ("TTS_PROVIDER", "TTS_LANG",
                        "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL",
                        "GEMINI_VOICE_NAME", "GEMINI_MODEL"):
                db.set_setting(key, request.form.get(key, "").strip())
            if request.form.get("ELEVENLABS_API_KEY"):
                db.set_setting("ELEVENLABS_API_KEY", request.form.get("ELEVENLABS_API_KEY"))
            if request.form.get("GEMINI_API_KEY"):
                db.set_setting("GEMINI_API_KEY", request.form.get("GEMINI_API_KEY"))

            flash("Settings saved successfully.", "success")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings"))

    config = {
        "SIP_DOMAIN": db.get_setting("SIP_DOMAIN"),
        "SIP_PORT": db.get_setting("SIP_PORT", "5060"),
        "SIP_USER": db.get_setting("SIP_USER"),
        "ASTERISK_DIAL_FORMAT": db.get_setting("ASTERISK_DIAL_FORMAT", "local_bd"),
    }
    password_set = bool(db.get_setting("SIP_PASSWORD"))
    cdr = cdr_settings()
    amarip_password_set = bool(cdr.get("AMARIP_PASSWORD"))
    cdr.pop("AMARIP_PASSWORD", None)
    tts = tts_settings()
    el_key_set = bool(tts.get("ELEVENLABS_API_KEY"))
    gemini_key_set = bool(tts.get("GEMINI_API_KEY"))

    return render_template(
        "settings.html",
        settings=config,
        password_set=password_set,
        dial_format=config["ASTERISK_DIAL_FORMAT"],
        cdr_settings=cdr,
        amarip_password_set=amarip_password_set,
        tts=tts,
        el_key_set=el_key_set,
        gemini_key_set=gemini_key_set,
        gemini_voices=GEMINI_VOICES,
        sip_ready=call_runner.sip_ready(),
    )


# ---------------------------------------------------------------------------
# CDR import
# ---------------------------------------------------------------------------

@app.route("/cdr/import", methods=["POST"])
def import_cdr():
    try:
        file_storage = request.files.get("cdr_csv")
        if not file_storage or not file_storage.filename:
            raise ValueError("CSV file is required for CDR import.")
        raw_csv = file_storage.read().decode("utf-8-sig", errors="ignore")
        imported = sum(1 for row in csv.DictReader(StringIO(raw_csv)) if save_cdr_record(row, "manual_csv"))
        if imported == 0:
            raise ValueError("No records imported. Check the CSV headers.")
        flash(f"Imported {imported} CDR records.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/cdr/fetch-amarip", methods=["POST"])
def fetch_cdr_amarip_route():
    try:
        imported = fetch_amarip_cdr()
        flash(f"Successfully fetched {imported} CDR records from AmarIP.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Other pages
# ---------------------------------------------------------------------------

@app.route("/bangla-guide")
def bangla_guide():
    return render_template("bangla_guide.html", sip_ready=call_runner.sip_ready())


@app.route("/health")
def health():
    return Response("ok\n", mimetype="text/plain")


if __name__ == "__main__":
    db.init_db()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
    )

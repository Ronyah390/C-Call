# E:\c-call-ivr\call_runner.py
"""Unified Call Orchestrator.

Loads IVR flows or direct one-way configurations, executes calls using
either the in-process `ivr_engine.IvrCall` (for IVR) or `direct_sip_call.py`
as a subprocess (for direct calls), and logs outcomes/events to SQLite.
"""
from __future__ import annotations

import os
import sys
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import db
from audio import format_dial_number, normalize_phone_number, ulaw_path_for, audio_duration_seconds
from ivr_engine import Event, Flow, IvrCall, Node

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"

# Background worker threads
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("CALL_WORKERS", "4")))
_lock = threading.Lock()

# Currently active IVR calls (by database call_id) to allow live hang-up
_active_ivr_calls: dict[int, IvrCall] = {}


def cancel_call(call_id: int) -> bool:
    """Request a live IVR call to hang up. Returns True if the call was active."""
    call = _active_ivr_calls.get(call_id)
    if call is None:
        return False
    call.request_cancel()
    return True


def active_call_ids() -> list[int]:
    """Return list of active live call IDs."""
    return list(_active_ivr_calls.keys())


# --- Settings & SIP helpers ---

def sip_config() -> dict[str, str]:
    return {
        "SIP_DOMAIN": db.get_setting("SIP_DOMAIN"),
        "SIP_PORT": db.get_setting("SIP_PORT", "5060"),
        "SIP_USER": db.get_setting("SIP_USER"),
        "SIP_PASSWORD": db.get_setting("SIP_PASSWORD"),
    }


def sip_ready() -> bool:
    cfg = sip_config()
    return all(cfg.get(k) for k in ("SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD"))


# --- Contacts / Customers ---

def upsert_contact(number: str, name: str = "") -> int:
    """Retrieve existing contact ID or insert a new contact, returning the ID."""
    row = db.query_one("SELECT id, name FROM contacts WHERE number = ?", (number,))
    if row:
        contact_id = row["id"]
        if name and name != row["name"]:
            db.execute("UPDATE contacts SET name = ? WHERE id = ?", (name, contact_id))
        return contact_id
    return db.insert_returning_id(
        "INSERT INTO contacts (number, name) VALUES (?, ?)",
        (number, name),
    )


def _attempt_no(contact_id: int) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM calls WHERE contact_id = ?", (contact_id,)
    )
    return int(row["n"]) + 1 if row else 1


# --- IVR Flow Loader ---

def load_flow(flow_id: int) -> Flow:
    flow_row = db.query_one("SELECT * FROM flows WHERE id = ?", (flow_id,))
    if not flow_row:
        raise ValueError(f"Flow {flow_id} not found")
    node_rows = db.query("SELECT * FROM flow_nodes WHERE flow_id = ?", (flow_id,))
    trans_rows = db.query(
        "SELECT * FROM flow_transitions WHERE node_id IN "
        "(SELECT id FROM flow_nodes WHERE flow_id = ?)",
        (flow_id,),
    )
    nodes: dict[int, Node] = {}
    for r in node_rows:
        transfer_number = r["transfer_number"]
        if transfer_number and not transfer_number.lower().startswith("sip:"):
            transfer_number = format_dial_number(
                transfer_number,
                db.get_setting("ASTERISK_DIAL_FORMAT", "local_bd"),
            )
        nodes[r["id"]] = Node(
            id=r["id"], name=r["name"], node_type=r["node_type"],
            prompt_audio=r["prompt_audio"], transfer_number=transfer_number,
            gather_timeout_seconds=r["gather_timeout_seconds"],
            max_retries=r["max_retries"],
        )
    for t in trans_rows:
        if t["node_id"] in nodes and t["next_node_id"]:
            nodes[t["node_id"]].transitions[t["digit"]] = t["next_node_id"]

    root = flow_row["root_node_id"] or (node_rows[0]["id"] if node_rows else None)
    if root is None:
        raise ValueError("Flow has no nodes")
    return Flow(id=flow_id, root_node_id=root, nodes=nodes)


# --- Background Workers ---

def _persist_event(call_id: int, ev: Event) -> None:
    db.execute(
        """
        INSERT INTO call_events
            (call_id, seq, event_type, node_id, digit, response_ms, detail, at_offset_ms)
        VALUES (?,
                (SELECT COALESCE(MAX(seq), 0) + 1 FROM call_events WHERE call_id = ?),
                ?, ?, ?, ?, ?, ?)
        """,
        (call_id, call_id, ev.type, ev.node_id, ev.digit, ev.response_ms,
         ev.detail, ev.at_offset_ms),
    )


def _run_ivr_thread(call_id: int, target_dial: str, flow: Flow, sip: dict[str, str]) -> None:
    """Runs a single interactive IVR call in a background thread."""
    def on_event(ev: Event) -> None:
        try:
            _persist_event(call_id, ev)
            if ev.type in ("ringing", "answered"):
                db.execute(
                    "UPDATE calls SET status = ?, "
                    "answered_at = CASE WHEN ? = 'answered' THEN now() ELSE answered_at END "
                    "WHERE id = ?",
                    (ev.type, ev.type, call_id),
                )
        except Exception:
            pass

    record = os.environ.get("RECORD_CALLS", "1") == "1"
    call = IvrCall(target_dial, flow, sip, on_event, record=record)
    
    # Register call globally to enable UI cancellations
    _active_ivr_calls[call_id] = call
    try:
        result = call.run()
    finally:
        _active_ivr_calls.pop(call_id, None)
        
    db.execute(
        """
        UPDATE calls SET
            status = ?, sip_final_status = ?, ring_seconds = ?, talk_seconds = ?,
            total_seconds = ?, digits_pressed = ?, drop_node_id = ?, last_node_id = ?,
            reached_terminal = ?, recording_path = ?, error = ?, ended_at = now()
        WHERE id = ?
        """,
        (result.status, result.sip_final_status, result.ring_seconds, result.talk_seconds,
         result.total_seconds, result.digits_pressed, result.drop_node_id, result.last_node_id,
         1 if result.reached_terminal else 0, result.recording_path, result.error, call_id),
    )


def _run_direct_subprocess(call_id: int, dial_target: str, audio_path: str, repeat_count: int, max_seconds: int, sip_cfg: dict) -> None:
    """Spawns direct_sip_call.py in a background thread to prevent UI blocks."""
    env = os.environ.copy()
    env.update(sip_cfg)
    
    db.execute("UPDATE calls SET status = 'ringing' WHERE id = ?", (call_id,))
    
    log_file_path = INSTANCE_DIR / "direct-sip.log"
    start_time = time.monotonic()
    
    try:
        with log_file_path.open("ab", buffering=0) as log_f:
            result = subprocess.run(
                [
                    sys.executable,
                    str(BASE_DIR / "direct_sip_call.py"),
                    dial_target,
                    str(audio_path),
                    str(repeat_count),
                    str(max_seconds)
                ],
                env=env,
                stdout=log_f,
                stderr=log_f,
                timeout=max_seconds + int(os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90")) + 30,
                check=False
            )
            
        elapsed = int(time.monotonic() - start_time)
        if result.returncode == 0:
            db.execute(
                """
                UPDATE calls SET
                    status = 'completed',
                    talk_seconds = ?,
                    total_seconds = ?,
                    ended_at = now()
                WHERE id = ?
                """,
                (max_seconds, elapsed, call_id)
            )
        else:
            db.execute(
                """
                UPDATE calls SET
                    status = 'failed',
                    total_seconds = ?,
                    ended_at = now(),
                    error = 'Call failed or was not answered.'
                WHERE id = ?
                """,
                (elapsed, call_id)
            )
    except subprocess.TimeoutExpired:
        elapsed = int(time.monotonic() - start_time)
        db.execute(
            """
            UPDATE calls SET
                status = 'no_answer',
                total_seconds = ?,
                ended_at = now(),
                error = 'Call timed out (no answer).'
            WHERE id = ?
            """,
            (elapsed, call_id)
        )
    except Exception as exc:
        elapsed = int(time.monotonic() - start_time)
        db.execute(
            """
            UPDATE calls SET
                status = 'failed',
                total_seconds = ?,
                ended_at = now(),
                error = ?
            WHERE id = ?
            """,
            (elapsed, str(exc), call_id)
        )


# --- Dispatch methods ---

def start_call(number: str, flow_id: int, *, name: str = "") -> int:
    """Create the database records and dispatch IVR call to background thread pool."""
    if not sip_ready():
        raise RuntimeError("SIP credentials not configured. Open Settings first.")
        
    normalized = normalize_phone_number(number)
    flow = load_flow(flow_id)
    sip = sip_config()
    dial_target = format_dial_number(normalized, db.get_setting("ASTERISK_DIAL_FORMAT", "local_bd"))

    with _lock:
        contact_id = upsert_contact(normalized, name)
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls (contact_id, flow_id, number, status, attempt_no, call_type)
            VALUES (?, ?, ?, 'initiated', ?, 'ivr')
            """,
            (contact_id, flow_id, normalized, _attempt_no(contact_id)),
        )
        
    _executor.submit(_run_ivr_thread, call_id, dial_target, flow, sip)
    return call_id


def start_bulk(numbers: list[str], flow_id: int) -> list[int]:
    """Launch multiple IVR calls in parallel."""
    return [start_call(number, flow_id) for number in numbers]


def start_direct_call(number: str, audio_name: str, repeat_count: int, message_text: str = "", audio_mode: str = "tts", *, name: str = "") -> int:
    """Create the database records and dispatch Direct call to background thread pool."""
    if not sip_ready():
        raise RuntimeError("SIP credentials not configured. Open Settings first.")
        
    normalized = normalize_phone_number(number)
    audio_path = INSTANCE_DIR / "sounds" / f"{audio_name}.ulaw"
    if not audio_path.exists():
        raise RuntimeError("Call audio file is missing.")
        
    sip = sip_config()
    dial_target = format_dial_number(normalized, db.get_setting("ASTERISK_DIAL_FORMAT", "local_bd"))
    
    # Calculate duration
    audio_secs = audio_duration_seconds(audio_name)
    repeat_val = 2 if int(repeat_count) > 1 else 1
    max_call_secs = audio_secs * repeat_val + max(0, repeat_val - 1)

    with _lock:
        contact_id = upsert_contact(normalized, name)
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls 
                (contact_id, number, status, attempt_no, call_type, audio_mode, repeat_count, audio_name, message)
            VALUES (?, ?, 'initiated', ?, 'direct', ?, ?, ?, ?)
            """,
            (contact_id, normalized, _attempt_no(contact_id), audio_mode, repeat_count, audio_name, message_text),
        )
        
    _executor.submit(_run_direct_subprocess, call_id, dial_target, str(audio_path), repeat_val, max_call_secs, sip)
    return call_id


def start_direct_bulk(numbers: list[str], audio_name: str, repeat_count: int, message_text: str = "", audio_mode: str = "tts") -> list[int]:
    """Launch multiple direct calls in parallel."""
    return [start_direct_call(number, audio_name, repeat_count, message_text, audio_mode) for number in numbers]

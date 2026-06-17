-- E:\c-call-ivr\schema.sql
-- Unified SQLite schema for C-Call and IVR Calling

-- 1. App settings (SIP Credentials, Dial Format, CDR setup)
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. IVR Flow headers
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    root_node_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 3. Individual flow nodes (steps)
CREATE TABLE IF NOT EXISTS flow_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES flows(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT 'menu', -- menu | message | hangup | transfer
    prompt_text TEXT NOT NULL DEFAULT '', -- spoken via TTS if no audio file
    prompt_audio TEXT NOT NULL DEFAULT '', -- converted .ulaw file basename (optional)
    prompt_lang TEXT NOT NULL DEFAULT 'bn',
    transfer_number TEXT NOT NULL DEFAULT '',
    gather_timeout_seconds INTEGER NOT NULL DEFAULT 8,
    max_retries INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 4. transitions between nodes (keypress mapping)
CREATE TABLE IF NOT EXISTS flow_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES flow_nodes(id) ON DELETE CASCADE,
    digit TEXT NOT NULL, -- '0'-'9', '*', '#'
    next_node_id INTEGER REFERENCES flow_nodes(id) ON DELETE SET NULL,
    label TEXT NOT NULL DEFAULT '',
    UNIQUE (node_id, digit)
);

-- 5. Contacts / customers database
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL UNIQUE, -- normalized E.164
    name TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 6. Log of call attempts (Direct calls and IVR calls combined)
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    flow_id INTEGER REFERENCES flows(id) ON DELETE SET NULL, -- Nullable for direct calls
    number TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'outbound',
    status TEXT NOT NULL DEFAULT 'initiated', -- initiated | ringing | answered | completed | busy | no_answer | failed | canceled
    sip_final_status TEXT NOT NULL DEFAULT '',
    ring_seconds INTEGER NOT NULL DEFAULT 0,
    talk_seconds INTEGER NOT NULL DEFAULT 0,
    total_seconds INTEGER NOT NULL DEFAULT 0,
    digits_pressed INTEGER NOT NULL DEFAULT 0,
    drop_node_id INTEGER REFERENCES flow_nodes(id) ON DELETE SET NULL,
    last_node_id INTEGER REFERENCES flow_nodes(id) ON DELETE SET NULL,
    reached_terminal INTEGER NOT NULL DEFAULT 0, -- 0 = false, 1 = true
    recording_path TEXT NOT NULL DEFAULT '',
    attempt_no INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    answered_at TEXT,
    ended_at TEXT,
    call_type TEXT NOT NULL DEFAULT 'ivr', -- ivr | direct
    audio_mode TEXT NOT NULL DEFAULT '', -- tts | upload (for direct calls)
    repeat_count INTEGER NOT NULL DEFAULT 1, -- for direct calls
    audio_name TEXT NOT NULL DEFAULT '', -- converted .ulaw basename (for direct calls)
    message TEXT NOT NULL DEFAULT '' -- TTS raw message (for direct calls)
);

CREATE INDEX IF NOT EXISTS idx_calls_number ON calls (number);
CREATE INDEX IF NOT EXISTS idx_calls_contact ON calls (contact_id);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls (started_at DESC);

-- 7. Log of individual customer events during an IVR call
CREATE TABLE IF NOT EXISTS call_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    node_id INTEGER REFERENCES flow_nodes(id) ON DELETE SET NULL,
    digit TEXT NOT NULL DEFAULT '',
    response_ms INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    at_offset_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_call_events_call ON call_events (call_id, seq);

-- 8. CDR (Call Detail Record) log imported/fetched from SIP provider
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

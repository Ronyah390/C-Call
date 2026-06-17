# E:\c-call-ivr\db.py
"""Database layer — PostgreSQL (psycopg3) when DATABASE_URL is set, SQLite otherwise.

All public functions (query, execute, get_setting, etc.) have the same signature
regardless of backend, so the rest of the app never needs to know which is active.
"""
from __future__ import annotations

import datetime
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "c_call_ivr.db"
DATABASE_URL = os.environ.get("DATABASE_URL")

INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

# ─── PostgreSQL backend (active when DATABASE_URL env var is set) ─────────────

if DATABASE_URL:
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as _err:
        raise ImportError(
            "DATABASE_URL is set but psycopg[binary] / psycopg-pool are not installed.\n"
            "Run:  pip install 'psycopg[binary]' psycopg-pool"
        ) from _err

    _SCHEMA = BASE_DIR / "schema_pg.sql"
    _pg_pool: "ConnectionPool | None" = None
    _pg_lock = threading.Lock()

    def _pool() -> "ConnectionPool":
        global _pg_pool
        if _pg_pool is None:
            with _pg_lock:
                if _pg_pool is None:
                    _pg_pool = ConnectionPool(
                        DATABASE_URL,
                        min_size=1,
                        max_size=10,
                        kwargs={"row_factory": dict_row, "autocommit": True},
                        open=True,
                    )
        return _pg_pool

    def _pg_tr(sql: str) -> str:
        """Accept either ? (SQLite-style) or %s (PostgreSQL-style) placeholders."""
        return sql.replace("?", "%s")

    def _normalize(row: dict | None) -> dict | None:
        """Make PostgreSQL rows behave like SQLite rows for the rest of the app:
        timestamps -> strings, booleans -> 0/1 ints. This is what lets app.py
        slice timestamps (e.g. started_at[:16]) and treat flags as ints."""
        if row is None:
            return None
        for k, v in row.items():
            if isinstance(v, datetime.datetime):
                row[k] = v.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(v, datetime.date):
                row[k] = v.strftime("%Y-%m-%d")
            elif isinstance(v, bool):
                row[k] = 1 if v else 0
        return row

    def init_db() -> None:
        sql = _SCHEMA.read_text(encoding="utf-8")
        with _pool().connection() as conn:
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                try:
                    conn.execute(stmt)
                except psycopg.errors.DuplicateObject:
                    conn.rollback()
                except psycopg.Error:
                    conn.rollback()
                    raise

    def query(sql: str, params: tuple = ()) -> list[dict]:
        with _pool().connection() as conn:
            return [_normalize(r) for r in conn.execute(_pg_tr(sql), params).fetchall()]

    def query_one(sql: str, params: tuple = ()) -> dict | None:
        with _pool().connection() as conn:
            return _normalize(conn.execute(_pg_tr(sql), params).fetchone())

    def execute(sql: str, params: tuple = ()) -> None:
        with _pool().connection() as conn:
            conn.execute(_pg_tr(sql), params)

    def insert_returning_id(sql: str, params: tuple = ()) -> int:
        tr = _pg_tr(sql)
        if "returning id" not in tr.lower():
            tr = tr.rstrip().rstrip(";") + " RETURNING id"
        with _pool().connection() as conn:
            row = conn.execute(tr, params).fetchone()
            return int(row["id"])

    def get_setting(key: str, default: str = "") -> str:
        row = query_one("SELECT value FROM app_settings WHERE key = %s", (key,))
        return row["value"] if row else os.environ.get(key, default)

    def set_setting(key: str, value: str) -> None:
        execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )

# ─── SQLite backend (default when DATABASE_URL is not set) ────────────────────

else:
    import sqlite3

    _SCHEMA = BASE_DIR / "schema.sql"
    _db_lock = threading.Lock()

    def _now_utc() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _get_conn() -> sqlite3.Connection:
        def dict_factory(cursor, row):
            return {col[0]: row[i] for i, col in enumerate(cursor.description)}
        c = sqlite3.connect(DB_PATH, timeout=30.0)
        c.row_factory = dict_factory
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA foreign_keys = ON")
        c.create_function("now", 0, _now_utc)
        return c

    def _tr(sql: str) -> str:
        """Translate PostgreSQL-style SQL to SQLite."""
        sql = sql.replace("%s", "?")
        for cast in ("::int", "::integer", "::bigint", "::text", "::boolean"):
            sql = sql.replace(cast, "")
        return sql

    def init_db() -> None:
        sql = _SCHEMA.read_text(encoding="utf-8")
        with _db_lock:
            with _get_conn() as c:
                c.executescript(sql)

    def query(sql: str, params: tuple = ()) -> list[dict]:
        with _db_lock:
            with _get_conn() as c:
                return c.execute(_tr(sql), params).fetchall()

    def query_one(sql: str, params: tuple = ()) -> dict | None:
        with _db_lock:
            with _get_conn() as c:
                return c.execute(_tr(sql), params).fetchone()

    def execute(sql: str, params: tuple = ()) -> None:
        with _db_lock:
            with _get_conn() as c:
                c.execute(_tr(sql), params)
                c.commit()

    def insert_returning_id(sql: str, params: tuple = ()) -> int:
        tr = _tr(sql)
        if " returning id" in tr.lower():
            tr = tr[:tr.lower().rfind(" returning id")]
        with _db_lock:
            with _get_conn() as c:
                cur = c.execute(tr, params)
                c.commit()
                return cur.lastrowid

    def get_setting(key: str, default: str = "") -> str:
        row = query_one("SELECT value FROM app_settings WHERE key = %s", (key,))
        return row["value"] if row else os.environ.get(key, default)

    def set_setting(key: str, value: str) -> None:
        execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = now()",
            (key, value),
        )

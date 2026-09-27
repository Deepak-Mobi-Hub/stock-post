"""SQLite storage: contacts, conversations, stock offers, buyer demand, drafts.

One file, no ORM. A single connection guarded by a lock is plenty for one
WhatsApp number's worth of traffic.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    wa_id TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    company TEXT DEFAULT '',
    role TEXT DEFAULT '',
    country TEXT DEFAULT '',
    email TEXT DEFAULT '',
    type TEXT DEFAULT 'unknown',          -- supplier | buyer | both | unknown
    stage TEXT DEFAULT 'new',             -- new | contacted | engaged | qualified | trading | dormant | opted_out
    source TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    profile_json TEXT DEFAULT '{}',       -- qualification facts gathered in conversation
    score INTEGER DEFAULT 0,
    bot_paused INTEGER DEFAULT 0,
    opted_out INTEGER DEFAULT 0,
    consent INTEGER DEFAULT 0,            -- agreed to be contacted on WhatsApp
    disclosed INTEGER DEFAULT 0,          -- AI-assistant disclosure already sent
    followups_sent INTEGER DEFAULT 0,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    next_followup_at TEXT,
    last_processed_msg_id INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    direction TEXT NOT NULL,              -- in | out
    author TEXT NOT NULL,                 -- contact | bot | owner | system
    kind TEXT DEFAULT 'text',             -- text | template | media | note
    body TEXT DEFAULT '',
    media_json TEXT DEFAULT '',
    wa_message_id TEXT UNIQUE,
    status TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id, id);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    body TEXT NOT NULL,
    reason TEXT DEFAULT '',
    kind TEXT DEFAULT 'reply',            -- reply | followup | template
    followup_days INTEGER,                -- nudge after sending, if they go quiet
    status TEXT DEFAULT 'pending',        -- pending | sent | discarded
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    category TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    model TEXT DEFAULT '',
    model_key TEXT DEFAULT '',
    storage_gb INTEGER,
    spec TEXT DEFAULT '',
    grade TEXT DEFAULT '',
    grade_norm TEXT DEFAULT '',
    qty INTEGER,
    unit_price REAL,
    currency TEXT DEFAULT '',
    price_base REAL,
    location TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    source TEXT DEFAULT '',
    status TEXT DEFAULT 'available',      -- available | sold | expired
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_key ON offers(model_key, status);

CREATE TABLE IF NOT EXISTS requirements (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    category TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    model TEXT DEFAULT '',
    model_key TEXT DEFAULT '',
    storage_gb INTEGER,
    spec TEXT DEFAULT '',
    grades TEXT DEFAULT '',               -- comma list of acceptable normalised grades; empty = any
    qty INTEGER,
    target_price REAL,
    currency TEXT DEFAULT '',
    price_base REAL,
    destination TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    status TEXT DEFAULT 'open',           -- open | filled | closed
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requirements_key ON requirements(model_key, status);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    reason TEXT NOT NULL,
    status TEXT DEFAULT 'open',           -- open | done
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    campaign TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',         -- queued | sent | failed | skipped
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS style_examples (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER,
    draft TEXT DEFAULT '',
    final TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ basics
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def insert(self, table: str, values: dict) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict) -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))

    # ---------------------------------------------------------------- contacts
    def get_contact(self, contact_id: int) -> dict | None:
        return self.one("SELECT * FROM contacts WHERE id = ?", (contact_id,))

    def contact_by_wa(self, wa_id: str) -> dict | None:
        return self.one("SELECT * FROM contacts WHERE wa_id = ?", (wa_id,))

    def upsert_contact(self, wa_id: str, **fields: Any) -> dict:
        existing = self.contact_by_wa(wa_id)
        now = iso()
        if existing:
            # Only fill blanks — never overwrite what Deepak or the assistant already recorded.
            updates = {k: v for k, v in fields.items() if v not in (None, "") and not existing.get(k)}
            if updates:
                updates["updated_at"] = now
                self.update("contacts", existing["id"], updates)
            return self.get_contact(existing["id"])  # type: ignore[return-value]
        values = {"wa_id": wa_id, "created_at": now, "updated_at": now}
        values.update({k: v for k, v in fields.items() if v is not None})
        return self.get_contact(self.insert("contacts", values))  # type: ignore[return-value]

    def update_contact(self, contact_id: int, **fields: Any) -> None:
        fields["updated_at"] = iso()
        self.update("contacts", contact_id, fields)

    def profile(self, contact: dict) -> dict:
        try:
            return json.loads(contact.get("profile_json") or "{}")
        except json.JSONDecodeError:
            return {}

    def merge_profile(self, contact_id: int, facts: dict[str, str]) -> dict:
        contact = self.get_contact(contact_id) or {}
        profile = self.profile(contact)
        for key, value in facts.items():
            if value:
                profile[key] = value
        self.update_contact(contact_id, profile_json=json.dumps(profile, ensure_ascii=False))
        return profile

    # ---------------------------------------------------------------- messages
    def add_message(self, contact_id: int, direction: str, author: str, body: str, *,
                    kind: str = "text", wa_message_id: str | None = None,
                    media: dict | None = None, status: str = "", at: datetime | None = None) -> int | None:
        """Store a message. Returns None if this WhatsApp message id was already stored (webhook retry)."""
        try:
            return self.insert("messages", {
                "contact_id": contact_id, "direction": direction, "author": author, "kind": kind,
                "body": body, "media_json": json.dumps(media) if media else "",
                "wa_message_id": wa_message_id, "status": status, "created_at": iso(at),
            })
        except sqlite3.IntegrityError:
            return None

    def messages_for(self, contact_id: int, limit: int = 60) -> list[dict]:
        rows = self.all("SELECT * FROM messages WHERE contact_id = ? ORDER BY id DESC LIMIT ?", (contact_id, limit))
        return list(reversed(rows))

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

"""SQLite storage. Every function opens its own connection so calls are safe from worker threads."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY,
    sheet_row INTEGER,
    submitted_at TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    netid TEXT,
    availability_raw TEXT,
    availability_json TEXT,
    status TEXT NOT NULL DEFAULT 'waiting',   -- waiting | done
    UNIQUE (submitted_at, email)
);
CREATE TABLE IF NOT EXISTS members (
    discord_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    availability_raw TEXT,
    availability_json TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    member_id INTEGER NOT NULL REFERENCES members(discord_id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    -- proposed -> emailed -> confirmed | declined ; cancelled = member can't run it ;
    -- withdrawn = member changed their availability before confirming
    status TEXT NOT NULL DEFAULT 'proposed',
    message_id INTEGER,
    emailed_at TEXT,
    reminded INTEGER NOT NULL DEFAULT 0,
    nudged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

ACTIVE = ("proposed", "emailed", "confirmed")


@contextmanager
def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)


def _rows(sql, args=()):
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def _load(row):
    if row and row.get("availability_json"):
        row["availability"] = json.loads(row["availability_json"])
    elif row:
        row["availability"] = None
    return row


# --- participants ---------------------------------------------------------

def upsert_participant(sheet_row, submitted_at, name, email, netid, availability_raw, done):
    """Insert a form response if new. Returns True when a row was added."""
    with connect() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO participants (sheet_row, submitted_at, name, email, netid,"
            " availability_raw, status) VALUES (?,?,?,?,?,?,?)",
            (sheet_row, submitted_at, name, email, netid, availability_raw, "done" if done else "waiting"),
        )
        return cur.rowcount == 1


def participants(where="1=1", args=()):
    return [_load(r) for r in _rows(f"SELECT * FROM participants WHERE {where} ORDER BY submitted_at", args)]


def participant(pid):
    rows = participants("id = ?", (pid,))
    return rows[0] if rows else None


def set_participant_availability(pid, availability):
    with connect() as con:
        con.execute("UPDATE participants SET availability_json = ? WHERE id = ?", (json.dumps(availability), pid))


def set_participant_status(pid, status):
    with connect() as con:
        con.execute("UPDATE participants SET status = ? WHERE id = ?", (status, pid))


# --- members --------------------------------------------------------------

def set_member(discord_id, name, raw, availability):
    with connect() as con:
        con.execute(
            "INSERT INTO members (discord_id, name, availability_raw, availability_json, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(discord_id) DO UPDATE SET name=excluded.name,"
            " availability_raw=excluded.availability_raw, availability_json=excluded.availability_json,"
            " updated_at=excluded.updated_at",
            (discord_id, name, raw, json.dumps(availability) if availability else None,
             datetime.now(config.TZ).isoformat(timespec="minutes")),
        )


def members():
    return [_load(r) for r in _rows("SELECT * FROM members ORDER BY name")]


def member(discord_id):
    rows = [_load(r) for r in _rows("SELECT * FROM members WHERE discord_id = ?", (discord_id,))]
    return rows[0] if rows else None


# --- sessions -------------------------------------------------------------

def add_session(week_start, participant_id, member_id, start_at, end_at):
    with connect() as con:
        cur = con.execute(
            "INSERT INTO sessions (week_start, participant_id, member_id, start_at, end_at) VALUES (?,?,?,?,?)",
            (week_start, participant_id, member_id, start_at, end_at),
        )
        return cur.lastrowid


def sessions(where="1=1", args=()):
    return _rows(
        "SELECT s.*, p.name AS participant_name, p.email AS participant_email, p.sheet_row AS sheet_row,"
        " m.name AS member_name FROM sessions s JOIN participants p ON p.id = s.participant_id"
        f" JOIN members m ON m.discord_id = s.member_id WHERE {where} ORDER BY s.start_at",
        args,
    )


def session(sid):
    rows = sessions("s.id = ?", (sid,))
    return rows[0] if rows else None


def active_sessions():
    return sessions("s.status IN (?,?,?)", ACTIVE)


def update_session(sid, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), sid))


def transition(sid, from_statuses, to_status, **fields):
    """Move a session between states atomically. Returns False if it was not in an allowed state,
    which is what stops a double-click from emailing a participant twice."""
    marks = ",".join("?" for _ in from_statuses)
    sets = "".join(f", {k} = ?" for k in fields)
    with connect() as con:
        cur = con.execute(
            f"UPDATE sessions SET status = ?{sets} WHERE id = ? AND status IN ({marks})",
            (to_status, *fields.values(), sid, *from_statuses),
        )
        return cur.rowcount == 1


# --- meta -----------------------------------------------------------------

def claim(key: str) -> bool:
    """Record that a once-only job ran. Returns False if it had already been claimed."""
    with connect() as con:
        cur = con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                          (key, datetime.now(config.TZ).isoformat(timespec="minutes")))
        return cur.rowcount == 1

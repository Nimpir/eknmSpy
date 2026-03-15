"""
db.py — SQLite storage for sessions and transcript segments
"""

import logging
import sqlite3
import os
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "transcripts.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    log.debug("Initialising database at %s", DB_PATH)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                ended_at    TEXT,
                guild       TEXT,
                channel     TEXT
            );

            CREATE TABLE IF NOT EXISTS segments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL REFERENCES sessions(id),
                speaker     TEXT NOT NULL,
                start_sec   REAL NOT NULL,
                end_sec     REAL NOT NULL,
                text        TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts
                USING fts5(text, speaker, content='segments', content_rowid='id');

            CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
                INSERT INTO segments_fts(rowid, text, speaker)
                VALUES (new.id, new.text, new.speaker);
            END;
        """)


def create_session(name: str, guild: str = "", channel: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (name, started_at, guild, channel) VALUES (?,?,?,?)",
            (name, datetime.now().isoformat(), guild, channel)
        )
        return cur.lastrowid


def close_session(session_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?",
            (datetime.now().isoformat(), session_id)
        )


def insert_segments(session_id: int, segments: list[dict]):
    """segments: [{'speaker': str, 'start': float, 'end': float, 'text': str}]"""
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO segments (session_id, speaker, start_sec, end_sec, text) VALUES (?,?,?,?,?)",
            [(session_id, s["speaker"], s["start"], s["end"], s["text"]) for s in segments]
        )


def get_session(session_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()


def get_last_session_for_guild(guild: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE guild=? ORDER BY started_at DESC LIMIT 1",
            (guild,)
        ).fetchone()


def get_sessions() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC"
        ).fetchall()


def get_segments(session_id: int) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM segments WHERE session_id=? ORDER BY start_sec",
            (session_id,)
        ).fetchall()


def search(query: str, session_id: int = None) -> list:
    """Full-text search. Returns rows with session info joined."""
    with get_conn() as conn:
        if session_id:
            return conn.execute("""
                SELECT seg.*, ses.name as session_name, ses.started_at
                FROM segments_fts fts
                JOIN segments seg ON seg.id = fts.rowid
                JOIN sessions ses ON ses.id = seg.session_id
                WHERE fts.text MATCH ? AND seg.session_id = ?
                ORDER BY seg.start_sec
            """, (query, session_id)).fetchall()
        else:
            return conn.execute("""
                SELECT seg.*, ses.name as session_name, ses.started_at
                FROM segments_fts fts
                JOIN segments seg ON seg.id = fts.rowid
                JOIN sessions ses ON ses.id = seg.session_id
                WHERE fts.text MATCH ?
                ORDER BY ses.started_at DESC, seg.start_sec
            """, (query,)).fetchall()

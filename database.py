"""database.py — SQLite database using Python's built-in sqlite3.

No external DB service needed.
- Users table   : id, email, hashed_password, created_at
- Sessions table: all generation history, email_body stored as TEXT (no file system)
- Statistics    : total generation counter

On Render free tier the SQLite file is ephemeral (resets on redeploy).
Set DB_PATH env var to a Render Disk mount path for persistence.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "./email_generator.db")
TIMEZONE = os.getenv("TIMEZONE", "UTC")


def _now() -> str:
    """Return ISO timestamp string in UTC."""
    return datetime.now(timezone.utc).isoformat()


# ── Thread-safe connection factory ──────────────────────────────────────────
# SQLite connections cannot be shared across threads, so we use thread-local storage.
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row  # access columns by name
        _local.conn.execute("PRAGMA journal_mode=WAL")  # better concurrency
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


class EmailHistoryDB:
    """SQLite-backed database for email generation history."""

    def __init__(self):
        self._init_database()
        print(f"✅ SQLite database ready at: {os.path.abspath(DB_PATH)}")

    def _init_database(self):
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email           TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id           TEXT PRIMARY KEY,
                timestamp            TEXT NOT NULL,
                email_address        TEXT NOT NULL,
                thread_subject       TEXT,
                intent               TEXT,
                subject              TEXT,
                email_body           TEXT,
                tone                 TEXT DEFAULT 'professional',
                selected_email_index INTEGER,
                email_goal           TEXT,
                thread_email_count   INTEGER DEFAULT 0,
                last_modified        TEXT NOT NULL,
                is_new_email         INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS statistics (
                id                INTEGER PRIMARY KEY,
                total_generations INTEGER DEFAULT 0
            );
        """)
        # Seed statistics row if not present
        conn.execute("INSERT OR IGNORE INTO statistics (id, total_generations) VALUES (1, 0)")
        conn.commit()
        print("✅ Database tables verified/created")

    # ── USER OPERATIONS ──────────────────────────────────────────────────────

    def create_user(self, email: str, hashed_password: str) -> bool:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO users (email, hashed_password, created_at) VALUES (?, ?, ?)",
                (email, hashed_password, _now())
            )
            conn.commit()
            print(f"✅ Created user: {email}")
            return True
        except sqlite3.IntegrityError:
            print(f"⚠️  User already exists: {email}")
            return False
        except Exception as e:
            print(f"❌ Error creating user: {e}")
            return False

    def get_user_by_email(self, email: str) -> Optional[Dict]:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, email, hashed_password, created_at FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_users(self) -> List[Dict]:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, email, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_user(self, email: str) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.commit()
        return cur.rowcount > 0

    def reset_password(self, email: str, hashed_password: str) -> bool:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE users SET hashed_password = ? WHERE email = ?",
            (hashed_password, email)
        )
        conn.commit()
        return cur.rowcount > 0

    # ── SESSION OPERATIONS ───────────────────────────────────────────────────

    def save_generation(self, session_data: Dict, session_id: Optional[str] = None) -> str:
        conn = _get_conn()
        now = _now()
        if session_id is None:
            session_id = f"session_{datetime.now(timezone.utc).strftime('%d%m%Y_%H%M%S')}"

        conn.execute("""
            INSERT INTO sessions (
                session_id, timestamp, email_address, thread_subject,
                intent, subject, email_body, tone, selected_email_index,
                email_goal, thread_email_count, last_modified, is_new_email
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, now,
            session_data.get("email_address", ""),
            session_data.get("thread_subject", ""),
            session_data.get("intent", ""),
            session_data.get("subject", ""),
            session_data.get("email_body", ""),        # full text stored directly
            session_data.get("tone", "professional"),
            session_data.get("selected_email_index"),
            session_data.get("email_goal", ""),
            session_data.get("thread_email_count", 0),
            now,
            1 if session_data.get("is_new_email") else 0,
        ))
        conn.execute("UPDATE statistics SET total_generations = total_generations + 1 WHERE id = 1")
        conn.commit()
        print(f"✅ Saved session: {session_id}")
        return session_id

    def update_session(self, session_id: str, updated_data: Dict) -> bool:
        conn = _get_conn()
        fields, values = [], []
        for field in ("subject", "email_body", "email_goal", "tone"):
            if field in updated_data:
                fields.append(f"{field} = ?")
                values.append(updated_data[field])
        if not fields:
            return False
        fields.append("last_modified = ?")
        values.append(_now())
        values.append(session_id)
        cur = conn.execute(
            f"UPDATE sessions SET {', '.join(fields)} WHERE session_id = ?",
            values
        )
        conn.commit()
        return cur.rowcount > 0

    def get_all_sessions(self, limit: int = 50) -> List[Dict]:
        conn = _get_conn()
        rows = conn.execute("""
            SELECT session_id, timestamp, email_address, thread_subject,
                   intent, subject, email_body, tone, selected_email_index,
                   email_goal, thread_email_count, last_modified, is_new_email
            FROM sessions ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_session_by_id(self, session_id: str) -> Optional[Dict]:
        conn = _get_conn()
        row = conn.execute("""
            SELECT session_id, timestamp, email_address, thread_subject,
                   intent, subject, email_body, tone, selected_email_index,
                   email_goal, thread_email_count, last_modified, is_new_email
            FROM sessions WHERE session_id = ?
        """, (session_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def delete_session(self, session_id: str) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE statistics SET total_generations = MAX(total_generations - 1, 0) WHERE id = 1"
        )
        conn.commit()
        return True

    def clear_all_history(self) -> bool:
        conn = _get_conn()
        conn.execute("DELETE FROM sessions")
        conn.execute("UPDATE statistics SET total_generations = 0 WHERE id = 1")
        conn.commit()
        return True

    # ── STATS ────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        conn = _get_conn()
        total = conn.execute(
            "SELECT total_generations FROM statistics WHERE id = 1"
        ).fetchone()
        current = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()
        intents = conn.execute(
            "SELECT intent, COUNT(*) as c FROM sessions GROUP BY intent"
        ).fetchall()
        last = conn.execute(
            "SELECT timestamp FROM sessions ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        return {
            "total_generations": total["total_generations"] if total else 0,
            "current_sessions":  current["c"] if current else 0,
            "intent_breakdown":  {r["intent"]: r["c"] for r in intents},
            "last_generation":   last["timestamp"] if last else None,
        }

    @staticmethod
    def _row_to_dict(row) -> Dict:
        d = dict(row)
        d["is_new_email"] = bool(d.get("is_new_email", 0))
        return d


# ── Global singleton ─────────────────────────────────────────────────────────
db = EmailHistoryDB()


# ── Public wrapper functions (used by api.py / email_router.py) ──────────────

def get_user_by_email(email: str) -> Optional[Dict]:
    return db.get_user_by_email(email)

def save_generation(session_data: Dict, session_id: Optional[str] = None) -> str:
    return db.save_generation(session_data, session_id)

def update_session(session_id: str, updated_data: Dict) -> bool:
    return db.update_session(session_id, updated_data)

def get_all_sessions(limit: int = 50) -> List[Dict]:
    return db.get_all_sessions(limit)

def get_session_by_id(session_id: str) -> Optional[Dict]:
    return db.get_session_by_id(session_id)

def delete_session(session_id: str) -> bool:
    return db.delete_session(session_id)

def clear_all_history() -> bool:
    return db.clear_all_history()

def get_stats() -> Dict:
    return db.get_stats()

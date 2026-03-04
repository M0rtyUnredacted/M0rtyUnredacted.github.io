"""SQLite state database.

Tables:
  processed_docs  -- NLM pipeline tracking
  daily_quota     -- per-day NLM video count
  tiktok_posts    -- TikTok scheduler log
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "nlm_app.db")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS processed_docs (
                file_id       TEXT PRIMARY KEY,
                file_name     TEXT,
                modified_time TEXT,
                status        TEXT,
                output_file   TEXT,
                error_msg     TEXT,
                processed_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_quota (
                date  TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tiktok_posts (
                file_id        TEXT PRIMARY KEY,
                file_name      TEXT,
                status         TEXT,
                scheduled_time TEXT,
                error_msg      TEXT,
                created_at     TEXT
            );
        """)
    log.debug("DB initialised at %s", DB_PATH)


# ── NLM processed_docs ────────────────────────────────────────────────────────

def is_doc_processed(file_id: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT status FROM processed_docs WHERE file_id = ?", (file_id,)
        ).fetchone()
    return row is not None and row["status"] in ("done", "in_progress")


def mark_doc_in_progress(file_id: str, file_name: str, modified_time: str = "") -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO processed_docs
               (file_id, file_name, modified_time, status, processed_at)
               VALUES (?, ?, ?, 'in_progress', datetime('now'))""",
            (file_id, file_name, modified_time),
        )


def mark_doc_done(file_id: str, output_file: str = "") -> None:
    with _conn() as con:
        con.execute(
            """UPDATE processed_docs
               SET status='done', output_file=?, processed_at=datetime('now')
               WHERE file_id=?""",
            (output_file, file_id),
        )


def mark_doc_failed(file_id: str, error_msg: str) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE processed_docs
               SET status='failed', error_msg=?, processed_at=datetime('now')
               WHERE file_id=?""",
            (error_msg, file_id),
        )


# ── Daily quota ───────────────────────────────────────────────────────────────

def quota_used_today() -> int:
    today = date.today().isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT count FROM daily_quota WHERE date=?", (today,)
        ).fetchone()
    return row["count"] if row else 0


def increment_quota() -> int:
    today = date.today().isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO daily_quota (date, count) VALUES (?, 1)
               ON CONFLICT(date) DO UPDATE SET count = count + 1""",
            (today,),
        )
        row = con.execute(
            "SELECT count FROM daily_quota WHERE date=?", (today,)
        ).fetchone()
    return row["count"]


# ── TikTok posts ──────────────────────────────────────────────────────────────

def is_tiktok_processed(file_id: str) -> bool:
    """Return True if this file was scheduled OR has already failed.

    Failed videos are NOT retried automatically — they stay blocked until
    the row is deleted from tiktok_posts, preventing duplicate uploads when
    a partial failure leaves a video on TikTok without a DB record.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT status FROM tiktok_posts WHERE file_id=?", (file_id,)
        ).fetchone()
    return row is not None and row["status"] in ("scheduled", "failed")


def mark_tiktok_scheduled(file_id: str, file_name: str, scheduled_time: str) -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO tiktok_posts
               (file_id, file_name, status, scheduled_time, created_at)
               VALUES (?, ?, 'scheduled', ?, datetime('now'))""",
            (file_id, file_name, scheduled_time),
        )


def mark_tiktok_failed(file_id: str, file_name: str, error_msg: str) -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO tiktok_posts
               (file_id, file_name, status, error_msg, created_at)
               VALUES (?, ?, 'failed', ?, datetime('now'))""",
            (file_id, file_name, error_msg),
        )


def reset_tiktok_failed() -> int:
    """Delete all 'failed' tiktok_posts rows so they are retried on next poll.

    Returns the number of rows deleted.
    """
    with _conn() as con:
        cur = con.execute("DELETE FROM tiktok_posts WHERE status='failed'")
    return cur.rowcount


def reset_tiktok_all() -> int:
    """Delete ALL tiktok_posts rows (scheduled + failed) so every Drive video
    is treated as new on the next poll.  Use when TikTok posts are confirmed
    not actually scheduled/live and the DB state needs a full reset.

    Returns the number of rows deleted.
    """
    with _conn() as con:
        cur = con.execute("DELETE FROM tiktok_posts")
    return cur.rowcount


def last_tiktok_scheduled_time() -> str | None:
    """Return ISO datetime string of the most recently scheduled TikTok post."""
    with _conn() as con:
        row = con.execute(
            "SELECT scheduled_time FROM tiktok_posts WHERE status='scheduled' "
            "ORDER BY scheduled_time DESC LIMIT 1"
        ).fetchone()
    return row["scheduled_time"] if row else None

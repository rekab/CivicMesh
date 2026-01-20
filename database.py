import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

from logger import _sanitize_for_log


@dataclass(frozen=True)
class DBConfig:
    path: str = "civic_mesh.db"
    timeout_sec: float = 5.0


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,        -- "mesh" | "wifi"
    session_id TEXT,
    fingerprint TEXT,
    upvotes INTEGER DEFAULT 0,
    downvotes INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    pin_order INTEGER
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    session_id TEXT NOT NULL,
    fingerprint TEXT,
    sent INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS votes (
    message_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    vote_type INTEGER NOT NULL,  -- 1=upvote, -1=downvote
    ts INTEGER NOT NULL,
    PRIMARY KEY (message_id, session_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    name TEXT,
    location TEXT,
    mac_address TEXT,
    fingerprint TEXT,
    created_ts INTEGER,
    last_post_ts INTEGER,
    post_count_hour INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_pinned ON messages(pinned, pin_order);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(sent, ts);
CREATE INDEX IF NOT EXISTS idx_votes_message ON votes(message_id);
CREATE INDEX IF NOT EXISTS idx_sessions_mac ON sessions(mac_address);
"""


def _connect(cfg: DBConfig) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(cfg.path) or ".", exist_ok=True)
    conn = sqlite3.connect(cfg.path, timeout=cfg.timeout_sec, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    return conn


def init_db(cfg: DBConfig, log=None) -> None:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:init schema path=%s", _sanitize_for_log(cfg.path))
        conn.executescript(SCHEMA_SQL)
        # Lightweight migration for older DBs.
        sessions_cols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "fingerprint" not in sessions_cols:
            if log:
                log.info("db:migrate add sessions.fingerprint")
            conn.execute("ALTER TABLE sessions ADD COLUMN fingerprint TEXT")
        messages_cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "session_id" not in messages_cols:
            if log:
                log.info("db:migrate add messages.session_id")
            conn.execute("ALTER TABLE messages ADD COLUMN session_id TEXT")
        if "fingerprint" not in messages_cols:
            if log:
                log.info("db:migrate add messages.fingerprint")
            conn.execute("ALTER TABLE messages ADD COLUMN fingerprint TEXT")
        outbox_cols = [r["name"] for r in conn.execute("PRAGMA table_info(outbox)").fetchall()]
        if "fingerprint" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.fingerprint")
            conn.execute("ALTER TABLE outbox ADD COLUMN fingerprint TEXT")
    finally:
        conn.close()


def insert_message(
    cfg: DBConfig,
    *,
    ts: int,
    channel: str,
    sender: str,
    content: str,
    source: str,
    session_id: Optional[str] = None,
    fingerprint: Optional[str] = None,
    log=None,
) -> int:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:insert_message channel=%s sender=%s source=%s len=%d", channel, sender, source, len(content))
        cur = conn.execute(
            "INSERT INTO messages (ts, channel, sender, content, source, session_id, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, channel, sender, content, source, session_id, fingerprint),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_messages(
    cfg: DBConfig,
    *,
    channel: str,
    limit: int = 50,
    offset: int = 0,
    include_pinned: bool = True,
    log=None,
) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_messages channel=%s limit=%d offset=%d", channel, limit, offset)

        rows: list[sqlite3.Row] = []
        if include_pinned:
            rows.extend(
                conn.execute(
                    "SELECT * FROM messages WHERE channel=? AND pinned=1 ORDER BY pin_order ASC NULLS LAST, ts DESC",
                    (channel,),
                ).fetchall()
            )
        rows.extend(
            conn.execute(
                "SELECT * FROM messages WHERE channel=? AND pinned=0 ORDER BY ts DESC LIMIT ? OFFSET ?",
                (channel, limit, offset),
            ).fetchall()
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


def queue_outbox(
    cfg: DBConfig,
    *,
    ts: int,
    channel: str,
    sender: str,
    content: str,
    session_id: str,
    fingerprint: Optional[str] = None,
    log=None,
) -> int:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:queue_outbox channel=%s sender=%s session=%s len=%d", channel, sender, session_id, len(content))
        cur = conn.execute(
            "INSERT INTO outbox (ts, channel, sender, content, session_id, fingerprint, sent) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (ts, channel, sender, content, session_id, fingerprint),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_pending_outbox(cfg: DBConfig, *, limit: int, log=None) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_pending_outbox limit=%d", limit)
        rows = conn.execute(
            "SELECT * FROM outbox WHERE sent=0 ORDER BY ts ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pending_outbox_for_session(
    cfg: DBConfig,
    *,
    session_id: str,
    channel: str,
    limit: int = 20,
    log=None,
) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_pending_outbox_for_session session=%s channel=%s limit=%d", session_id, channel, limit)
        rows = conn.execute(
            "SELECT * FROM outbox WHERE sent=0 AND session_id=? AND channel=? ORDER BY ts DESC LIMIT ?",
            (session_id, channel, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pending_outbox_for_channel(
    cfg: DBConfig,
    *,
    channel: str,
    limit: int = 20,
    log=None,
) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_pending_outbox_for_channel channel=%s limit=%d", channel, limit)
        rows = conn.execute(
            "SELECT * FROM outbox WHERE sent=0 AND channel=? ORDER BY ts DESC LIMIT ?",
            (channel, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_outbox_sent(cfg: DBConfig, *, outbox_ids: list[int], log=None) -> None:
    if not outbox_ids:
        return
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:mark_outbox_sent count=%d", len(outbox_ids))
        q = "UPDATE outbox SET sent=1 WHERE id IN (%s)" % ",".join("?" for _ in outbox_ids)
        conn.execute(q, outbox_ids)
    finally:
        conn.close()


def create_or_update_session(
    cfg: DBConfig,
    *,
    session_id: str,
    name: str,
    location: str,
    mac_address: Optional[str],
    fingerprint: Optional[str] = None,
    now_ts: Optional[int] = None,
    log=None,
) -> None:
    now_ts = int(now_ts or time.time())
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:create_or_update_session session=%s mac=%s", session_id, mac_address or "")
        conn.execute(
            """
            INSERT INTO sessions(session_id, name, location, mac_address, fingerprint, created_ts, last_post_ts, post_count_hour)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
            ON CONFLICT(session_id) DO UPDATE SET
              name=excluded.name,
              location=excluded.location,
              mac_address=excluded.mac_address,
              fingerprint=excluded.fingerprint
            """,
            (session_id, name, location, mac_address, fingerprint, now_ts),
        )
    finally:
        conn.close()


def update_session_fingerprint(cfg: DBConfig, *, session_id: str, fingerprint: str, log=None) -> None:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:update_session_fingerprint session=%s", session_id)
        conn.execute("UPDATE sessions SET fingerprint=? WHERE session_id=?", (fingerprint, session_id))
    finally:
        conn.close()


def get_session(cfg: DBConfig, *, session_id: str, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_session session=%s", session_id)
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record_post_for_session(cfg: DBConfig, *, session_id: str, now_ts: Optional[int] = None, log=None) -> None:
    now_ts = int(now_ts or time.time())
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:record_post_for_session session=%s ts=%d", session_id, now_ts)
        conn.execute(
            """
            UPDATE sessions
            SET last_post_ts=?,
                post_count_hour=COALESCE(post_count_hour,0)+1
            WHERE session_id=?
            """,
            (now_ts, session_id),
        )
    finally:
        conn.close()


def posts_in_last_window(cfg: DBConfig, *, session_id: str, window_sec: int = 3600, now_ts: Optional[int] = None, log=None) -> int:
    """
    Rolling window approximation: uses last_post_ts + counter for the window.
    For a precise rolling window, we would store per-post timestamps; this keeps schema minimal.
    """
    now_ts = int(now_ts or time.time())
    sess = get_session(cfg, session_id=session_id, log=log)
    if not sess:
        return 0
    last_ts = sess.get("last_post_ts")
    count = int(sess.get("post_count_hour") or 0)
    if not last_ts:
        return 0
    if now_ts - int(last_ts) > window_sec:
        # window expired; reset in DB
        conn = _connect(cfg)
        try:
            if log:
                log.debug("db:rate_limit_reset session=%s", session_id)
            conn.execute("UPDATE sessions SET post_count_hour=0 WHERE session_id=?", (session_id,))
        finally:
            conn.close()
        return 0
    return count


def update_vote(cfg: DBConfig, *, message_id: int, session_id: str, vote_type: int, ts: int, log=None) -> None:
    """
    vote_type: 1=upvote, -1=downvote, 0=remove
    """
    if vote_type not in (-1, 0, 1):
        raise ValueError("vote_type must be -1, 0, or 1")
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:update_vote msg=%d session=%s vote=%d", message_id, session_id, vote_type)
        if vote_type == 0:
            conn.execute("DELETE FROM votes WHERE message_id=? AND session_id=?", (message_id, session_id))
        else:
            conn.execute(
                """
                INSERT INTO votes(message_id, session_id, vote_type, ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id, session_id) DO UPDATE SET vote_type=excluded.vote_type, ts=excluded.ts
                """,
                (message_id, session_id, vote_type, ts),
            )
        _recount_votes(conn, message_id)
    finally:
        conn.close()


def _recount_votes(conn: sqlite3.Connection, message_id: int) -> None:
    up = conn.execute("SELECT COUNT(*) AS c FROM votes WHERE message_id=? AND vote_type=1", (message_id,)).fetchone()["c"]
    down = conn.execute(
        "SELECT COUNT(*) AS c FROM votes WHERE message_id=? AND vote_type=-1", (message_id,)
    ).fetchone()["c"]
    conn.execute("UPDATE messages SET upvotes=?, downvotes=? WHERE id=?", (int(up), int(down), message_id))


def get_vote_counts(cfg: DBConfig, *, message_id: int, log=None) -> tuple[int, int]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_vote_counts msg=%d", message_id)
        row = conn.execute("SELECT upvotes, downvotes FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return 0, 0
        return int(row["upvotes"] or 0), int(row["downvotes"] or 0)
    finally:
        conn.close()


def get_message(cfg: DBConfig, *, message_id: int, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_message id=%d", message_id)
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_vote(cfg: DBConfig, *, message_id: int, session_id: str, log=None) -> int:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_user_vote msg=%d session=%s", message_id, session_id)
        row = conn.execute(
            "SELECT vote_type FROM votes WHERE message_id=? AND session_id=?",
            (message_id, session_id),
        ).fetchone()
        return int(row["vote_type"]) if row else 0
    finally:
        conn.close()


def pin_message(cfg: DBConfig, *, message_id: int, pin_order: Optional[int] = None, log=None) -> None:
    conn = _connect(cfg)
    try:
        if pin_order is None:
            row = conn.execute("SELECT COALESCE(MAX(pin_order),0) AS m FROM messages WHERE pinned=1").fetchone()
            pin_order = int(row["m"] or 0) + 1
        if log:
            log.debug("db:pin_message msg=%d order=%d", message_id, pin_order)
        conn.execute("UPDATE messages SET pinned=1, pin_order=? WHERE id=?", (pin_order, message_id))
    finally:
        conn.close()


def unpin_message(cfg: DBConfig, *, message_id: int, log=None) -> None:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:unpin_message msg=%d", message_id)
        conn.execute("UPDATE messages SET pinned=0, pin_order=NULL WHERE id=?", (message_id,))
    finally:
        conn.close()


def cleanup_retention_bytes_per_channel(cfg: DBConfig, *, channel: str, max_bytes: int, log=None) -> int:
    """
    Deletes oldest unpinned messages until approximate total content size is under max_bytes.
    Returns number of deleted rows.
    """
    conn = _connect(cfg)
    try:
        # Approximate bytes as UTF-8 length of content plus small overhead
        row = conn.execute("SELECT COALESCE(SUM(LENGTH(content)),0) AS b FROM messages WHERE channel=?", (channel,)).fetchone()
        total = int(row["b"] or 0)
        if total <= max_bytes:
            return 0
        deleted = 0
        if log:
            log.info("retention:channel=%s total_bytes=%d max_bytes=%d", channel, total, max_bytes)
        while total > max_bytes:
            r = conn.execute(
                """
                SELECT id, LENGTH(content) AS b
                FROM messages
                WHERE channel=? AND pinned=0
                ORDER BY ts ASC
                LIMIT 1
                """,
                (channel,),
            ).fetchone()
            if not r:
                break
            mid = int(r["id"])
            b = int(r["b"] or 0)
            conn.execute("DELETE FROM messages WHERE id=?", (mid,))
            # votes are not FK-enforced; clean them too
            conn.execute("DELETE FROM votes WHERE message_id=?", (mid,))
            total -= b
            deleted += 1
        if log and deleted:
            log.info("retention:deleted channel=%s count=%d", channel, deleted)
        return deleted
    finally:
        conn.close()


def search_messages(
    cfg: DBConfig,
    *,
    query: str,
    channel: Optional[str] = None,
    sender: Optional[str] = None,
    limit: int = 5,
    log=None,
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:search_messages q=%s channel=%s sender=%s limit=%d", q, channel or "", sender or "", limit)
        where = ["content LIKE ?"]
        params: list[Any] = [f"%{q}%"]
        if channel:
            where.append("channel=?")
            params.append(channel)
        if sender:
            where.append("sender LIKE ?")
            params.append(f"%{sender}%")
        sql = "SELECT id, ts, channel, sender, content FROM messages WHERE " + " AND ".join(where) + " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

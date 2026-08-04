"""SQLite index.

The database is a *cache*, never a source of truth — the .jsonl files are. Any schema
mismatch or corruption is resolved by deleting the file and rebuilding, so migrations
are never needed.

Each thread gets its own connection (sqlite3 objects are not shareable across threads).
WAL lets the indexer write while the UI worker reads.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from csm import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Every file we have parsed. The (mtime, size) pair is the dirty-detection unit.
CREATE TABLE IF NOT EXISTS files(
  path       TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  kind       TEXT NOT NULL,          -- 'main' | 'dup' | 'subagent'
  mtime      REAL NOT NULL,
  size       INTEGER NOT NULL,
  parsed_ok  INTEGER NOT NULL DEFAULT 1,
  error      TEXT
);
CREATE INDEX IF NOT EXISTS files_session ON files(session_id);

CREATE TABLE IF NOT EXISTS sessions(
  session_id     TEXT PRIMARY KEY,
  canonical_path TEXT NOT NULL,
  project_dir    TEXT NOT NULL,
  cwd            TEXT,
  git_branch     TEXT,
  title          TEXT,
  title_source   TEXT,               -- 'custom' | 'ai' | 'prompt' | 'none'
  first_ts       TEXT,
  last_ts        TEXT,
  cli_version    TEXT,
  human_msgs     INTEGER NOT NULL DEFAULT 0,
  assistant_msgs INTEGER NOT NULL DEFAULT 0,
  total_records  INTEGER NOT NULL DEFAULT 0,
  malformed_lines INTEGER NOT NULL DEFAULT 0,
  models         TEXT,               -- JSON {model: record_count}
  estimated      INTEGER NOT NULL DEFAULT 0,  -- any unknown model priced at fallback
  cost_usd       REAL NOT NULL DEFAULT 0,
  in_tok         INTEGER NOT NULL DEFAULT 0,
  out_tok        INTEGER NOT NULL DEFAULT 0,
  cache_w_tok    INTEGER NOT NULL DEFAULT 0,
  cache_r_tok    INTEGER NOT NULL DEFAULT 0,
  file_bytes     INTEGER NOT NULL DEFAULT 0,  -- canonical + duplicate .jsonl files
  extra_bytes    INTEGER NOT NULL DEFAULT 0,  -- <uuid>/ + session-env + tasks + jobs
  dup_paths      TEXT,               -- JSON [path, ...]
  slug           TEXT
);
CREATE INDEX IF NOT EXISTS sessions_cwd  ON sessions(cwd);
CREATE INDEX IF NOT EXISTS sessions_last ON sessions(last_ts DESC);

-- Byte offsets into the canonical file. Lets the transcript viewer seek directly to a
-- message in an 88MB file instead of ever storing transcript text in the DB.
CREATE TABLE IF NOT EXISTS message_index(
  session_id TEXT NOT NULL,
  msg_idx    INTEGER NOT NULL,
  byte_off   INTEGER NOT NULL,
  byte_len   INTEGER NOT NULL,
  role       TEXT NOT NULL,
  ts         TEXT,
  PRIMARY KEY(session_id, msg_idx)
) WITHOUT ROWID;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  session_id UNINDEXED,
  role       UNINDEXED,
  ts         UNINDEXED,
  msg_idx    UNINDEXED,
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS usage_daily(
  day          TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  model        TEXT NOT NULL,
  in_tok       INTEGER NOT NULL DEFAULT 0,
  out_tok      INTEGER NOT NULL DEFAULT 0,
  cache_w5_tok INTEGER NOT NULL DEFAULT 0,
  cache_w1h_tok INTEGER NOT NULL DEFAULT 0,
  cache_r_tok  INTEGER NOT NULL DEFAULT 0,
  cost_usd     REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(day, session_id, model)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS usage_day ON usage_daily(day);
"""


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def connect() -> sqlite3.Connection:
    """Per-thread connection to the index."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.DB_PATH))
        _configure(conn)
        _local.conn = conn
    return conn


def close() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def _schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else None
    except sqlite3.Error:
        return None


def init(reset: bool = False) -> sqlite3.Connection:
    """Open the index, rebuilding from scratch on version mismatch or damage."""
    config.APP_SUPPORT.mkdir(parents=True, exist_ok=True)

    if reset:
        _nuke()

    conn = connect()
    version = _schema_version(conn)
    if version is not None and version != config.SCHEMA_VERSION:
        close()
        _nuke()
        conn = connect()
        version = None

    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                     (str(config.SCHEMA_VERSION),))
        conn.commit()
    except sqlite3.DatabaseError:
        # Corrupt file: it is only a cache, so start over rather than fail to launch.
        close()
        _nuke()
        conn = connect()
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                     (str(config.SCHEMA_VERSION),))
        conn.commit()
    return conn


def _nuke() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(config.DB_PATH) + suffix)
        p.unlink(missing_ok=True)


def get_meta(key: str, default=None):
    row = connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value) -> None:
    conn = connect()
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))
    conn.commit()

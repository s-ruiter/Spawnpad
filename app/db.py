import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    token_id TEXT NOT NULL,
    token_secret TEXT NOT NULL,
    verify_tls INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    node TEXT NOT NULL,
    vm_name TEXT NOT NULL,
    vmid INTEGER,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL,
    batch_id TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    server_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    step TEXT NOT NULL,
    state TEXT NOT NULL,
    message TEXT NOT NULL,
    progress REAL
);
CREATE TABLE IF NOT EXISTS last_used (
    server_id INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
    datastore TEXT,
    bridge TEXT,
    vlan INTEGER
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # migratie voor databases van vóór batches
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
    conn.commit()

import stat

from app import db


def test_schema_creates_tables(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"servers", "jobs", "job_events", "last_used"} <= names


def test_db_file_is_private(tmp_path):
    p = tmp_path / "x.db"
    c = db.connect(p)
    c.close()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600

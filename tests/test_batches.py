import sqlite3
import threading

from app import db
from tests.conftest import make_qcow2
from tests.test_deploy import FakeClient
from tests.test_jobs import SERVER, cfgbody


def upload(client, name="fgt.qcow2", virtual=1024**3):
    return client.post("/api/uploads", files={"file": (name, make_qcow2(virtual))}).json()


def item(up, count=1, **cfg):
    return {"upload_id": up["upload_id"], "candidate": up["candidates"][0]["name"], "count": count, "config": cfgbody(**cfg)}


def start(client, app, fake=None):
    sid = client.post("/api/servers", json=SERVER).json()["id"]
    app.state.client_factory = lambda row: fake or FakeClient()
    return sid


def wait_all(app, jobs):
    for j in jobs:
        app.state.job_manager.wait(j["id"])


def test_count_creates_numbered_jobs(client, app):
    sid = start(client, app)
    r = client.post("/api/batches", json={"server_id": sid, "items": [item(upload(client), count=3, name="fgt")]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert [j["vm_name"] for j in body["jobs"]] == ["fgt-1", "fgt-2", "fgt-3"]
    wait_all(app, body["jobs"])
    batch = client.get(f"/api/batches/{body['id']}").json()
    assert [j["status"] for j in batch["jobs"]] == ["done"] * 3
    assert all(j["step"] == "klaar" for j in batch["jobs"])


def test_single_count_keeps_plain_name(client, app):
    sid = start(client, app)
    body = client.post("/api/batches", json={"server_id": sid, "items": [item(upload(client), name="fmg")]}).json()
    assert [j["vm_name"] for j in body["jobs"]] == ["fmg"]


def test_different_files_in_one_batch(client, app):
    sid = start(client, app)
    fmg, fgt = upload(client, "fmg.qcow2"), upload(client, "fgt.qcow2")
    r = client.post("/api/batches", json={"server_id": sid, "items": [
        item(fmg, name="fmg", extra_disks=[{"size_gb": 100}]), item(fgt, count=2, name="fgt"),
    ]})
    body = r.json()
    assert [j["vm_name"] for j in body["jobs"]] == ["fmg", "fgt-1", "fgt-2"]
    wait_all(app, body["jobs"])
    batch = client.get(f"/api/batches/{body['id']}").json()
    assert all(j["status"] == "done" for j in batch["jobs"])
    uploads = app.state.settings.temp_dir / "uploads"
    assert list(uploads.iterdir()) == []


def test_shared_upload_survives_until_last_job_finishes(client, app):
    gate, pick = threading.Event(), threading.Lock()
    seen = []

    class Gated(FakeClient):
        def upload_import(self, node, storage, path, filename, on_progress):
            with pick:  # atomair bepalen welke job "eerst" is
                blocked = not seen
                seen.append(filename)
            if blocked:
                gate.wait(10)
            return super().upload_import(node, storage, path, filename, on_progress)

    sid = start(client, app, Gated())
    up = upload(client)
    body = client.post("/api/batches", json={"server_id": sid, "items": [item(up, count=2)]}).json()
    j1, j2 = body["jobs"]
    app.state.job_manager.wait(j2["id"])  # tweede job is klaar, de eerste hangt nog
    d = app.state.settings.temp_dir / "uploads" / up["upload_id"]
    assert d.exists()
    gate.set()
    app.state.job_manager.wait(j1["id"])
    assert not d.exists()


def test_batch_validation_is_all_or_nothing(client, app):
    sid = start(client, app)
    good, bad = upload(client, "a.qcow2"), upload(client, "b.qcow2", virtual=3 * 1024**3)
    r = client.post("/api/batches", json={"server_id": sid, "items": [item(good, name="a"), item(bad, name="b", disk_gb=1)]})
    assert r.status_code == 422
    assert client.get("/api/deployments").json() == []
    assert (app.state.settings.temp_dir / "uploads" / good["upload_id"]).exists()


def test_explicit_vmid_not_allowed_with_count(client, app):
    sid = start(client, app)
    r = client.post("/api/batches", json={"server_id": sid, "items": [item(upload(client), count=2, vmid=100)]})
    assert r.status_code == 422 and "VMID" in r.json()["detail"]


def test_count_limits(client, app):
    sid = start(client, app)
    up = upload(client)
    assert client.post("/api/batches", json={"server_id": sid, "items": [item(up, count=0)]}).status_code == 422
    assert client.post("/api/batches", json={"server_id": sid, "items": [item(up, count=21)]}).status_code == 422
    assert client.post("/api/batches", json={"server_id": sid, "items": []}).status_code == 422


def test_invalid_vm_name_is_rejected(client, app):
    sid = start(client, app)
    r = client.post("/api/batches", json={"server_id": sid, "items": [item(upload(client), name="fgt lab_1")]})
    assert r.status_code == 422 and "naam" in r.json()["detail"].lower()


def test_unknown_upload_is_404(client, app):
    sid = start(client, app)
    bogus = {"upload_id": "a" * 32, "candidates": [{"name": "x.qcow2"}]}
    assert client.post("/api/batches", json={"server_id": sid, "items": [item(bogus)]}).status_code == 404


def test_history_includes_batch_id(client, app):
    sid = start(client, app)
    body = client.post("/api/batches", json={"server_id": sid, "items": [item(upload(client), count=2)]}).json()
    wait_all(app, body["jobs"])
    assert {j["batch_id"] for j in client.get("/api/deployments").json()} == {body["id"]}


def test_migration_adds_batch_id_to_existing_db(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT, url TEXT, token_id TEXT, token_secret TEXT, verify_tls INTEGER);
        CREATE TABLE jobs (id TEXT PRIMARY KEY, server_id INTEGER, node TEXT, vm_name TEXT, vmid INTEGER,
            config_json TEXT, status TEXT, error TEXT, created_at REAL, finished_at REAL);
        INSERT INTO jobs(id,vm_name,status,created_at,config_json) VALUES('old1','x','done',1.0,'{}');
    """)
    c.commit()
    c.close()
    conn = db.connect(p)
    db.init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "batch_id" in cols
    assert conn.execute("SELECT id FROM jobs").fetchone()["id"] == "old1"
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='batches'").fetchone()

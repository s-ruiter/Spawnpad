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
    ids = [j["id"] for j in body["jobs"]]
    import time as _t
    for _ in range(200):  # wachten tot een van de twee jobs bij de upload vastzit
        if seen:
            break
        _t.sleep(0.01)
    blocked = next(i for i in ids if f"proxlaber-{i}.qcow2" == seen[0])
    other = next(i for i in ids if i != blocked)
    app.state.job_manager.wait(other)  # de andere job is klaar, de eerste hangt nog
    d = app.state.settings.temp_dir / "uploads" / up["upload_id"]
    assert d.exists()
    gate.set()
    app.state.job_manager.wait(blocked)
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


def iso_user_data(iso_bytes: bytes) -> str:
    import io

    import pycdlib

    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(iso_bytes))
    out = io.BytesIO()
    iso.get_file_from_iso_fp(out, rr_path="/openstack/latest/user_data")
    iso.close()
    return out.getvalue().decode("ascii")


def day0(**kw):
    base = {"product": "fortigate", "hostname": "{name}", "admin_password": "Sup3rS3cret",
            "network": {"mode": "static", "ip": "10.0.0.11/24", "gateway": "10.0.0.1", "dns": ["1.1.1.1"]},
            "custom_cli": "config system global\n set timezone 26\nend", "tokens": ["AAAA1111", "BBBB2222"]}
    base.update(kw)
    return base


def ci_item(up, count=2, d0=None, **cfg):
    it = item(up, count=count, iso_storage="local", **cfg)
    it["day0"] = d0 or day0()
    return it


def test_day0_is_rendered_per_vm_and_attached(client, app):
    fake = FakeClient()
    sid = start(client, app, fake)
    body = client.post("/api/batches", json={"server_id": sid, "items": [ci_item(upload(client), name="fgt")]}).json()
    wait_all(app, body["jobs"])
    texts = [iso_user_data(data) for _, _, _, data in fake.iso_uploads]
    assert len(texts) == 2
    first = next(t for t in texts if "10.0.0.11" in t)
    second = next(t for t in texts if "10.0.0.12" in t)
    assert "AAAA1111" in first and "BBBB2222" in second
    assert "set hostname fgt-1" in first and "set hostname fgt-2" in second
    assert "set timezone 26" in first
    assert all(p.get("ide2", "").endswith("media=cdrom") for p in fake.created_params)


def test_token_count_must_match_vm_count(client, app):
    sid = start(client, app)
    r = client.post("/api/batches", json={"server_id": sid, "items": [ci_item(upload(client), count=3)]})
    assert r.status_code == 422 and "tokens" in r.json()["detail"]


def test_day0_requires_iso_storage(client, app):
    sid = start(client, app)
    it = item(upload(client), count=2)
    it["day0"] = day0()
    r = client.post("/api/batches", json={"server_id": sid, "items": [it]})
    assert r.status_code == 422 and "ISO" in r.json()["detail"]


def test_static_ip_outside_subnet_is_rejected_up_front(client, app):
    sid = start(client, app)
    d0 = day0(network={"mode": "static", "ip": "10.0.0.254/24", "gateway": "10.0.0.1", "dns": []}, tokens=[])
    r = client.post("/api/batches", json={"server_id": sid, "items": [ci_item(upload(client), count=3, d0=d0)]})
    assert r.status_code == 422 and "subnet" in r.json()["detail"]
    assert client.get("/api/deployments").json() == []


def test_secrets_are_not_persisted_or_returned(client, app):
    sid = start(client, app)
    body = client.post("/api/batches", json={"server_id": sid, "items": [ci_item(upload(client), name="fgt")]}).json()
    wait_all(app, body["jobs"])
    rows = app.state.conn.execute("SELECT config_json FROM jobs").fetchall()
    dump = " ".join(r["config_json"] for r in rows)
    for secret in ("Sup3rS3cret", "AAAA1111", "BBBB2222", "user_data"):
        assert secret not in dump
    api = client.get(f"/api/batches/{body['id']}").text + client.get("/api/deployments").text
    for secret in ("Sup3rS3cret", "AAAA1111", "BBBB2222"):
        assert secret not in api


def test_recovery_deletes_the_iso_volume(client, app):
    import json
    import time

    fake = FakeClient()
    sid = start(client, app, fake)
    up = upload(client)
    conn = app.state.conn
    conn.execute(
        "INSERT INTO jobs(id,server_id,node,vm_name,vmid,config_json,status,created_at) VALUES('j9',?,?,?,?,?,?,?)",
        (sid, "pve1", "fgt", None,
         json.dumps({"config": cfgbody(iso_storage="isostore"), "candidate": "fgt.qcow2", "upload_id": up["upload_id"]}), "running", time.time()),
    )
    conn.commit()
    app.state.job_manager.recover_on_startup()
    assert "isostore:iso/proxlaber-j9.iso" in fake.volumes_deleted


def test_preview_masks_secrets(client, app):
    r = client.post("/api/cloudinit/preview", json={"day0": day0(), "name": "fgt", "n": 1, "count": 2})
    assert r.status_code == 200
    text = r.json()["user_data"]
    assert "set hostname fgt-1" in text or "set hostname fgt" in text
    assert "Sup3rS3cret" not in text and "AAAA1111" not in text and "BBBB2222" not in text
    assert "LICENSE-TOKEN" in text


def test_preview_reports_errors(client, app):
    r = client.post("/api/cloudinit/preview", json={"day0": day0(tokens=["AAAA1111"]), "name": "fgt", "n": 1, "count": 2})
    assert r.status_code == 422


def test_node_storages_list_includes_iso_storages(client, app):
    class C(FakeClient):
        def storages(self, node): return [{"name": "local-lvm", "avail": 1}]
        def import_storages(self, node): return ["local"]
        def iso_storages(self, node): return ["local"]

    sid = start(client, app, C())
    assert client.get(f"/api/servers/{sid}/nodes/pve1/storages").json()["iso_storages"] == ["local"]

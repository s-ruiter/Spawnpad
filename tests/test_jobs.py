import json
import threading
import time

import pytest

from app.proxmox import ProxmoxError
from tests.conftest import make_qcow2
from tests.test_deploy import FakeClient

SERVER = {"name": "pve1", "url": "https://pve1:8006", "token_id": "r@pam!t", "token_secret": "s", "verify_tls": False}


def cfgbody(**kw):
    c = dict(
        name="fgt", node="pve1", datastore="local-lvm", import_storage="local", disk_gb=4,
        nics=[{"bridge": "vmbr0", "vlan": 10}],
    )
    c.update(kw)
    return c


def setup(client, app, fake, virtual=3 * 1024**3):
    sid = client.post("/api/servers", json=SERVER).json()["id"]
    app.state.client_factory = lambda row: fake
    up = client.post("/api/uploads", files={"file": ("fgt.qcow2", make_qcow2(virtual))}).json()
    return sid, up


def deploy(client, sid, up, **kw):
    return client.post("/api/deployments", json={
        "server_id": sid, "upload_id": up["upload_id"], "candidate": up["candidates"][0]["name"],
        "config": cfgbody(**kw),
    })


def test_start_rejects_disk_smaller_than_virtual(client, app):
    sid, up = setup(client, app, FakeClient())
    r = deploy(client, sid, up, disk_gb=1)
    assert r.status_code == 422


def test_deployment_runs_to_done_and_cleans_upload(client, app):
    fake = FakeClient()
    sid, up = setup(client, app, fake)
    jid = deploy(client, sid, up).json()["id"]
    app.state.job_manager.wait(jid)
    job = client.get(f"/api/deployments/{jid}").json()
    assert job["status"] == "done" and job["vmid"] == 105
    assert job["events"][-1]["step"] == "klaar"
    assert not (app.state.settings.temp_dir / "uploads" / up["upload_id"]).exists()


def test_failed_deployment_records_error(client, app):
    sid, up = setup(client, app, FakeClient(fail_at="create_vm"))
    jid = deploy(client, sid, up).json()["id"]
    app.state.job_manager.wait(jid)
    job = client.get(f"/api/deployments/{jid}").json()
    assert job["status"] == "failed" and "create_vm kapot" in job["error"]


def test_history_lists_newest_first(client, app):
    sid, up = setup(client, app, FakeClient())
    j1 = deploy(client, sid, up).json()["id"]
    app.state.job_manager.wait(j1)
    up2 = client.post("/api/uploads", files={"file": ("b.qcow2", make_qcow2(1024**3))}).json()
    j2 = deploy(client, sid, up2, name="second").json()["id"]
    app.state.job_manager.wait(j2)
    ids = [j["id"] for j in client.get("/api/deployments").json()]
    assert ids == [j2, j1]


def test_events_replayed_after_reconnect(client, app):
    sid, up = setup(client, app, FakeClient())
    jid = deploy(client, sid, up).json()["id"]
    app.state.job_manager.wait(jid)
    all_ev = client.get(f"/api/deployments/{jid}").json()["events"]
    cut = all_ev[2]["id"]
    with client.stream("GET", f"/api/deployments/{jid}/events", headers={"Last-Event-ID": str(cut)}) as r:
        text = "".join(r.iter_text())
    ids = [int(l.split(": ")[1]) for l in text.splitlines() if l.startswith("id: ")]
    assert ids == [e["id"] for e in all_ev if e["id"] > cut]
    payloads = [json.loads(l[6:]) for l in text.splitlines() if l.startswith("data: ")]
    assert payloads[-1]["step"] == "klaar"


def test_last_used_is_saved_after_start(client, app):
    sid, up = setup(client, app, FakeClient())
    jid = deploy(client, sid, up).json()["id"]
    app.state.job_manager.wait(jid)
    assert client.get(f"/api/servers/{sid}/last-used").json() == {"datastore": "local-lvm", "bridge": "vmbr0", "vlan": 10}


def test_recover_marks_running_jobs_failed_and_cleans_up(client, app):
    fake = FakeClient()
    sid, up = setup(client, app, fake)
    conn = app.state.conn
    conn.execute(
        "INSERT INTO jobs(id,server_id,node,vm_name,vmid,config_json,status,created_at) VALUES('j9',?,?,?,?,?,?,?)",
        (sid, "pve1", "fgt", 120, json.dumps({"config": cfgbody(), "candidate": "fgt.qcow2", "upload_id": up["upload_id"]}), "running", time.time()),
    )
    conn.commit()
    app.state.job_manager.recover_on_startup()
    job = client.get("/api/deployments/j9").json()
    assert job["status"] == "failed" and "herstart" in job["error"]
    assert "delete_vm" in fake.calls and "delete_volume" in fake.calls
    assert not (app.state.settings.temp_dir / "uploads" / up["upload_id"]).exists()


def test_two_parallel_deployments_get_distinct_vmids(client, app):
    barrier = threading.Barrier(2)
    used, lock = set(), threading.Lock()
    first = threading.local()

    class C(FakeClient):
        def next_vmid(self):
            if not getattr(first, "done", False):
                first.done = True
                barrier.wait(timeout=5)
            with lock:
                return min(i for i in range(100, 200) if i not in used)

        def create_vm(self, node, vmid, name, params):
            with lock:
                if vmid in used:
                    raise ProxmoxError(f"VM {vmid} already exists", status=500)
                used.add(vmid)
            return "UPID:c"

    fake = C()
    sid, up1 = setup(client, app, fake)
    up2 = client.post("/api/uploads", files={"file": ("b.qcow2", make_qcow2(1024**3))}).json()
    j1 = deploy(client, sid, up1).json()["id"]
    j2 = deploy(client, sid, up2, name="b").json()["id"]
    app.state.job_manager.wait(j1)
    app.state.job_manager.wait(j2)
    v = {client.get(f"/api/deployments/{j}").json()["vmid"] for j in (j1, j2)}
    assert v == {100, 101}


def test_lookups(client, app):
    class C(FakeClient):
        def nodes(self): return ["pve1"]
        def storages(self, node): return [{"name": "local-lvm", "avail": 1}]
        def import_storages(self, node): return ["local"]
        def bridges(self, node): return ["vmbr0"]

    sid, _ = setup(client, app, C())
    assert client.get(f"/api/servers/{sid}/nodes").json() == ["pve1"]
    st = client.get(f"/api/servers/{sid}/nodes/pve1/storages").json()
    assert st["storages"][0]["name"] == "local-lvm" and st["import_storages"] == ["local"]
    assert client.get(f"/api/servers/{sid}/nodes/pve1/bridges").json() == ["vmbr0"]


def test_vmid_not_stored_before_create(client, app):
    sid, up = setup(client, app, FakeClient(fail_at="create_vm"))
    jid = deploy(client, sid, up, vmid=100).json()["id"]
    app.state.job_manager.wait(jid)
    assert client.get(f"/api/deployments/{jid}").json()["vmid"] is None


def _running_job(app, sid, up, vmid=120):
    conn = app.state.conn
    conn.execute(
        "INSERT INTO jobs(id,server_id,node,vm_name,vmid,config_json,status,created_at) VALUES('j9',?,?,?,?,?,?,?)",
        (sid, "pve1", "fgt", vmid,
         json.dumps({"config": cfgbody(), "candidate": "fgt.qcow2", "upload_id": up["upload_id"]}), "running", time.time()),
    )
    conn.commit()


def test_recovery_does_not_delete_foreign_vm(client, app):
    fake = FakeClient()
    fake.vm_tag_value = "somebody-else"
    sid, up = setup(client, app, fake)
    _running_job(app, sid, up)
    app.state.job_manager.recover_on_startup()
    assert "delete_vm" not in fake.calls


def test_recovery_deletes_the_job_specific_volume(client, app):
    fake = FakeClient()
    sid, up = setup(client, app, fake)
    _running_job(app, sid, up)
    app.state.job_manager.recover_on_startup()
    assert fake.volumes_deleted == ["local:import/proxlaber-j9.qcow2"]


def test_taken_vmid_is_rejected_before_job_and_keeps_upload(client, app):
    sid, up = setup(client, app, FakeClient(free=False))
    r = deploy(client, sid, up, vmid=100)
    assert r.status_code == 422 and "100" in r.json()["detail"]
    assert (app.state.settings.temp_dir / "uploads" / up["upload_id"]).exists()
    assert client.get("/api/deployments").json() == []

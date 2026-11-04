import io
import socket
import threading
import time
import zipfile

import httpx
import pytest
import uvicorn

from scripts.fake_proxmox import create_fake
from tests.conftest import make_qcow2


@pytest.fixture
def fake_server():
    started = []

    def start(**kw):
        fake = create_fake(**kw)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="warning"))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{port}/_state")
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        started.append((server, t))
        return f"http://127.0.0.1:{port}", fake.state.fake

    yield start
    for server, t in started:
        server.should_exit = True
        t.join(5)


def _deploy(client, url, vmname="fgt-e2e"):
    sid = client.post("/api/servers", json={
        "name": "fake", "url": url, "token_id": "root@pam!t", "token_secret": "x", "verify_tls": True,
    }).json()["id"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fortios.qcow2", make_qcow2(1024**3, pad=2 * 1024 * 1024))
        z.writestr("README.txt", "x")
    up = client.post("/api/uploads", files={"file": ("FGT.zip", buf.getvalue())}).json()
    r = client.post("/api/deployments", json={
        "server_id": sid, "upload_id": up["upload_id"], "candidate": "fortios.qcow2",
        "config": {
            "name": vmname, "node": "pve1", "datastore": "local-lvm", "import_storage": "local", "disk_gb": 2,
            "nics": [{"bridge": "vmbr0", "vlan": 10}, {"bridge": "vmbr1"}],
            "extra_disks": [{"size_gb": 100}],
        },
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_full_deploy_against_fake_proxmox(client, app, fake_server):
    url, fake_state = fake_server()
    jid = _deploy(client, url)
    app.state.job_manager.wait(jid, timeout=30)
    job = client.get(f"/api/deployments/{jid}").json()
    assert job["status"] == "done", job
    vm = fake_state["vms"][job["vmid"]]
    assert vm["scsi1"] == "local-lvm:100" and vm["cpu"] == "host" and vm["net0"] == "virtio,bridge=vmbr0,tag=10" and vm["net1"] == "virtio,bridge=vmbr1"
    assert fake_state["uploads"][0]["bytes"] > 2 * 1024 * 1024
    assert "resize_disk" in fake_state["log"] and fake_state["log"][-1] == "delete_volume"


def test_failed_create_cleans_up(client, app, fake_server):
    url, fake_state = fake_server(fail_at="create")
    jid = _deploy(client, url)
    app.state.job_manager.wait(jid, timeout=30)
    job = client.get(f"/api/deployments/{jid}").json()
    assert job["status"] == "failed" and "create failed" in job["error"]
    assert "delete_volume" in fake_state["log"] and fake_state["vms"] == {}


def test_day0_cloudinit_against_fake_proxmox(client, app, fake_server):
    url, fake_state = fake_server()
    sid = client.post("/api/servers", json={"name": "fake", "url": url, "token_id": "root@pam!t", "token_secret": "x", "verify_tls": True}).json()["id"]
    up = client.post("/api/uploads", files={"file": ("fgt.qcow2", make_qcow2(1024**3, pad=1024))}).json()
    r = client.post("/api/batches", json={"server_id": sid, "items": [{
        "upload_id": up["upload_id"], "candidate": "fgt.qcow2", "count": 2,
        "config": {"name": "fgt", "node": "pve1", "datastore": "local-lvm", "import_storage": "local", "iso_storage": "local",
                   "disk_gb": 2, "nics": [{"bridge": "vmbr0"}]},
        "day0": {"product": "fortigate", "network": {"mode": "static", "ip": "10.0.0.11/24", "gateway": "10.0.0.1"},
                 "tokens": ["AAAA1111", "BBBB2222"]},
    }]})
    assert r.status_code == 201, r.text
    jobs = r.json()["jobs"]
    for j in jobs:
        app.state.job_manager.wait(j["id"], timeout=30)
    batch = client.get(f"/api/batches/{r.json()['id']}").json()
    assert [j["status"] for j in batch["jobs"]] == ["done", "done"], batch
    assert sorted(u["content"] for u in fake_state["uploads"]) == ["import", "import", "iso", "iso"]
    assert all(v["ide2"].endswith(",media=cdrom") and "/proxlaber-" in v["ide2"] for v in fake_state["vms"].values())

import json

import httpx
import pytest

from app.proxmox import ProxmoxClient, ProxmoxError, build_vm_params
from app.schemas import NicConfig, VmConfig


def make(handler):
    return ProxmoxClient(
        "https://pve:8006", "root@pam!t", "sekret", verify_tls=False,
        transport=httpx.MockTransport(handler), poll_interval=0,
    )


def ok(data):
    return httpx.Response(200, json={"data": data})


def test_auth_header_uses_api_token():
    seen = {}

    def h(req):
        seen["auth"] = req.headers["Authorization"]
        return ok({"version": "9.0.3"})

    make(h)._req("GET", "/version")
    assert seen["auth"] == "PVEAPIToken=root@pam!t=sekret"


def test_check_rejects_pve8():
    c = make(lambda req: ok({"version": "8.4.1"}))
    with pytest.raises(ProxmoxError):
        c.check()


def test_check_warns_without_import_storage():
    def h(req):
        if req.url.path.endswith("/version"):
            return ok({"version": "9.0.3"})
        return ok([])

    res = make(h).check()
    assert res["version"] == "9.0.3" and res["import_storages"] == []
    assert "import" in res["warning"]


def _cfg():
    return VmConfig(
        name="fgt", node="pve1", datastore="local-lvm", import_storage="local", disk_gb=2,
        nics=[NicConfig(bridge="vmbr0", vlan=10), NicConfig(bridge="vmbr1")],
    )


def test_build_vm_params_fortigate_defaults():
    p = build_vm_params(_cfg(), "local:import/x.qcow2")
    assert p["cpu"] == "host" and p["bios"] == "seabios"
    assert p["scsi0"] == "local-lvm:0,import-from=local:import/x.qcow2"
    assert p["net0"] == "virtio,bridge=vmbr0,tag=10"
    assert p["net1"] == "virtio,bridge=vmbr1"
    assert p["start"] == 0 and p["serial0"] == "socket"


def test_wait_task_raises_on_failed_exitstatus():
    c = make(lambda req: ok({"status": "stopped", "exitstatus": "boom"}))
    with pytest.raises(ProxmoxError):
        c.wait_task("pve1", "UPID:x")


def test_wait_task_returns_on_ok():
    c = make(lambda req: ok({"status": "stopped", "exitstatus": "OK"}))
    c.wait_task("pve1", "UPID:x")


def test_upload_reports_progress(tmp_path):
    f = tmp_path / "a.qcow2"
    f.write_bytes(b"x" * (3 * 1024 * 1024))
    seen = []

    def h(req):
        body = req.read()
        assert b'name="content"' in body and b"import" in body
        assert b'filename="a.qcow2"' in body
        return ok("UPID:upload")

    upid = make(h).upload_import("pve1", "local", f, "a.qcow2", lambda frac, bps: seen.append(frac))
    assert upid == "UPID:upload"
    assert seen and 0 < seen[0] <= 1 and seen[-1] == 1


def test_errors_wrapped():
    def h(req):
        raise httpx.ConnectError("down")

    with pytest.raises(ProxmoxError):
        make(h)._req("GET", "/version")


def test_http_error_has_status():
    c = make(lambda req: httpx.Response(401, json={"errors": "no"}))
    with pytest.raises(ProxmoxError) as e:
        c._req("GET", "/version")
    assert e.value.status == 401

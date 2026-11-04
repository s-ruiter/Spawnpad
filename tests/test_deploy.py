import pytest

from app.deploy import STEPS, run_deployment
from app.proxmox import ProxmoxError
from app.schemas import NicConfig, VmConfig
from tests.conftest import make_qcow2


class FakeClient:
    def __init__(self, fail_at=None, free=True, fail_cleanup=False):
        self.calls = []
        self.uploaded_names = []
        self.created_params = []
        self.volumes_deleted = []
        self.resize_upid = None
        self.vm_tag_value = "proxlaber-j9"
        self.fail_at = fail_at
        self.free = free
        self.fail_cleanup = fail_cleanup

    def _do(self, name, *a):
        self.calls.append(name)
        if self.fail_at == name:
            raise ProxmoxError(f"{name} kapot")

    def close(self):
        pass

    def vmid_free(self, vmid):
        self.calls.append("vmid_free")
        return self.free

    def next_vmid(self):
        self.calls.append("next_vmid")
        return 105

    def upload_import(self, node, storage, path, filename, on_progress):
        self._do("upload_import")
        self.uploaded_names.append(filename)
        on_progress(0.5, 1e6)
        on_progress(1.0, 1e6)
        return "UPID:up"

    def create_vm(self, node, vmid, name, params):
        self._do("create_vm")
        self.created_params.append(params)
        return "UPID:create"

    def wait_task(self, node, upid, on_tick=None, timeout=3600):
        self._do("wait_task")

    def resize_disk(self, node, vmid, disk, size_gb):
        self._do("resize_disk")
        self.resized = size_gb
        return self.resize_upid

    def start_vm(self, node, vmid):
        self._do("start_vm")
        return "UPID:start"

    def delete_vm(self, node, vmid):
        self.calls.append("delete_vm")
        if getattr(self, "delete_vm_failures", 0) > 0:
            self.delete_vm_failures -= 1
            raise ProxmoxError("VM is locked (create)")
        if self.fail_cleanup:
            raise ProxmoxError("cleanup kapot")

    def vm_tags(self, node, vmid):
        self.calls.append("vm_tags")
        return self.vm_tag_value

    def delete_volume(self, node, storage, volid):
        self.calls.append("delete_volume")
        self.volumes_deleted.append(volid)
        if self.fail_cleanup:
            raise ProxmoxError("cleanup kapot")


def cfg(**kw):
    base = dict(
        name="fgt", node="pve1", datastore="local-lvm", import_storage="local", disk_gb=2,
        nics=[NicConfig(bridge="vmbr0")],
    )
    base.update(kw)
    return VmConfig(**base)


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "fortios.qcow2"
    p.write_bytes(make_qcow2(1024**3))
    return p


class Events:
    def __init__(self):
        self.items = []

    def __call__(self, step, state, message, progress=None):
        self.items.append((step, state, message, progress))

    def states(self):
        out = {}
        for step, state, *_ in self.items:
            out[step] = state
        return out


def test_happy_path_order(image):
    c, ev = FakeClient(), Events()
    vmid = run_deployment("j1", c, cfg(), image, ev)
    assert vmid == 105
    order = [x for x in c.calls if x in ("upload_import", "create_vm", "resize_disk", "delete_volume")]
    assert order == ["upload_import", "create_vm", "resize_disk", "delete_volume"]
    assert ev.states() == {s: "done" for s in STEPS}


def test_vmid_taken_fails_before_upload(image):
    c, ev = FakeClient(free=False), Events()
    with pytest.raises(ProxmoxError):
        run_deployment("j1", c, cfg(vmid=100), image, ev)
    assert "upload_import" not in c.calls and "delete_vm" not in c.calls


def test_upload_failure_cleans_up_volume_and_no_vm_delete(image):
    c, ev = FakeClient(fail_at="upload_import"), Events()
    with pytest.raises(ProxmoxError):
        run_deployment("j1", c, cfg(), image, ev)
    assert "delete_volume" in c.calls and "delete_vm" not in c.calls
    assert ev.states()["uploaden"] == "failed"


def test_create_failure_deletes_vm_and_volume(image):
    c, ev = FakeClient(fail_at="create_vm"), Events()
    with pytest.raises(ProxmoxError):
        run_deployment("j1", c, cfg(), image, ev)
    # create_vm zelf faalde: er is geen VM om te verwijderen, wel het geüploade volume
    assert "delete_volume" in c.calls


def test_failed_import_task_deletes_vm_and_volume(image):
    class C(FakeClient):
        n = 0

        def wait_task(self, node, upid, on_tick=None, timeout=3600):
            self.n += 1
            if self.n == 2:  # eerste = upload, tweede = create/import
                raise ProxmoxError("import mislukt")

    c, ev = C(), Events()
    with pytest.raises(ProxmoxError):
        run_deployment("j1", c, cfg(), image, ev)
    assert "delete_vm" in c.calls and "delete_volume" in c.calls


def test_cleanup_error_does_not_mask_original_error(image):
    c, ev = FakeClient(fail_at="resize_disk", fail_cleanup=True), Events()
    with pytest.raises(ProxmoxError, match="resize_disk kapot"):
        run_deployment("j1", c, cfg(), image, ev, cleanup_delay=0)


def test_no_resize_when_disk_equals_virtual_size(image):
    c, ev = FakeClient(), Events()
    run_deployment("j1", c, cfg(disk_gb=1), image, ev)
    assert "resize_disk" not in c.calls


def test_start_after_starts_vm(image):
    c, ev = FakeClient(), Events()
    run_deployment("j1", c, cfg(start_after=True), image, ev)
    assert "start_vm" in c.calls


def test_upload_progress_is_emitted(image):
    c, ev = FakeClient(), Events()
    run_deployment("j1", c, cfg(), image, ev)
    progress = [p for step, st, m, p in ev.items if step == "uploaden" and p is not None]
    assert 0.5 in progress and 1.0 in progress


def test_vmid_conflict_retries_with_next_vmid(image):
    class C(FakeClient):
        ids = iter([100, 101])
        attempts = 0

        def next_vmid(self):
            return next(self.ids)

        def create_vm(self, node, vmid, name, params):
            self.attempts += 1
            if vmid == 100:
                raise ProxmoxError("VM 100 already exists", status=500)
            self.created = vmid
            return "UPID:c"

    c, ev = C(), Events()
    assert run_deployment("j1", c, cfg(), image, ev) == 101
    assert c.created == 101 and c.attempts == 2


def test_explicit_vmid_conflict_is_not_retried(image):
    class C(FakeClient):
        def create_vm(self, node, vmid, name, params):
            raise ProxmoxError("VM 100 already exists", status=500)

    c, ev = C(), Events()
    with pytest.raises(ProxmoxError):
        run_deployment("j1", c, cfg(vmid=100), image, ev)


def test_on_vmid_callback_reports_chosen_id(image):
    seen = []
    run_deployment("j1", FakeClient(), cfg(), image, Events(), on_vmid=seen.append)
    assert seen == [105]


def test_import_file_name_is_unique_per_job(image):
    a, b = FakeClient(), FakeClient()
    run_deployment("jobA", a, cfg(), image, Events(), cleanup_delay=0)
    run_deployment("jobB", b, cfg(), image, Events(), cleanup_delay=0)
    assert a.uploaded_names == ["proxlaber-jobA.qcow2"] and b.uploaded_names == ["proxlaber-jobB.qcow2"]
    assert a.volumes_deleted == ["local:import/proxlaber-jobA.qcow2"]


def test_vm_is_tagged_with_job_id(image):
    c = FakeClient()
    run_deployment("jobA", c, cfg(), image, Events(), cleanup_delay=0)
    assert c.created_params[0]["scsi0"].endswith("import-from=local:import/proxlaber-jobA.qcow2")
    assert c.created_params[0]["tags"] == "proxlaber-jobA"


def test_on_vmid_not_called_when_create_fails(image):
    seen = []
    c = FakeClient(fail_at="create_vm")
    with pytest.raises(ProxmoxError):
        run_deployment("j1", c, cfg(vmid=100), image, Events(), on_vmid=seen.append, cleanup_delay=0)
    assert seen == []


def test_resize_task_is_awaited_and_failure_surfaces(image):
    class C(FakeClient):
        def wait_task(self, node, upid, on_tick=None, timeout=3600):
            if upid == "UPID:resize":
                raise ProxmoxError("resize mislukt")

    c, ev = C(), Events()
    c.resize_upid = "UPID:resize"
    with pytest.raises(ProxmoxError, match="resize mislukt"):
        run_deployment("j1", c, cfg(), image, ev, cleanup_delay=0)
    assert ev.states()["resize"] == "failed" and "delete_vm" in c.calls


def test_cleanup_retries_locked_vm(image):
    c = FakeClient(fail_at="resize_disk")
    c.delete_vm_failures = 2
    with pytest.raises(ProxmoxError):
        run_deployment("j1", c, cfg(), image, Events(), cleanup_delay=0)
    assert c.calls.count("delete_vm") == 3


class RecordingLock:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append("lock_enter")

    def __exit__(self, *a):
        self.log.append("lock_exit")


def test_vmid_allocation_and_create_are_serialized_by_lock(image):
    class C(FakeClient):
        def __init__(self, log):
            super().__init__()
            self.log = log
            self.n = 0

        def next_vmid(self):
            self.log.append("next_vmid")
            return 105

        def create_vm(self, node, vmid, name, params):
            self.log.append("create_vm")
            return "UPID:c"

        def vmid_free(self, vmid):
            # VMID geldt pas als 'bezet' nadat Proxmox de config heeft aangemaakt: tweede poll
            self.n += 1
            self.log.append("vmid_free")
            return self.n < 2

        def upload_import(self, *a, **k):
            self.log.append("upload")
            return "UPID:up"

    log = []
    run_deployment("j1", C(log), cfg(), image, Events(), vmid_lock=RecordingLock(log), reserve_poll=0)
    # upload gebeurt buiten het slot; next_vmid, create_vm en het wachten op reservering erbinnen
    assert log == ["upload", "lock_enter", "next_vmid", "create_vm", "vmid_free", "vmid_free", "lock_exit"]


def test_without_lock_there_is_no_reservation_wait(image):
    c = FakeClient()
    run_deployment("j1", c, cfg(), image, Events())
    assert c.calls.count("vmid_free") == 0

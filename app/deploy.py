import time
from pathlib import Path
from typing import Callable

from . import images
from .proxmox import ProxmoxError, build_vm_params
from .schemas import VmConfig

STEPS = ["ontvangen", "uitgepakt", "uploaden", "vm_aanmaken", "disk_importeren", "resize", "netwerk_boot", "klaar"]

def import_filename(job_id: str) -> str:
    """Unieke naam op de import-storage, zodat gelijktijdige uitrollen elkaars bestand niet raken."""
    return f"proxlaber-{job_id}.qcow2"


def vm_tag(job_id: str) -> str:
    return f"proxlaber-{job_id}"


Emit = Callable[[str, str, str, "float | None"], None]


def run_deployment(
    job_id: str, client, cfg: VmConfig, image: Path, emit: Emit,
    on_vmid: "Callable[[int], None] | None" = None,
    cleanup_delay: float = 2.0,
) -> int:
    node = cfg.node
    remote_name = import_filename(job_id)
    volid = f"{cfg.import_storage}:import/{remote_name}"
    tag = vm_tag(job_id)
    step = "ontvangen"
    uploaded = False
    vm_created = False
    vmid: int | None = None

    def run(name: str, message: str = "") -> None:
        nonlocal step
        step = name
        emit(name, "running", message, None)

    def done(name: str, message: str = "") -> None:
        emit(name, "done", message, 1.0 if name == "uploaden" else None)

    try:
        emit("ontvangen", "done", "Bestand ontvangen", None)
        emit("uitgepakt", "done", image.name, None)

        if cfg.vmid is not None:
            if not client.vmid_free(cfg.vmid):
                raise ProxmoxError(f"VMID {cfg.vmid} is al in gebruik")
            vmid = cfg.vmid
        else:
            vmid = client.next_vmid()

        run("uploaden", "Uploaden naar Proxmox")
        last = {"p": -1.0}

        def on_progress(frac: float, bps: float) -> None:
            if frac - last["p"] >= 0.01 or frac >= 1.0:
                last["p"] = frac
                emit("uploaden", "running", f"{bps / 1e6:.1f} MB/s", frac)

        uploaded = True
        upid = client.upload_import(node, cfg.import_storage, image, remote_name, on_progress)
        client.wait_task(node, upid)
        done("uploaden", "Upload klaar")

        run("vm_aanmaken", f"VM {vmid} aanmaken")
        for attempt in range(3):
            try:
                upid = client.create_vm(node, vmid, cfg.name, build_vm_params(cfg, volid, tag))
                break
            except ProxmoxError as e:
                if cfg.vmid is None and attempt < 2 and "exist" in e.detail.lower():
                    vmid = client.next_vmid()
                    continue
                raise
        vm_created = True
        if on_vmid:
            on_vmid(vmid)
        done("vm_aanmaken")

        run("disk_importeren", f"Disk importeren naar {cfg.datastore}")
        client.wait_task(node, upid)
        done("disk_importeren")

        run("resize")
        if cfg.disk_gb > images.virtual_gb(image):
            resize_upid = client.resize_disk(node, vmid, "scsi0", cfg.disk_gb)
            if isinstance(resize_upid, str) and resize_upid.startswith("UPID"):
                client.wait_task(node, resize_upid)
            done("resize", f"Disk vergroot naar {cfg.disk_gb} GB")
        else:
            done("resize", "Geen vergroting nodig")

        run("netwerk_boot")
        if cfg.start_after:
            client.wait_task(node, client.start_vm(node, vmid))
        done("netwerk_boot")

        try:
            client.delete_volume(node, cfg.import_storage, volid)
        except ProxmoxError as e:
            emit("klaar", "running", f"Waarschuwing: import-bestand niet verwijderd: {e.detail}", None)

        emit("klaar", "done", f"VM {vmid} is klaar", None)
        return vmid
    except Exception as e:
        message = getattr(e, "detail", None) or str(e)
        if vm_created and vmid is not None:
            for attempt in range(5):  # de VM kan nog vergrendeld zijn door de lopende create/import-taak
                try:
                    client.delete_vm(node, vmid)
                    break
                except Exception:
                    if attempt < 4:
                        time.sleep(cleanup_delay)
        if uploaded:
            try:
                client.delete_volume(node, cfg.import_storage, volid)
            except Exception:
                pass
        emit(step, "failed", message, None)
        raise

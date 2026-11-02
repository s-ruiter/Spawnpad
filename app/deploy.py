from pathlib import Path
from typing import Callable

from . import images
from .proxmox import ProxmoxError, build_vm_params
from .schemas import VmConfig

STEPS = ["ontvangen", "uitgepakt", "uploaden", "vm_aanmaken", "disk_importeren", "resize", "netwerk_boot", "klaar"]

Emit = Callable[[str, str, str, "float | None"], None]


def run_deployment(
    job_id: str, client, cfg: VmConfig, image: Path, emit: Emit,
    on_vmid: "Callable[[int], None] | None" = None,
) -> int:
    node = cfg.node
    volid = f"{cfg.import_storage}:import/{image.name}"
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
        if on_vmid:
            on_vmid(vmid)

        run("uploaden", "Uploaden naar Proxmox")
        last = {"p": -1.0}

        def on_progress(frac: float, bps: float) -> None:
            if frac - last["p"] >= 0.01 or frac >= 1.0:
                last["p"] = frac
                emit("uploaden", "running", f"{bps / 1e6:.1f} MB/s", frac)

        uploaded = True
        upid = client.upload_import(node, cfg.import_storage, image, image.name, on_progress)
        client.wait_task(node, upid)
        done("uploaden", "Upload klaar")

        run("vm_aanmaken", f"VM {vmid} aanmaken")
        for attempt in range(3):
            try:
                upid = client.create_vm(node, vmid, cfg.name, build_vm_params(cfg, volid))
                break
            except ProxmoxError as e:
                if cfg.vmid is None and attempt < 2 and "exist" in e.detail.lower():
                    vmid = client.next_vmid()
                    if on_vmid:
                        on_vmid(vmid)
                    continue
                raise
        vm_created = True
        done("vm_aanmaken")

        run("disk_importeren", f"Disk importeren naar {cfg.datastore}")
        client.wait_task(node, upid)
        done("disk_importeren")

        run("resize")
        if cfg.disk_gb > images.virtual_gb(image):
            client.resize_disk(node, vmid, "scsi0", cfg.disk_gb)
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
            try:
                client.delete_vm(node, vmid)
            except Exception:
                pass
        if uploaded:
            try:
                client.delete_volume(node, cfg.import_storage, volid)
            except Exception:
                pass
        emit(step, "failed", message, None)
        raise

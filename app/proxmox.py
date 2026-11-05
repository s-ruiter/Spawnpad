import time
from pathlib import Path
from typing import Callable, Iterator

import httpx

from .schemas import VmConfig

CHUNK = 1024 * 1024


class ProxmoxError(Exception):
    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def build_vm_params(cfg: VmConfig, import_volid: str, tag: str | None = None, iso_volid: str | None = None) -> dict:
    params: dict = {
        "cpu": cfg.cpu_type,
        "cores": cfg.cores,
        "memory": cfg.memory_mb,
        "bios": cfg.bios,
        "machine": cfg.machine,
        "ostype": "l26",
        "scsi0": f"{cfg.datastore}:0,import-from={import_volid}",
        "boot": "order=scsi0",
        "start": 0,
    }
    if cfg.scsihw:  # leeg = Proxmox-standaard
        params["scsihw"] = cfg.scsihw
    if tag:
        params["tags"] = tag
    if iso_volid:
        params["ide2"] = f"{iso_volid},media=cdrom"  # config drive voor cloud-init
    for i, disk in enumerate(cfg.extra_disks, start=1):  # lege data-disks, bv. /var van FortiManager/FortiAnalyzer
        params[f"scsi{i}"] = f"{disk.datastore or cfg.datastore}:{disk.size_gb}"
    for i, nic in enumerate(cfg.nics):
        value = f"{nic.model},bridge={nic.bridge}"
        if nic.vlan is not None:
            value += f",tag={nic.vlan}"
        params[f"net{i}"] = value
    return params


class ProxmoxClient:
    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        verify_tls: bool = True,
        transport: httpx.BaseTransport | None = None,
        poll_interval: float = 1.0,
    ):
        self.poll_interval = poll_interval
        self._http = httpx.Client(
            base_url=url.rstrip("/") + "/api2/json",
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
            verify=verify_tls,
            transport=transport,
            timeout=httpx.Timeout(30.0, read=None, write=None),
        )

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, **kw):
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ProxmoxError(f"Connection to Proxmox failed: {e}") from e
        if r.status_code >= 400:
            # Proxmox zet de foutmelding bij 5xx vaak in de HTTP reason phrase en laat de body leeg ({"data": null})
            parts = [r.reason_phrase]
            try:
                body = r.json()
                parts.append(str(body.get("errors") or body.get("message") or body.get("detail") or ""))
            except ValueError:
                parts.append(r.text)
            detail = " — ".join(x for x in parts if x)
            raise ProxmoxError(f"Proxmox returned {r.status_code}: {detail}", status=r.status_code)
        return r.json().get("data")

    def check(self) -> dict:
        version = str(self._req("GET", "/version")["version"])
        if int(version.split(".")[0]) < 9:
            raise ProxmoxError(f"Proxmox VE {version} is not supported (minimum 9)")
        # GET /storage kent geen 'content'-filter; de storage-configs bevatten wel een content-lijst
        storages = [s for s in (self._req("GET", "/storage") or []) if "import" in str(s.get("content", "")).split(",")]
        result: dict = {"version": version, "import_storages": [s["storage"] for s in storages]}
        if not storages:
            result["warning"] = "No storage with content type import found"
        return result

    def nodes(self) -> list[str]:
        return sorted(n["node"] for n in self._req("GET", "/nodes"))

    def storages(self, node: str) -> list[dict]:
        rows = self._req("GET", f"/nodes/{node}/storage", params={"content": "images"}) or []
        return [{"name": s["storage"], "avail": s.get("avail")} for s in rows]

    def import_storages(self, node: str) -> list[str]:
        rows = self._req("GET", f"/nodes/{node}/storage", params={"content": "import"}) or []
        return [s["storage"] for s in rows]

    def bridges(self, node: str) -> list[str]:
        rows = self._req("GET", f"/nodes/{node}/network", params={"type": "any_bridge"}) or []
        return sorted(b["iface"] for b in rows)

    def next_vmid(self) -> int:
        return int(self._req("GET", "/cluster/nextid"))

    def vmid_free(self, vmid: int) -> bool:
        try:
            self._req("GET", "/cluster/nextid", params={"vmid": vmid})
        except ProxmoxError as e:
            if e.status in (400, 500):
                return False
            raise
        return True

    def upload_import(self, node: str, storage: str, path: Path, filename: str, on_progress: Callable[[float, float], None]) -> str:
        return self.upload_file(node, storage, path, filename, "import", on_progress)

    def iso_storages(self, node: str) -> list[str]:
        rows = self._req("GET", f"/nodes/{node}/storage", params={"content": "iso"}) or []
        return [s["storage"] for s in rows]

    def upload_file(
        self,
        node: str,
        storage: str,
        path: Path,
        filename: str,
        content: str,
        on_progress: Callable[[float, float], None],
    ) -> str:
        boundary = "----proxlaber" + str(time.time_ns())
        head = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"\r\n\r\n{content}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"filename\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = path.stat().st_size

        def body() -> Iterator[bytes]:
            yield head
            sent = 0
            start = time.monotonic()
            with open(path, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    yield chunk
                    sent += len(chunk)
                    elapsed = max(time.monotonic() - start, 1e-6)
                    on_progress(sent / size if size else 1.0, sent / elapsed)
            yield tail

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + size + len(tail)),
        }
        return self._req("POST", f"/nodes/{node}/storage/{storage}/upload", content=body(), headers=headers)

    def create_vm(self, node: str, vmid: int, name: str, params: dict) -> str:
        return self._req("POST", f"/nodes/{node}/qemu", data={"vmid": vmid, "name": name, **params})

    def resize_disk(self, node: str, vmid: int, disk: str, size_gb: int):
        """Geeft een UPID terug (PVE 8+ voert resize als taak uit) of None."""
        return self._req("PUT", f"/nodes/{node}/qemu/{vmid}/resize", data={"disk": disk, "size": f"{size_gb}G"})

    def vm_tags(self, node: str, vmid: int) -> str:
        return (self._req("GET", f"/nodes/{node}/qemu/{vmid}/config") or {}).get("tags", "")

    def start_vm(self, node: str, vmid: int) -> str:
        return self._req("POST", f"/nodes/{node}/qemu/{vmid}/status/start")

    def wait_task(self, node: str, upid: str, on_tick: Callable[[], None] | None = None, timeout: float = 3600) -> None:
        deadline = time.monotonic() + timeout
        errors = 0
        while True:
            try:
                st = self._req("GET", f"/nodes/{node}/tasks/{upid}/status")
                errors = 0
            except ProxmoxError as e:
                # korte netwerkstoring of 5xx tijdens pollen: even doorproberen i.p.v. de uitrol te laten falen
                if (e.status is None or e.status >= 500) and errors < 5:
                    errors += 1
                    time.sleep(self.poll_interval)
                    continue
                raise
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise ProxmoxError(f"Task failed: {st.get('exitstatus')}")
                return
            if time.monotonic() > deadline:
                raise ProxmoxError("Timed out waiting for Proxmox task")
            if on_tick:
                on_tick()
            time.sleep(self.poll_interval)

    def delete_vm(self, node: str, vmid: int) -> None:
        self._req("DELETE", f"/nodes/{node}/qemu/{vmid}", params={"purge": 1, "destroy-unreferenced-disks": 1})

    def delete_volume(self, node: str, storage: str, volid: str) -> None:
        self._req("DELETE", f"/nodes/{node}/storage/{storage}/content/{volid}")


def client_from_row(row) -> ProxmoxClient:
    return ProxmoxClient(row["url"], row["token_id"], row["token_secret"], verify_tls=bool(row["verify_tls"]))

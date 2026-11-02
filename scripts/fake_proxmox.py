"""Minimale nagebootste Proxmox VE 9 API voor ontwikkeling en tests.

Start:  python scripts/fake_proxmox.py   (poort 8006, http)
Env:    FAKE_FAIL_AT=create   -> POST /qemu geeft 500
        FAKE_UPLOAD_DELAY=0.1 -> seconden wachttijd per ontvangen 64 KiB (traag uploaden)
"""
import itertools
import os

from fastapi import FastAPI, HTTPException, Request


def create_fake(fail_at: str | None = None, upload_delay: float = 0.0) -> FastAPI:
    app = FastAPI()
    state = {"vms": {}, "volumes": [], "log": [], "uploads": []}
    counter = itertools.count(1)
    polls: dict[str, int] = {}
    app.state.fake = state

    def data(x):
        return {"data": x}

    @app.get("/api2/json/version")
    def version():
        return data({"version": "9.0.3", "release": "9.0"})

    @app.get("/api2/json/storage")
    def storage(content: str = ""):
        return data([{"storage": "local"}] if content == "import" else [{"storage": "local"}, {"storage": "local-lvm"}])

    @app.get("/api2/json/nodes")
    def nodes():
        return data([{"node": "pve1"}, {"node": "pve2"}])

    @app.get("/api2/json/nodes/{node}/storage")
    def node_storage(node: str, content: str = ""):
        if content == "import":
            return data([{"storage": "local"}])
        return data([{"storage": "local-lvm", "avail": 200 * 1024**3}, {"storage": "ssd", "avail": 80 * 1024**3}])

    @app.get("/api2/json/nodes/{node}/network")
    def network(node: str):
        return data([{"iface": "vmbr0"}, {"iface": "vmbr1"}, {"iface": "vmbr2"}])

    @app.get("/api2/json/cluster/nextid")
    def nextid(vmid: int | None = None):
        if vmid is not None:
            if vmid in state["vms"]:
                raise HTTPException(400, f"VM {vmid} already exists")
            return data(vmid)
        return data(next(i for i in itertools.count(100) if i not in state["vms"]))

    @app.post("/api2/json/nodes/{node}/storage/{storage}/upload")
    async def upload(node: str, storage: str, request: Request):
        import asyncio

        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if upload_delay:
                await asyncio.sleep(upload_delay)
        state["uploads"].append({"storage": storage, "bytes": size})
        state["volumes"].append(storage)
        state["log"].append("upload")
        return data(f"UPID:pve1:upload-{next(counter)}")

    @app.post("/api2/json/nodes/{node}/qemu")
    async def create(node: str, request: Request):
        form = dict((await request.form()).items())
        if fail_at == "create":
            raise HTTPException(500, "create failed (FAKE_FAIL_AT)")
        vmid = int(form["vmid"])
        if vmid in state["vms"]:
            raise HTTPException(500, f"VM {vmid} already exists")
        state["vms"][vmid] = form
        state["log"].append("create_vm")
        return data(f"UPID:pve1:create-{next(counter)}")

    @app.get("/api2/json/nodes/{node}/tasks/{upid}/status")
    def task(node: str, upid: str):
        polls[upid] = polls.get(upid, 0) + 1
        if "create" in upid and polls[upid] < 4:
            return data({"status": "running"})
        return data({"status": "stopped", "exitstatus": "OK"})

    @app.put("/api2/json/nodes/{node}/qemu/{vmid}/resize")
    async def resize(node: str, vmid: int, request: Request):
        state["log"].append("resize_disk")
        return data(None)

    @app.post("/api2/json/nodes/{node}/qemu/{vmid}/status/start")
    def start(node: str, vmid: int):
        state["log"].append("start_vm")
        return data(f"UPID:pve1:start-{next(counter)}")

    @app.delete("/api2/json/nodes/{node}/qemu/{vmid}")
    def delete_vm(node: str, vmid: int):
        state["vms"].pop(vmid, None)
        state["log"].append("delete_vm")
        return data(f"UPID:pve1:del-{next(counter)}")

    @app.delete("/api2/json/nodes/{node}/storage/{storage}/content/{volid:path}")
    def delete_volume(node: str, storage: str, volid: str):
        state["log"].append("delete_volume")
        return data(f"UPID:pve1:delvol-{next(counter)}")

    @app.get("/_state")
    def get_state():
        return state

    return app


app = create_fake(os.environ.get("FAKE_FAIL_AT"), float(os.environ.get("FAKE_UPLOAD_DELAY", "0")))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FAKE_PORT", "8006")))

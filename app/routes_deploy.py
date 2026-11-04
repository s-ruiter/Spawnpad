import re
import shutil
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile

from . import images
from .schemas import Candidate, UploadInfo

router = APIRouter(prefix="/api")

_ID = re.compile(r"^[0-9a-f]{32}$")


def upload_dir(request: Request, upload_id: str):
    if not _ID.match(upload_id):
        raise HTTPException(404, "Upload niet gevonden")
    return request.app.state.settings.temp_dir / "uploads" / upload_id


@router.post("/uploads")
def create_upload(file: UploadFile, request: Request) -> UploadInfo:  # sync: draait in de threadpool, blokkeert de event loop niet
    name = (file.filename or "").lower()
    if not name.endswith((".qcow2", ".zip")):
        raise HTTPException(422, "Alleen .qcow2 of .zip bestanden zijn toegestaan")
    images.cleanup_old_uploads(request.app.state.settings.temp_dir)  # verlaten uploads opruimen zolang de schijf er nog is
    upload_id = uuid.uuid4().hex
    d = upload_dir(request, upload_id)
    try:
        saved = images.receive_upload(file, d)
        if name.endswith(".zip"):
            files = images.inspect_zip(saved, d)
            saved.unlink()
        else:
            files = [saved]
        candidates = [Candidate(name=f.name, virtual_gb=images.virtual_gb(f)) for f in files]
    except ValueError as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(422, str(e))
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return UploadInfo(upload_id=upload_id, candidates=candidates)


@router.delete("/uploads/{upload_id}", status_code=204)
def delete_upload(upload_id: str, request: Request) -> Response:
    shutil.rmtree(upload_dir(request, upload_id), ignore_errors=True)
    return Response(status_code=204)


# --- deployments -----------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402

from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .proxmox import ProxmoxError  # noqa: E402
from .routes_servers import get_server  # noqa: E402
from . import cloudinit  # noqa: E402
from .schemas import BatchIn, Day0Spec, VmConfig  # noqa: E402


class DeployIn(BaseModel):
    server_id: int
    upload_id: str
    candidate: str
    config: VmConfig


def _event(row) -> dict:
    return {k: row[k] for k in ("id", "ts", "step", "state", "message", "progress")}


def _job(row) -> dict:
    return {k: row[k] for k in ("id", "server_id", "node", "vm_name", "vmid", "status", "error", "created_at", "finished_at", "batch_id")}


def _get_job(conn, job_id: str):
    row = conn.execute(
        "SELECT j.*, s.name AS server_name FROM jobs j LEFT JOIN servers s ON s.id=j.server_id WHERE j.id=?", (job_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Uitrol niet gevonden")
    return row


@router.post("/deployments", status_code=201)
def create_deployment(body: DeployIn, request: Request) -> dict:
    if not _ID.match(body.upload_id):
        raise HTTPException(404, "Upload niet gevonden")
    try:
        job_id = request.app.state.job_manager.start(
            request.app.state.conn, body.server_id, body.config, body.upload_id, body.candidate
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except ProxmoxError as e:
        raise HTTPException(502, e.detail)
    return {"id": job_id}


@router.post("/batches", status_code=201)
def create_batch(body: BatchIn, request: Request) -> dict:
    for it in body.items:
        if not _ID.match(it.upload_id):
            raise HTTPException(404, "Upload niet gevonden")
    try:
        batch_id, jobs = request.app.state.job_manager.start_batch(request.app.state.conn, body.server_id, body.items)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except ProxmoxError as e:
        raise HTTPException(502, e.detail)
    return {"id": batch_id, "jobs": jobs}


class PreviewIn(BaseModel):
    day0: Day0Spec
    name: str
    n: int = 1
    count: int = 1


@router.post("/cloudinit/preview")
def cloudinit_preview(body: PreviewIn) -> dict:
    """Toont de user_data die voor VM n gebouwd wordt, met wachtwoord en token afgeschermd."""
    if not 1 <= body.n <= body.count <= 20:
        raise HTTPException(422, "Ongeldig aantal")
    try:
        cloudinit.pick_tokens(body.day0.tokens, body.count)  # valideert aantal en tekens
        masked = body.day0.model_copy(update={"admin_password": "********" if body.day0.admin_password else None})
        token = "XXXXXXXXXXXXXXXXXXXX" if any(t.strip() for t in body.day0.tokens) else None
        return {"user_data": cloudinit.render_user_data(masked, body.name, body.n, body.count, token)}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, request: Request) -> dict:
    conn = request.app.state.conn
    batch = conn.execute(
        "SELECT b.*, s.name AS server_name FROM batches b LEFT JOIN servers s ON s.id=b.server_id WHERE b.id=?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise HTTPException(404, "Batch niet gevonden")
    jobs = []
    for j in conn.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY created_at, rowid", (batch_id,)).fetchall():
        last = conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 1", (j["id"],)).fetchone()
        # voortgang van de upload staat in het laatste 'uploaden'-event met een percentage
        prog = conn.execute(
            "SELECT progress FROM job_events WHERE job_id=? AND step='uploaden' AND progress IS NOT NULL ORDER BY id DESC LIMIT 1", (j["id"],)
        ).fetchone()
        jobs.append({
            **_job(j),
            "step": last["step"] if last else None, "state": last["state"] if last else None,
            "message": last["message"] if last else "", "upload_progress": prog["progress"] if prog else None,
        })
    return {"id": batch["id"], "created_at": batch["created_at"], "server_name": batch["server_name"], "jobs": jobs}


@router.get("/deployments")
def list_deployments(request: Request) -> list[dict]:
    rows = request.app.state.conn.execute(
        "SELECT j.*, s.name AS server_name FROM jobs j LEFT JOIN servers s ON s.id=j.server_id "
        "ORDER BY j.created_at DESC, j.rowid DESC"
    ).fetchall()
    return [{**_job(r), "server_name": r["server_name"]} for r in rows]


@router.get("/deployments/{job_id}")
def get_deployment(job_id: str, request: Request) -> dict:
    conn = request.app.state.conn
    row = _get_job(conn, job_id)
    events = conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
    return {**_job(row), "server_name": row["server_name"], "events": [_event(e) for e in events]}


@router.get("/deployments/{job_id}/events")
async def stream_events(job_id: str, request: Request):
    conn = request.app.state.conn
    _get_job(conn, job_id)
    last = int(request.headers.get("last-event-id", "0") or 0)

    async def gen():
        nonlocal last
        idle = 0
        while True:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id", (job_id, last)
            ).fetchall()
            for r in rows:
                last = r["id"]
                yield f"id: {r['id']}\nevent: step\ndata: {json.dumps(_event(r))}\n\n"
            if rows:
                idle = 0
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]
            if status != "running" and not rows:
                return
            idle += 1
            if idle % 20 == 0:
                yield ": ping\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# --- lookups -----------------------------------------------------------------

def _lookup(request: Request, server_id: int, fn):
    row = get_server(request.app.state.conn, server_id)
    client = request.app.state.client_factory(row)
    try:
        return fn(client)
    except ProxmoxError as e:
        raise HTTPException(502, e.detail)
    finally:
        client.close()


@router.get("/servers/{server_id}/nodes")
def server_nodes(server_id: int, request: Request) -> list[str]:
    return _lookup(request, server_id, lambda c: c.nodes())


@router.get("/servers/{server_id}/nodes/{node}/storages")
def server_storages(server_id: int, node: str, request: Request) -> dict:
    return _lookup(request, server_id, lambda c: {"storages": c.storages(node), "import_storages": c.import_storages(node), "iso_storages": c.iso_storages(node)})


@router.get("/servers/{server_id}/nodes/{node}/bridges")
def server_bridges(server_id: int, node: str, request: Request) -> list[str]:
    return _lookup(request, server_id, lambda c: c.bridges(node))


@router.get("/servers/{server_id}/last-used")
def server_last_used(server_id: int, request: Request) -> dict:
    get_server(request.app.state.conn, server_id)
    row = request.app.state.conn.execute("SELECT * FROM last_used WHERE server_id=?", (server_id,)).fetchone()
    if row is None:
        return {}
    return {"datastore": row["datastore"], "bridge": row["bridge"], "vlan": row["vlan"]}

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
async def create_upload(file: UploadFile, request: Request) -> UploadInfo:
    name = (file.filename or "").lower()
    if not name.endswith((".qcow2", ".zip")):
        raise HTTPException(422, "Alleen .qcow2 of .zip bestanden zijn toegestaan")
    upload_id = uuid.uuid4().hex
    d = upload_dir(request, upload_id)
    try:
        saved = await images.receive_upload(file, d)
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

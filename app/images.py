import math
import shutil
import struct
import time
import zipfile
from pathlib import Path

from fastapi import UploadFile

CHUNK = 1024 * 1024
MAGIC = b"QFI\xfb"


def qcow2_virtual_size(path: Path) -> int:
    with open(path, "rb") as fh:
        head = fh.read(32)
    if len(head) < 32 or head[:4] != MAGIC:
        raise ValueError(f"{Path(path).name} is geen geldig qcow2-bestand")
    return struct.unpack(">Q", head[24:32])[0]


def virtual_gb(path: Path) -> int:
    return math.ceil(qcow2_virtual_size(path) / 1024**3)


def inspect_zip(path: Path, dest: Path) -> list[Path]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError("Ongeldig zip-bestand") from e
    out: list[Path] = []
    with zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name.lower().endswith(".qcow2"):
                continue
            target = dest / name
            n = 1
            while target.exists():
                target = dest / f"{n}-{name}"
                n += 1
            try:
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
            except (zipfile.BadZipFile, OSError) as e:
                raise ValueError("Zip-bestand is beschadigd") from e
            out.append(target)
    if not out:
        raise ValueError("Geen .qcow2-bestand in de zip gevonden")
    return out


async def receive_upload(upload: UploadFile, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (Path(upload.filename or "upload").name or "upload")
    with open(target, "wb") as fh:
        while chunk := await upload.read(CHUNK):
            fh.write(chunk)
    return target


def cleanup_old_uploads(temp_dir: Path, max_age_s: float = 24 * 3600) -> None:
    root = Path(temp_dir) / "uploads"
    if not root.exists():
        return
    cutoff = time.time() - max_age_s
    for d in root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)

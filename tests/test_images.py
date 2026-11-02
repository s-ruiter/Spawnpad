import io
import os
import time
import zipfile

import pytest

from app import images
from tests.conftest import make_qcow2


def test_qcow2_virtual_size_reads_header(tmp_path):
    p = tmp_path / "a.qcow2"
    p.write_bytes(make_qcow2(4 * 1024**3))
    assert images.qcow2_virtual_size(p) == 4 * 1024**3


def test_rejects_non_qcow2_magic(tmp_path):
    p = tmp_path / "a.qcow2"
    p.write_bytes(b"NOPE" + b"\0" * 100)
    with pytest.raises(ValueError):
        images.qcow2_virtual_size(p)


def _zip(tmp_path, members):
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return p


def test_zip_extracts_single_qcow2(tmp_path):
    z = _zip(tmp_path, {"fortios.qcow2": make_qcow2(), "readme.txt": b"hi"})
    out = tmp_path / "out"
    out.mkdir()
    files = images.inspect_zip(z, out)
    assert [f.name for f in files] == ["fortios.qcow2"]
    assert not (out / "readme.txt").exists()


def test_zip_without_qcow2_raises(tmp_path):
    z = _zip(tmp_path, {"a.txt": b"x"})
    with pytest.raises(ValueError):
        images.inspect_zip(z, tmp_path)


def test_zip_slip_member_is_flattened(tmp_path):
    z = _zip(tmp_path, {"../../evil.qcow2": make_qcow2()})
    out = tmp_path / "a" / "b"
    out.mkdir(parents=True)
    files = images.inspect_zip(z, out)
    assert files == [out / "evil.qcow2"]
    assert not (tmp_path / "evil.qcow2").exists()


def test_corrupt_zip_raises_valueerror(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"this is not a zip")
    with pytest.raises(ValueError):
        images.inspect_zip(p, tmp_path)


def test_cleanup_old_uploads(tmp_path):
    old = tmp_path / "uploads" / "old"
    new = tmp_path / "uploads" / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    t = time.time() - 48 * 3600
    os.utime(old, (t, t))
    images.cleanup_old_uploads(tmp_path)
    assert not old.exists() and new.exists()


def test_upload_qcow2_returns_candidate(client):
    r = client.post("/api/uploads", files={"file": ("fgt.qcow2", make_qcow2(2 * 1024**3 + 1))})
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == [{"name": "fgt.qcow2", "virtual_gb": 3}]
    assert client.delete(f"/api/uploads/{body['upload_id']}").status_code == 204


def test_upload_zip_returns_candidates(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fortios.qcow2", make_qcow2(1024**3))
    r = client.post("/api/uploads", files={"file": ("FGT.zip", buf.getvalue())})
    assert r.status_code == 200 and r.json()["candidates"][0]["virtual_gb"] == 1


def test_upload_bad_zip_422_and_dir_removed(client, app):
    r = client.post("/api/uploads", files={"file": ("bad.zip", b"nope")})
    assert r.status_code == 422
    uploads = app.state.settings.temp_dir / "uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []


def test_upload_fake_qcow2_422(client):
    r = client.post("/api/uploads", files={"file": ("a.qcow2", b"not really")})
    assert r.status_code == 422


def test_upload_wrong_extension_422(client):
    assert client.post("/api/uploads", files={"file": ("a.iso", b"x")}).status_code == 422

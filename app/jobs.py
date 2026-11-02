import json
import shutil
import threading
import time
import uuid
from typing import Callable

from . import db, images
from .config import Settings
from .deploy import import_filename, run_deployment, vm_tag
from .schemas import VmConfig


class JobManager:
    def __init__(self, settings: Settings, get_factory: Callable):
        self.settings = settings
        self._get_factory = get_factory
        self._threads: dict[str, threading.Thread] = {}

    def _upload_dir(self, upload_id: str):
        return self.settings.temp_dir / "uploads" / upload_id

    def start(self, conn, server_id: int, cfg: VmConfig, upload_id: str, candidate: str) -> str:
        server = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if server is None:
            raise LookupError("Server niet gevonden")
        updir = self._upload_dir(upload_id)
        image = updir / candidate
        if candidate != image.name or not image.is_file():
            raise LookupError("Geüpload bestand niet gevonden")
        if cfg.disk_gb < images.virtual_gb(image):
            raise ValueError(f"Disk moet minimaal {images.virtual_gb(image)} GB zijn (virtuele grootte van het image)")
        if not cfg.nics:
            raise ValueError("Minimaal één netwerkinterface is nodig")

        if cfg.vmid is not None:
            client = self._get_factory()(dict(server))
            try:
                if not client.vmid_free(cfg.vmid):
                    raise ValueError(f"VMID {cfg.vmid} is al in gebruik op deze server")
            finally:
                client.close()

        job_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO jobs(id,server_id,node,vm_name,vmid,config_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, server_id, cfg.node, cfg.name, None,
             json.dumps({"config": cfg.model_dump(), "candidate": candidate, "upload_id": upload_id}),
             "running", time.time()),
        )
        nic = cfg.nics[0]
        conn.execute(
            "INSERT INTO last_used(server_id,datastore,bridge,vlan) VALUES(?,?,?,?) "
            "ON CONFLICT(server_id) DO UPDATE SET datastore=excluded.datastore,bridge=excluded.bridge,vlan=excluded.vlan",
            (server_id, cfg.datastore, nic.bridge, nic.vlan),
        )
        conn.commit()
        t = threading.Thread(target=self._work, args=(job_id, dict(server), cfg, image, upload_id), daemon=True)
        self._threads[job_id] = t
        t.start()
        return job_id

    def _work(self, job_id: str, server: dict, cfg: VmConfig, image, upload_id: str) -> None:
        conn = db.connect(self.settings.db_path)

        def emit(step, state, message, progress=None):
            conn.execute(
                "INSERT INTO job_events(job_id,ts,step,state,message,progress) VALUES(?,?,?,?,?,?)",
                (job_id, time.time(), step, state, message, progress),
            )
            conn.commit()

        def on_vmid(vmid: int):
            conn.execute("UPDATE jobs SET vmid=? WHERE id=?", (vmid, job_id))
            conn.commit()

        client = None
        status, error = "done", None
        try:
            client = self._get_factory()(server)
            run_deployment(job_id, client, cfg, image, emit, on_vmid=on_vmid)
        except Exception as e:  # run_deployment heeft al opgeruimd en een failed-event gestuurd
            status, error = "failed", getattr(e, "detail", None) or str(e)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            shutil.rmtree(self._upload_dir(upload_id), ignore_errors=True)
            conn.execute(
                "UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, time.time(), job_id),
            )
            conn.commit()
            conn.close()

    def wait(self, job_id: str, timeout: float = 10) -> None:
        t = self._threads.get(job_id)
        if t:
            t.join(timeout)

    def recover_on_startup(self) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            rows = conn.execute(
                "SELECT j.*, s.id AS sid FROM jobs j JOIN servers s ON s.id=j.server_id WHERE j.status='running'"
            ).fetchall()
            for row in rows:
                meta = json.loads(row["config_json"])
                server = conn.execute("SELECT * FROM servers WHERE id=?", (row["server_id"],)).fetchone()
                cfg = meta["config"]
                if server is not None:
                    self._best_effort_cleanup(dict(server), row["node"], row["vmid"], cfg["import_storage"], row["id"])
                shutil.rmtree(self._upload_dir(meta["upload_id"]), ignore_errors=True)
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                    ("App herstart tijdens uitrol", time.time(), row["id"]),
                )
                conn.execute(
                    "INSERT INTO job_events(job_id,ts,step,state,message) VALUES(?,?,?,?,?)",
                    (row["id"], time.time(), "klaar", "failed", "App herstart tijdens uitrol"),
                )
            conn.commit()
        finally:
            conn.close()

    def _best_effort_cleanup(self, server: dict, node: str, vmid: int | None, storage: str, job_id: str) -> None:
        try:
            client = self._get_factory()(server)
        except Exception:
            return
        try:
            # Alleen een VM verwijderen die deze job zelf heeft aangemaakt (tag), nooit alleen op vmid.
            if vmid is not None:
                try:
                    if vm_tag(job_id) in client.vm_tags(node, vmid).split(";"):
                        client.delete_vm(node, vmid)
                except Exception:
                    pass
            try:
                client.delete_volume(node, storage, f"{storage}:import/{import_filename(job_id)}")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

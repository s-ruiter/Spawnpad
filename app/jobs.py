import json
import re
import shutil
import threading
import time
import uuid
from typing import Callable

from . import db, images
from .config import Settings
from .deploy import import_filename, run_deployment, vm_tag
from .schemas import BatchItem, VmConfig


class JobManager:
    def __init__(self, settings: Settings, get_factory: Callable):
        self.settings = settings
        self._get_factory = get_factory
        self._threads: dict[str, threading.Thread] = {}
        self._refs: dict[str, int] = {}  # aantal lopende jobs per upload; de map gaat weg als de laatste klaar is
        self._lock = threading.Lock()
        self._vmid_locks: dict[int, threading.Lock] = {}
        self.reserve_poll = 0.5

    def _upload_dir(self, upload_id: str):
        return self.settings.temp_dir / "uploads" / upload_id

    NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

    def _server(self, conn, server_id: int):
        server = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if server is None:
            raise LookupError("Server niet gevonden")
        return server

    def _prepare(self, server, cfg: VmConfig, upload_id: str, candidate: str, count: int = 1):
        """Controleert één uitrol-opdracht volledig vóórdat er iets gestart wordt; geeft het image-pad terug."""
        image = self._upload_dir(upload_id) / candidate
        if candidate != image.name or not image.is_file():
            raise LookupError("Geüpload bestand niet gevonden")
        if not self.NAME_RE.match(cfg.name):
            raise ValueError(f"Ongeldige VM-naam '{cfg.name}': gebruik letters, cijfers en streepjes (max. 63 tekens)")
        if count > 1 and cfg.vmid is not None:
            raise ValueError("Een vast VMID kan alleen bij een aantal van 1")
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
        return image

    def _launch(self, conn, server, cfg: VmConfig, image, upload_id: str, candidate: str, batch_id: str | None) -> str:
        job_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO jobs(id,server_id,node,vm_name,vmid,config_json,status,created_at,batch_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, server["id"], cfg.node, cfg.name, None,
             json.dumps({"config": cfg.model_dump(), "candidate": candidate, "upload_id": upload_id}),
             "running", time.time(), batch_id),
        )
        nic = cfg.nics[0]
        conn.execute(
            "INSERT INTO last_used(server_id,datastore,bridge,vlan) VALUES(?,?,?,?) "
            "ON CONFLICT(server_id) DO UPDATE SET datastore=excluded.datastore,bridge=excluded.bridge,vlan=excluded.vlan",
            (server["id"], cfg.datastore, nic.bridge, nic.vlan),
        )
        conn.commit()
        with self._lock:
            self._refs[upload_id] = self._refs.get(upload_id, 0) + 1
        t = threading.Thread(target=self._work, args=(job_id, dict(server), cfg, image, upload_id), daemon=True)
        self._threads[job_id] = t
        t.start()
        return job_id

    def start(self, conn, server_id: int, cfg: VmConfig, upload_id: str, candidate: str) -> str:
        server = self._server(conn, server_id)
        image = self._prepare(server, cfg, upload_id, candidate)
        return self._launch(conn, server, cfg, image, upload_id, candidate, None)

    def start_batch(self, conn, server_id: int, items: list[BatchItem]) -> tuple[str, list[dict]]:
        """Alles wordt eerst gevalideerd; pas daarna starten de jobs (alles of niets)."""
        server = self._server(conn, server_id)
        prepared = [(it, self._prepare(server, it.config, it.upload_id, it.candidate, it.count)) for it in items]
        batch_id = uuid.uuid4().hex
        conn.execute("INSERT INTO batches(id,server_id,created_at) VALUES(?,?,?)", (batch_id, server_id, time.time()))
        conn.commit()
        jobs = []
        for it, image in prepared:
            for n in range(1, it.count + 1):
                name = it.config.name if it.count == 1 else f"{it.config.name}-{n}"
                if not self.NAME_RE.match(name):
                    name = it.config.name  # kan alleen bij een naam van 62-63 tekens; de job faalt dan netjes bij Proxmox
                cfg = it.config.model_copy(update={"name": name})
                jobs.append({"id": self._launch(conn, server, cfg, image, it.upload_id, it.candidate, batch_id), "vm_name": name})
        return batch_id, jobs

    def _vmid_lock(self, server_id: int) -> threading.Lock:
        with self._lock:
            return self._vmid_locks.setdefault(server_id, threading.Lock())

    def _release(self, upload_id: str) -> None:
        with self._lock:
            left = self._refs.get(upload_id, 1) - 1
            if left <= 0:
                self._refs.pop(upload_id, None)
                shutil.rmtree(self._upload_dir(upload_id), ignore_errors=True)
            else:
                self._refs[upload_id] = left

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
            run_deployment(job_id, client, cfg, image, emit, on_vmid=on_vmid, vmid_lock=self._vmid_lock(server["id"]),
                           reserve_poll=self.reserve_poll)
        except Exception as e:  # run_deployment heeft al opgeruimd en een failed-event gestuurd
            status, error = "failed", getattr(e, "detail", None) or str(e)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            self._release(upload_id)
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

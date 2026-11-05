import json
import re
import shutil
import threading
import time
import uuid
from typing import Callable

from . import cloudinit, db, images
from .config import Settings
from .deploy import import_filename, iso_filename, run_deployment, vm_tag
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
            raise LookupError("Server not found")
        return server

    def _prepare(self, server, cfg: VmConfig, upload_id: str, candidate: str, count: int = 1):
        """Controleert één uitrol-opdracht volledig vóórdat er iets gestart wordt; geeft het image-pad terug."""
        image = self._upload_dir(upload_id) / candidate
        if candidate != image.name or not image.is_file():
            raise LookupError("Uploaded file not found")
        if not self.NAME_RE.match(cfg.name):
            raise ValueError(f"Invalid VM name '{cfg.name}': use letters, digits and hyphens (max. 63 characters)")
        if count > 1 and cfg.vmid is not None:
            raise ValueError("A fixed VMID is only possible with a count of 1")
        if cfg.disk_gb < images.virtual_gb(image):
            raise ValueError(f"Disk must be at least {images.virtual_gb(image)} GB (virtual size of the image)")
        if not cfg.nics:
            raise ValueError("At least one network interface is required")
        if cfg.vmid is not None:
            client = self._get_factory()(dict(server))
            try:
                if not client.vmid_free(cfg.vmid):
                    raise ValueError(f"VMID {cfg.vmid} is already in use on this server")
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
        """Everything is validated first (including the Day-0 configuration); only then do the jobs start (all or nothing)."""
        server = self._server(conn, server_id)
        plan = []  # (item, image, [(naam, VmConfig)])
        for it in items:
            image = self._prepare(server, it.config, it.upload_id, it.candidate, it.count)
            tokens = cloudinit.pick_tokens(it.day0.tokens, it.count) if it.day0 else [None] * it.count
            if it.day0 and not it.config.iso_storage:
                raise ValueError("Choose a storage with content type ISO for the Day-0 configuration")
            vms = []
            for n in range(1, it.count + 1):
                name = it.config.name if it.count == 1 else f"{it.config.name}-{n}"
                update = {"name": name}
                if it.day0:
                    update["user_data"] = cloudinit.render_user_data(it.day0, name, n, it.count, tokens[n - 1])
                vms.append(it.config.model_copy(update=update))
            plan.append((it, image, vms))
        batch_id = uuid.uuid4().hex
        conn.execute("INSERT INTO batches(id,server_id,created_at) VALUES(?,?,?)", (batch_id, server_id, time.time()))
        conn.commit()
        jobs = []
        for it, image, vms in plan:
            for cfg in vms:
                jobs.append({"id": self._launch(conn, server, cfg, image, it.upload_id, it.candidate, batch_id), "vm_name": cfg.name})
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
                    self._best_effort_cleanup(dict(server), row["node"], row["vmid"], cfg["import_storage"], row["id"], cfg.get("iso_storage"))
                shutil.rmtree(self._upload_dir(meta["upload_id"]), ignore_errors=True)
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                    ("App restarted during deployment", time.time(), row["id"]),
                )
                conn.execute(
                    "INSERT INTO job_events(job_id,ts,step,state,message) VALUES(?,?,?,?,?)",
                    (row["id"], time.time(), "klaar", "failed", "App restarted during deployment"),
                )
            conn.commit()
        finally:
            conn.close()

    def _best_effort_cleanup(self, server: dict, node: str, vmid: int | None, storage: str, job_id: str, iso_storage: str | None = None) -> None:
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
            if iso_storage:
                try:
                    client.delete_volume(node, iso_storage, f"{iso_storage}:iso/{iso_filename(job_id)}")
                except Exception:
                    pass
        finally:
            try:
                client.close()
            except Exception:
                pass

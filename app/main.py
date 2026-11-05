from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db
from .config import Settings, get_settings
from .proxmox import client_from_row
from .jobs import JobManager
from .images import cleanup_old_uploads
from .routes_deploy import router as deploy_router
from .routes_servers import router as servers_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Spawnpad")
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    app.state.settings = settings
    app.state.conn = conn
    app.state.client_factory = client_from_row
    app.state.job_manager = JobManager(settings, lambda: app.state.client_factory)
    app.state.job_manager.recover_on_startup()
    cleanup_old_uploads(settings.temp_dir)
    app.include_router(servers_router)
    app.include_router(deploy_router)
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
    return app

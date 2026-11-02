from fastapi import FastAPI

from . import db
from .config import Settings, get_settings
from .proxmox import client_from_row
from .routes_servers import router as servers_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Proxlaber")
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    app.state.settings = settings
    app.state.conn = conn
    app.state.client_factory = client_from_row
    app.include_router(servers_router)
    return app

import pytest

from app import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def app(tmp_path):
    from app.config import Settings
    from app.main import create_app

    return create_app(Settings(data_dir=tmp_path))


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

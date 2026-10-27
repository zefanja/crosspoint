import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-test-secret-key-0123")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("APP_VERSION", "abcdef0123456789")

    from app.config import get_settings
    from app.db import get_engine, get_sessionmaker

    for cached in (get_settings, get_engine, get_sessionmaker):
        cached.cache_clear()
    yield tmp_path
    get_engine().dispose()
    for cached in (get_settings, get_engine, get_sessionmaker):
        cached.cache_clear()


@pytest.fixture
def client(data_dir):
    from app.main import app

    with TestClient(app) as c:
        yield c

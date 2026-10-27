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
def migrated(data_dir):
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    return data_dir


@pytest.fixture
def client(migrated):
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(migrated):
    from app.db import get_sessionmaker

    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def make_user(db):
    from app.auth import create_user

    def make(name="Anna", role="learner"):
        return create_user(db, name, role)

    return make


@pytest.fixture
def login(client, make_user):
    """Create a user, log in via QR link, return (user, csrf token)."""
    from app.auth import SESSION_COOKIE, csrf_token_for

    def do(name="Anna", role="learner"):
        user, code = make_user(name, role)
        r = client.get(f"/login?c={code}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        return user, csrf_token_for(client.cookies[SESSION_COOKIE])

    return do


@pytest.fixture
def run(data_dir):
    """Run coroutines on one event loop per test; closes all collections afterwards."""
    import asyncio

    from app import anki_service

    loop = asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.run_until_complete(anki_service.close_all())
    loop.close()

import pytest

from app.config import ConfigError, load_settings


def test_healthz_ok(client, data_dir):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "abcdef0123456789"}
    assert not list(data_dir.glob(".healthz-*"))


def test_healthz_fails_without_write_access(client, data_dir):
    data_dir.chmod(0o500)
    try:
        assert client.get("/healthz").status_code == 503
    finally:
        data_dir.chmod(0o700)


def test_index_shows_short_version_and_source_link(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "abcdef0" in r.text
    assert "Quellcode" in r.text


def test_missing_required_env_fails(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("BASE_URL", "http://x")
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_settings()


def test_alembic_upgrade_head(data_dir):
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    assert (data_dir / "app.db").exists()

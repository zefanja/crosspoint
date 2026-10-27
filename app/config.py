"""Configuration from environment variables only (12-factor)."""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED = ("SECRET_KEY", "BASE_URL")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    secret_key: str
    base_url: str
    data_dir: Path
    tz: str
    initial_admin_name: str
    backup_keep_days: int
    app_version: str
    default_language: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def short_version(self) -> str:
        return self.app_version[:7]


def load_settings() -> Settings:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )
    secret_key = os.environ["SECRET_KEY"]
    if len(secret_key) < 32:
        log.warning("SECRET_KEY is shorter than 32 characters – fine for dev, not for production")
    return Settings(
        secret_key=secret_key,
        base_url=os.environ["BASE_URL"].rstrip("/"),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        tz=os.environ.get("TZ", "Europe/Berlin"),
        initial_admin_name=os.environ.get("INITIAL_ADMIN_NAME", "Admin"),
        backup_keep_days=int(os.environ.get("BACKUP_KEEP_DAYS", "7")),
        app_version=os.environ.get("APP_VERSION", "dev"),
        default_language=os.environ.get("DEFAULT_LANGUAGE", "en"),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()

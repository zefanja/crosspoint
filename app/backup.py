"""Nightly backup at 03:00 into DATA_DIR/backups/<date>/ plus rotation."""

import logging
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app import anki_service
from app.config import get_settings

log = logging.getLogger(__name__)
BACKUP_HOUR = 3


def backups_dir() -> Path:
    return get_settings().data_dir / "backups"


def _backup_app_db(dest: Path) -> None:
    src = sqlite3.connect(get_settings().db_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()


async def run_backup(day: date | None = None) -> Path:
    """Create backups/<day>/{app.db, collections.tar.gz}. Returns the directory."""
    day = day or date.today()
    target = backups_dir() / day.isoformat()
    target.mkdir(parents=True, exist_ok=True)
    await run_in_threadpool(_backup_app_db, target / "app.db")
    vapid_key = get_settings().data_dir / "vapid_private.pem"
    if vapid_key.exists():
        shutil.copy2(vapid_key, target / vapid_key.name)

    with tempfile.TemporaryDirectory(dir=backups_dir()) as tmp:
        staging = Path(tmp) / "collections"
        for user_id in anki_service.collection_ids():
            await anki_service.backup_collection(user_id, staging / user_id)

        def pack() -> None:
            partial = target / "collections.tar.gz.part"
            with tarfile.open(partial, "w:gz") as tar:
                if staging.exists():
                    tar.add(staging, arcname="collections")
            partial.rename(target / "collections.tar.gz")

        await run_in_threadpool(pack)
    rotate()
    log.info("Backup written to %s", target)
    return target


def rotate(keep_days: int | None = None, today: date | None = None) -> None:
    keep_days = get_settings().backup_keep_days if keep_days is None else keep_days
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    if not backups_dir().exists():
        return
    for entry in backups_dir().iterdir():
        try:
            day = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if day <= cutoff:
            shutil.rmtree(entry, ignore_errors=True)


def backup_due(now: datetime | None = None) -> bool:
    """True after 03:00 if today's backup does not exist yet."""
    now = now or datetime.now()
    done = backups_dir() / now.date().isoformat() / "collections.tar.gz"
    return now.hour >= BACKUP_HOUR and not done.exists()

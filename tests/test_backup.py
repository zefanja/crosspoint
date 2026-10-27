import os
import sqlite3
import tarfile
import time
from datetime import date, datetime

from app import anki_service, backup


def test_run_backup_and_rotation(run, migrated, make_user):
    user, _ = make_user()
    run(anki_service.ensure_collection(user.id))
    target = run(backup.run_backup(date(2026, 10, 27)))

    con = sqlite3.connect(target / "app.db")
    assert con.execute("select count(*) from users").fetchone()[0] == 1
    con.close()
    with tarfile.open(target / "collections.tar.gz") as tar:
        assert f"collections/{user.id}/collection.anki2" in tar.getnames()

    old = backup.backups_dir() / "2026-10-01"
    old.mkdir()
    backup.rotate(keep_days=7, today=date(2026, 10, 27))
    assert not old.exists() and target.exists()


def test_backup_due(migrated):
    assert not backup.backup_due(datetime(2026, 10, 27, 2, 59))
    assert backup.backup_due(datetime(2026, 10, 27, 3, 0))
    done = backup.backups_dir() / "2026-10-27"
    done.mkdir(parents=True)
    (done / "collections.tar.gz").write_bytes(b"")
    assert not backup.backup_due(datetime(2026, 10, 27, 3, 5))


def test_cleanup_exports(client, data_dir):
    from app.main import cleanup_exports

    old = data_dir / "exports" / "old.apkg"
    new = data_dir / "exports" / "new.apkg"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    cleanup_exports()
    assert not old.exists() and new.exists()

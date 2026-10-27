"""Distribute group decks from the admin collection into member collections.

Runs as an in-process background task (single uvicorn worker) with progress that the
admin UI polls. Each deck is exported once from the admin collection WITHOUT scheduling
and imported into every member collection; notes carry the tag `gruppendeck`.
Re-distributing updates existing notes by GUID (content changes, learning progress
stays) and adds new ones. Notes deleted in the admin deck are not deleted for members.
"""

import asyncio
import logging
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app import anki_service, stats
from app.config import get_settings
from app.db import get_sessionmaker
from app.i18n import _
from app.models import GroupDeck, GroupMember, User

log = logging.getLogger(__name__)
MAX_JOBS = 50


@dataclass
class Job:
    id: str
    title: str
    total: int = 0
    done: int = 0
    errors: list[str] = field(default_factory=list)
    finished: bool = False
    started_at: datetime = field(default_factory=datetime.now)
    task: asyncio.Task | None = None

    @property
    def percent(self) -> int:
        return 100 if not self.total else round(100 * self.done / self.total)


_jobs: dict[str, Job] = {}


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def _new_job(title: str) -> Job:
    job = Job(id=secrets.token_hex(8), title=title)
    _jobs[job.id] = job
    while len(_jobs) > MAX_JOBS:
        oldest = next(iter(_jobs))
        if not _jobs[oldest].finished:
            break
        del _jobs[oldest]
    return job


async def wait(job_id: str) -> Job:
    """Await a job (tests)."""
    job = _jobs[job_id]
    if job.task is not None:
        await job.task
    return job


def _start(job: Job, coro) -> Job:
    """Must be called from the event loop (async route handlers)."""

    async def runner():
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 - surface in the UI
            log.exception("Job %s failed", job.title)
            job.errors.append(f"Aborted: {exc}")
        finally:
            job.finished = True

    job.task = asyncio.create_task(runner())
    return job


def _members(group_id: int) -> list[tuple[str, str]]:
    with get_sessionmaker()() as db:
        rows = db.execute(
            select(User.id, User.display_name)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == group_id)
            .order_by(User.display_name)
        ).all()
    return [(r[0], r[1]) for r in rows]


async def _run_distribution(job: Job, decks: list[GroupDeck], users: list[tuple[str, str]]):
    job.total = len(decks) * len(users)
    tmp_root = get_settings().data_dir / "exports"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        packages: list[tuple[GroupDeck, Path]] = []
        for gd in decks:
            try:
                await anki_service.tag_deck_notes(anki_service.ADMIN_COLLECTION, gd.admin_deck_id)
                path = Path(tmp) / f"{gd.id}.apkg"
                await anki_service.export_deck(
                    anki_service.ADMIN_COLLECTION, gd.admin_deck_id, path, with_scheduling=False
                )
                packages.append((gd, path))
            except anki_service.AnkiServiceError as exc:
                job.errors.append(f"{gd.deck_name}: {_(str(exc))}")
                job.total -= len(users)

        for user_id, name in users:
            for gd, path in packages:
                try:
                    await anki_service.import_package(user_id, path, with_scheduling=False)
                except Exception as exc:  # noqa: BLE001 - continue with the others
                    log.exception("Import for %s failed", user_id)
                    job.errors.append(f"{name}: {exc}")
                job.done += 1
            await stats.update_after_review(user_id)

    now = datetime.now()
    with get_sessionmaker()() as db:
        for gd, _ in packages:
            row = db.get(GroupDeck, gd.id)
            if row is not None:
                row.last_distributed_at = now
        db.commit()


def distribute_group_deck(group_deck: GroupDeck) -> Job:
    """Distribute one group deck to all members of its group."""
    job = _new_job(_("Distribute “%(deck)s”", deck=group_deck.deck_name))
    return _start(job, _run_distribution(job, [group_deck], _members(group_deck.group_id)))


def distribute_group_to_users(group_id: int, users: list[tuple[str, str]], title: str) -> Job | None:
    """Give (new) members all decks of the group. None if the group has no decks."""
    with get_sessionmaker()() as db:
        decks = list(db.scalars(select(GroupDeck).where(GroupDeck.group_id == group_id)))
        for gd in decks:
            db.expunge(gd)
    if not decks or not users:
        return None
    job = _new_job(title)
    return _start(job, _run_distribution(job, decks, users))


def refresh_stats(users: list[tuple[str, str]], title: str) -> Job:
    """Recompute user_stats for the given users (opens each collection once)."""
    job = _new_job(title)

    async def run():
        job.total = len(users)
        for user_id, _ in users:
            await stats.update_after_review(user_id)
            job.done += 1

    return _start(job, run())

"""The ONLY module that imports `anki`.

Every collection access runs inside the per-user asyncio lock and in the threadpool.
Routes call the public `async` functions below; the `_sync` helpers receive an open
Collection and must never be called from anywhere else.

Verified against anki==26.9.3 (see SPEC §6, spike results).
"""

import asyncio
import logging
import re
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from anki._backend import RustBackend
from anki.collection import (
    Collection,
    DeckIdLimit,
    ExportAnkiPackageOptions,
    ImportAnkiPackageOptions,
    ImportAnkiPackageRequest,
)
from anki.errors import NotFoundError, SearchError
from anki.import_export_pb2 import ImportAnkiPackageUpdateCondition
from anki.notes import NoteFieldsCheckResult
from anki.scheduler.v3 import CardAnswer
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.sanitize import plain_text

log = logging.getLogger(__name__)

ADMIN_COLLECTION = "_admin"
MAX_OPEN = 20
IDLE_SECONDS = 600
GROUP_TAG = "gruppendeck"
DEFAULT_DECK = "Meine Karten"
BASIC = "Basic"
BIBELVERS = "Bibelvers"
DEFAULT_NEW_PER_DAY = 10
DEFAULT_REVIEWS_PER_DAY = 200
DEFAULT_RETENTION = 0.9
# Collections are created with an English backend so stock names stay "Basic"/"Default"
# (a German backend would create "Einfach"); afterwards they are opened in German so
# interval labels and error messages are localized.
CREATE_LANG = "en"
UI_LANG = "de"

RATINGS = {1: CardAnswer.AGAIN, 2: CardAnswer.HARD, 3: CardAnswer.GOOD, 4: CardAnswer.EASY}
QUEUE_SUSPENDED = -1
CARD_TYPE_NEW = 0

_USER_ID = re.compile(r"^[0-9A-Za-z_]{1,40}$")
# describe_next_states() wraps numbers in Unicode isolates (U+2068/U+2069).
_LABEL_MARKS = str.maketrans("", "", "⁨⁩")

BIBELVERS_CSS = """\
.card {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 24px;
  line-height: 1.5;
  text-align: center;
  color: #1d1d1f;
  background-color: #ffffff;
  padding: 0 12px;
}
.uebersetzung { font-size: 14px; opacity: 0.6; margin-top: 4px; }
.text { text-align: left; display: inline-block; max-width: 36em; }
.card.nightMode, .nightMode .card { color: #ececef; background-color: #1f1f23; }
@media (prefers-color-scheme: dark) {
  .card { color: #ececef; background-color: #1f1f23; }
}
"""
BIBELVERS_QFMT = (
    "{{Referenz}}"
    "{{#Übersetzung}}<div class=uebersetzung>{{Übersetzung}}</div>{{/Übersetzung}}"
)
BIBELVERS_AFMT = "{{FrontSide}}\n\n<hr id=answer>\n\n<div class=text>{{Text}}</div>"


class AnkiServiceError(Exception):
    """Base class for errors the UI shows to the user."""


class EmptyNote(AnkiServiceError):
    pass


class ReadOnlyNote(AnkiServiceError):
    pass


class InvalidSearch(AnkiServiceError):
    pass


class NotFound(AnkiServiceError):
    pass


# --------------------------------------------------------------------------- data types


@dataclass
class DeckInfo:
    id: int
    name: str  # full name, e.g. "Bibel::Psalmen"
    short_name: str
    depth: int
    new: int
    learn: int
    review: int


@dataclass
class ReviewCard:
    card_id: int
    question: str
    answer: str
    labels: list[str]  # interval preview for Again/Hard/Good/Easy
    queue: int


@dataclass
class ReviewState:
    deck_id: int
    deck_name: str
    card: ReviewCard | None
    new: int
    learn: int
    review: int
    can_undo: bool


@dataclass
class NotetypeInfo:
    id: int
    name: str
    fields: list[str]


@dataclass
class NoteData:
    id: int
    notetype: str
    fields: list[tuple[str, str]]
    deck_id: int
    tags: list[str]
    readonly: bool
    suspended: bool


@dataclass
class CardRow:
    card_id: int
    note_id: int
    front: str
    deck: str
    due: str
    suspended: bool


@dataclass
class DeckOptions:
    new_per_day: int
    reviews_per_day: int
    desired_retention: float


@dataclass
class DailyCount:
    day: date
    reviews: int
    again: int
    new: int


@dataclass
class CollectionStats:
    total_cards: int
    due_today: int
    new_today: int
    reviews_today: int
    retention_30d: float | None
    streak_days: int
    last_review_at: datetime | None
    daily: list[DailyCount] = field(default_factory=list)


# --------------------------------------------------------------------------- cache & locks


@dataclass
class _Entry:
    col: Collection
    last_used: float


_open: "OrderedDict[str, _Entry]" = OrderedDict()
_cache_lock = threading.Lock()  # guards _open only; collections are guarded by user locks
_locks: dict[str, asyncio.Lock] = {}

T = TypeVar("T")


def _lock(user_id: str) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


def collections_dir() -> Path:
    return get_settings().data_dir / "collections"


def collection_path(user_id: str) -> Path:
    if not _USER_ID.match(user_id):
        raise ValueError(f"invalid collection id: {user_id!r}")
    return collections_dir() / user_id / "collection.anki2"


def _get_col(user_id: str) -> Collection:
    """Return the open collection, opening (and creating) it if needed.

    Must run in the threadpool while holding the user's lock.
    """
    with _cache_lock:
        entry = _open.get(user_id)
        if entry is not None:
            _open.move_to_end(user_id)
            entry.last_used = time.monotonic()
            return entry.col
    path = collection_path(user_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        col = Collection(str(path), backend=RustBackend(langs=[CREATE_LANG]))
        try:
            _setup_new_collection(col)
        finally:
            col.close()
    col = Collection(str(path), backend=RustBackend(langs=[UI_LANG]))
    try:
        _ensure_notetypes(col)
    except Exception:
        col.close()
        raise
    with _cache_lock:
        _open[user_id] = _Entry(col, time.monotonic())
    return col


async def _run(user_id: str, fn: Callable[..., T], *args: Any) -> T:
    async with _lock(user_id):
        result = await run_in_threadpool(lambda: fn(_get_col(user_id), *args))
    await _evict_if_needed()
    return result


async def _close(user_id: str) -> None:
    async with _lock(user_id):
        with _cache_lock:
            entry = _open.pop(user_id, None)
        if entry is not None:
            await run_in_threadpool(entry.col.close)


async def _evict_if_needed() -> None:
    while True:
        with _cache_lock:
            if len(_open) <= MAX_OPEN:
                return
            victim = next((uid for uid in _open if not _lock(uid).locked()), None)
        if victim is None:
            return
        await _close(victim)


async def close_idle(max_idle: float = IDLE_SECONDS) -> int:
    """Close collections unused for `max_idle` seconds. Returns how many were closed."""
    now = time.monotonic()
    with _cache_lock:
        idle = [uid for uid, e in _open.items() if now - e.last_used > max_idle]
    for uid in idle:
        await _close(uid)
    return len(idle)


async def close_all() -> None:
    """Close every open collection (shutdown)."""
    with _cache_lock:
        user_ids = list(_open)
    for uid in user_ids:
        await _close(uid)
    _locks.clear()


def open_count() -> int:
    with _cache_lock:
        return len(_open)


# --------------------------------------------------------------------------- setup


def _setup_new_collection(col: Collection) -> None:
    col.set_config("fsrs", True)
    prefs = col.get_preferences()
    prefs.scheduling.rollover = 4
    col.set_preferences(prefs)
    _ensure_notetypes(col)

    conf = col.decks.config_dict_for_deck_id(1)
    conf["new"]["perDay"] = DEFAULT_NEW_PER_DAY
    conf["rev"]["perDay"] = DEFAULT_REVIEWS_PER_DAY
    conf["desiredRetention"] = DEFAULT_RETENTION
    col.decks.update_config(conf)

    col.decks.select(col.decks.id(DEFAULT_DECK))


def _ensure_notetypes(col: Collection) -> None:
    if col.models.by_name(BIBELVERS) is not None:
        return
    mm = col.models
    m = mm.new(BIBELVERS)
    for name in ("Referenz", "Text", "Übersetzung"):
        mm.add_field(m, mm.new_field(name))
    t = mm.new_template("Referenz → Text")
    t["qfmt"] = BIBELVERS_QFMT
    t["afmt"] = BIBELVERS_AFMT
    mm.add_template(m, t)
    m["css"] = BIBELVERS_CSS
    mm.add(m)


async def ensure_collection(user_id: str) -> None:
    """Create the user's collection if it does not exist yet."""
    await _run(user_id, lambda col: None)


# --------------------------------------------------------------------------- decks


def _deck_list(col: Collection) -> list[DeckInfo]:
    result: list[DeckInfo] = []

    def walk(node, depth: int, prefix: str) -> None:
        for child in node.children:
            full = f"{prefix}::{child.name}" if prefix else child.name
            result.append(
                DeckInfo(
                    id=child.deck_id,
                    name=full,
                    short_name=child.name,
                    depth=depth,
                    new=child.new_count,
                    learn=child.learn_count,
                    review=child.review_count,
                )
            )
            walk(child, depth + 1, full)

    walk(col.sched.deck_due_tree(), 0, "")
    return result


async def deck_list(user_id: str) -> list[DeckInfo]:
    """Deck tree flattened in display order, with new/learn/review counts."""
    return await _run(user_id, _deck_list)


def _deck_names(col: Collection) -> list[tuple[int, str]]:
    return [
        (d.id, d.name)
        for d in col.decks.all_names_and_ids(skip_empty_default=True, include_filtered=False)
    ]


async def deck_names(user_id: str) -> list[tuple[int, str]]:
    """(id, full name) of all normal decks, for selects."""
    return await _run(user_id, _deck_names)


def _require_deck(col: Collection, deck_id: int) -> dict:
    deck = col.decks.get(deck_id, default=False)
    if deck is None:
        raise NotFound("Deck nicht gefunden")
    return deck


def _create_deck(col: Collection, name: str) -> int:
    name = "::".join(part.strip() for part in name.split("::") if part.strip())
    if not name:
        raise AnkiServiceError("Bitte einen Decknamen eingeben.")
    return int(col.decks.id(name))


async def create_deck(user_id: str, name: str) -> int:
    """Create a deck (or return the existing one with that name)."""
    return await _run(user_id, _create_deck, name)


# --------------------------------------------------------------------------- review


def _can_undo_review(col: Collection) -> bool:
    return col.undo_status().undo == col.tr.actions_answer_card()


def _review_state(col: Collection, deck_id: int) -> ReviewState:
    deck = _require_deck(col, deck_id)
    col.decks.select(deck_id)  # the v3 queue is scoped to the current deck
    queued = col.sched.get_queued_cards(fetch_limit=1)
    card_data = None
    if queued.cards:
        qc = queued.cards[0]
        card = col.get_card(qc.card.id)
        card_data = ReviewCard(
            card_id=card.id,
            question=col.media.strip_av_tags(card.question()),
            answer=col.media.strip_av_tags(card.answer()),
            labels=[s.translate(_LABEL_MARKS) for s in col.sched.describe_next_states(qc.states)],
            queue=qc.queue,
        )
    return ReviewState(
        deck_id=deck_id,
        deck_name=deck["name"],
        card=card_data,
        new=queued.new_count,
        learn=queued.learning_count,
        review=queued.review_count,
        can_undo=_can_undo_review(col),
    )


async def review_state(user_id: str, deck_id: int) -> ReviewState:
    """Next card of the deck (or None) plus remaining counts."""
    return await _run(user_id, _review_state, deck_id)


def _answer(col: Collection, deck_id: int, card_id: int, rating: int, taken_secs: float):
    if rating not in RATINGS:
        raise ValueError(f"invalid rating {rating}")
    _require_deck(col, deck_id)
    col.decks.select(deck_id)
    queued = col.sched.get_queued_cards(fetch_limit=1)
    answered = False
    # Only answer if the card is still the current one (protects against double taps).
    if queued.cards and queued.cards[0].card.id == card_id:
        card = col.get_card(card_id)
        card.timer_started = time.time() - max(0.0, taken_secs)
        col.sched.answer_card(
            col.sched.build_answer(card=card, states=queued.cards[0].states, rating=RATINGS[rating])
        )
        answered = True
    return _review_state(col, deck_id), answered


async def answer_card(
    user_id: str, deck_id: int, card_id: int, rating: int, taken_secs: float
) -> tuple[ReviewState, bool]:
    """Answer the current card (1=Again … 4=Easy). Returns the next state and whether
    the answer was recorded (False if `card_id` was no longer the current card)."""
    return await _run(user_id, _answer, deck_id, card_id, rating, taken_secs)


def _undo(col: Collection, deck_id: int) -> tuple[ReviewState, bool]:
    undone = False
    if _can_undo_review(col):
        col.undo()
        undone = True
    return _review_state(col, deck_id), undone


async def undo_review(user_id: str, deck_id: int) -> tuple[ReviewState, bool]:
    """Undo the last answer, if the last undoable operation was an answer."""
    return await _run(user_id, _undo, deck_id)


# --------------------------------------------------------------------------- notes


def _notetypes_for_adding(col: Collection) -> list[NotetypeInfo]:
    result = []
    for name in (BIBELVERS, BASIC):
        m = col.models.by_name(name)
        if m is not None:
            result.append(NotetypeInfo(m["id"], m["name"], [f["name"] for f in m["flds"]]))
    return result


async def notetypes_for_adding(user_id: str) -> list[NotetypeInfo]:
    """The note types offered in the add form (Bibelvers, Basic)."""
    return await _run(user_id, _notetypes_for_adding)


def _get_note_obj(col: Collection, note_id: int):
    try:
        return col.get_note(note_id)
    except NotFoundError as exc:
        raise NotFound("Notiz nicht gefunden") from exc


def _set_fields(note, fields: dict[str, str]) -> None:
    for name in note.keys():
        if name in fields:
            note[name] = fields[name]
    if note.fields_check() == NoteFieldsCheckResult.EMPTY:
        raise EmptyNote("Das erste Feld darf nicht leer sein.")


def _add_note(col: Collection, notetype: str, deck_id: int, fields: dict[str, str], tags):
    m = col.models.by_name(notetype)
    if m is None or notetype not in (BASIC, BIBELVERS):
        raise NotFound("Notiztyp nicht gefunden")
    _require_deck(col, deck_id)
    note = col.new_note(m)
    _set_fields(note, fields)
    note.tags = list(tags)
    col.add_note(note, deck_id)
    return int(note.id)


async def add_note(
    user_id: str,
    notetype: str,
    deck_id: int,
    fields: dict[str, str],
    tags: tuple[str, ...] = (),
) -> int:
    """Add a note. Field values must already be sanitized. Returns the note id."""
    return await _run(user_id, _add_note, notetype, deck_id, fields, tags)


def _note_data(col: Collection, note_id: int) -> NoteData:
    note = _get_note_obj(col, note_id)
    cards = note.cards()
    return NoteData(
        id=int(note.id),
        notetype=note.note_type()["name"],
        fields=list(note.items()),
        deck_id=int(cards[0].did) if cards else 0,
        tags=list(note.tags),
        readonly=note.has_tag(GROUP_TAG),
        suspended=bool(cards) and all(c.queue == QUEUE_SUSPENDED for c in cards),
    )


async def get_note(user_id: str, note_id: int) -> NoteData:
    return await _run(user_id, _note_data, note_id)


def _update_note(col: Collection, note_id, fields, deck_id, allow_group: bool) -> None:
    note = _get_note_obj(col, note_id)
    if note.has_tag(GROUP_TAG) and not allow_group:
        raise ReadOnlyNote("Karten aus Gruppendecks können nicht bearbeitet werden.")
    _require_deck(col, deck_id)
    _set_fields(note, fields)
    col.update_note(note)
    card_ids = [c.id for c in note.cards() if c.did != deck_id]
    if card_ids:
        col.set_deck(card_ids, deck_id)


async def update_note(
    user_id: str, note_id: int, fields: dict[str, str], deck_id: int, allow_group: bool = False
) -> None:
    """Update fields (already sanitized) and move the cards to `deck_id`."""
    await _run(user_id, _update_note, note_id, fields, deck_id, allow_group)


def _delete_note(col: Collection, note_id: int, allow_group: bool) -> None:
    note = _get_note_obj(col, note_id)
    if note.has_tag(GROUP_TAG) and not allow_group:
        raise ReadOnlyNote("Karten aus Gruppendecks können nicht gelöscht werden.")
    col.remove_notes([note.id])


async def delete_note(user_id: str, note_id: int, allow_group: bool = False) -> None:
    await _run(user_id, _delete_note, note_id, allow_group)


def _set_suspended(col: Collection, note_id: int, suspended: bool) -> None:
    card_ids = list(_get_note_obj(col, note_id).card_ids())
    if suspended:
        col.sched.suspend_cards(card_ids)
    else:
        col.sched.unsuspend_cards(card_ids)


async def set_suspended(user_id: str, note_id: int, suspended: bool) -> None:
    """Suspend or unsuspend all cards of a note (allowed for group decks too)."""
    await _run(user_id, _set_suspended, note_id, suspended)


# --------------------------------------------------------------------------- browse


def _due_text(col: Collection, card) -> str:
    if card.queue == QUEUE_SUSPENDED:
        return "ausgesetzt"
    if card.type == CARD_TYPE_NEW:
        return "neu"
    stats = col.card_stats_data(card.id)
    if stats.HasField("due_date"):
        return datetime.fromtimestamp(stats.due_date).strftime("%d.%m.%Y")
    return "–"


def _search(col: Collection, query: str, limit: int) -> tuple[list[CardRow], int]:
    try:
        card_ids = col.find_cards(query.strip() or "deck:*", order=True)
    except SearchError as exc:
        raise InvalidSearch(str(exc)) from exc
    rows = []
    for cid in card_ids[:limit]:
        card = col.get_card(cid)
        note = card.note()
        rows.append(
            CardRow(
                card_id=int(card.id),
                note_id=int(note.id),
                front=plain_text(note.fields[0])[:80],
                deck=col.decks.name(card.did),
                due=_due_text(col, card),
                suspended=card.queue == QUEUE_SUSPENDED,
            )
        )
    return rows, len(card_ids)


async def search_cards(user_id: str, query: str, limit: int = 200) -> tuple[list[CardRow], int]:
    """Search with Anki search syntax. Returns (first `limit` rows, total matches)."""
    return await _run(user_id, _search, query, limit)


# --------------------------------------------------------------------------- deck options


def _get_deck_options(col: Collection, deck_id: int) -> DeckOptions:
    _require_deck(col, deck_id)
    conf = col.decks.config_dict_for_deck_id(deck_id)
    return DeckOptions(
        new_per_day=conf["new"]["perDay"],
        reviews_per_day=conf["rev"]["perDay"],
        desired_retention=round(conf.get("desiredRetention", DEFAULT_RETENTION), 2),
    )


async def get_deck_options(user_id: str, deck_id: int) -> DeckOptions:
    return await _run(user_id, _get_deck_options, deck_id)


def _set_deck_options(col: Collection, deck_id: int, opts: DeckOptions) -> None:
    deck = _require_deck(col, deck_id)
    conf = col.decks.config_dict_for_deck_id(deck_id)
    sharing = [d for d in col.decks.all() if not d["dyn"] and d.get("conf") == conf["id"]]
    if len(sharing) > 1:
        # Options are per deck in our UI, so give this deck its own preset.
        new_id = col.decks.add_config_returning_id(deck["name"], clone_from=conf)
        col.decks.set_config_id_for_deck_dict(deck, new_id)
        conf = col.decks.get_config(new_id)
    conf["new"]["perDay"] = opts.new_per_day
    conf["rev"]["perDay"] = opts.reviews_per_day
    conf["desiredRetention"] = opts.desired_retention
    col.decks.update_config(conf)


async def set_deck_options(user_id: str, deck_id: int, opts: DeckOptions) -> None:
    """Save options for this deck only (splits off a preset if it was shared)."""
    await _run(user_id, _set_deck_options, deck_id, opts)


# --------------------------------------------------------------------------- export / import


def _export_collection(col: Collection, out_path: str) -> None:
    try:
        col.export_collection_package(out_path, include_media=False, legacy=False)
    finally:
        col.reopen()  # export_collection_package closes the collection


async def export_collection(user_id: str, out_path: Path) -> None:
    """Whole collection incl. scheduling as .colpkg."""
    await _run(user_id, _export_collection, str(out_path))


def _export_deck(col: Collection, deck_id: int, out_path: str, with_scheduling: bool) -> int:
    _require_deck(col, deck_id)
    return col.export_anki_package(
        out_path=out_path,
        options=ExportAnkiPackageOptions(
            with_scheduling=with_scheduling,
            with_deck_configs=with_scheduling,
            with_media=False,
            legacy=False,
        ),
        limit=DeckIdLimit(deck_id=deck_id),
    )


async def export_deck(
    user_id: str, deck_id: int, out_path: Path, with_scheduling: bool = True
) -> int:
    """One deck (incl. subdecks) as .apkg. Returns the number of exported notes."""
    return await _run(user_id, _export_deck, deck_id, str(out_path), with_scheduling)


def _import_package(col: Collection, path: str, with_scheduling: bool) -> int:
    log_ = col.import_anki_package(
        ImportAnkiPackageRequest(
            package_path=path,
            options=ImportAnkiPackageOptions(
                with_scheduling=with_scheduling,
                with_deck_configs=with_scheduling,
                update_notes=ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_ALWAYS,
                update_notetypes=ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_IF_NEWER,
                merge_notetypes=True,
            ),
        )
    )
    return len(log_.log.new) + len(log_.log.updated)


async def import_package(user_id: str, path: Path, with_scheduling: bool = False) -> int:
    """Import an .apkg; existing notes are updated by GUID. Returns new+updated count."""
    return await _run(user_id, _import_package, str(path), with_scheduling)


# --------------------------------------------------------------------------- stats


def _stats(col: Collection, days: int) -> CollectionStats:
    tree = col.sched.deck_due_tree()
    cutoff = col.sched.day_cutoff  # start of tomorrow (epoch secs, local rollover)
    day_ms = 86_400_000
    end_ms = cutoff * 1000
    rows = col.db.all(
        "select id, cid, ease, type from revlog where ease > 0 and id >= ?",
        end_ms - 366 * day_ms,
    )
    today_start = datetime.fromtimestamp(cutoff) - timedelta(days=1)
    by_day: dict[int, DailyCount] = {}
    new_cards: dict[int, set] = {}
    passed = failed = 0
    for rid, cid, ease, rtype in rows:
        idx = (end_ms - rid - 1) // day_ms  # 0 = today
        if idx < days:
            dc = by_day.setdefault(idx, DailyCount((today_start - timedelta(days=idx)).date(), 0, 0, 0))
            dc.reviews += 1
            dc.again += ease == 1
            if rtype == 0:
                new_cards.setdefault(idx, set()).add(cid)
        if idx < 30 and rtype == 1:
            if ease > 1:
                passed += 1
            else:
                failed += 1
    for idx, cids in new_cards.items():
        by_day[idx].new = len(cids)

    active = {(end_ms - rid - 1) // day_ms for rid, *_ in rows}
    streak = 0
    day = 0 if 0 in active else 1  # a streak survives until today's reviews are done
    while day in active:
        streak += 1
        day += 1

    last_id = max((rid for rid, *_ in rows), default=None)
    return CollectionStats(
        total_cards=col.card_count(),
        due_today=tree.learn_count + tree.review_count,
        new_today=tree.new_count,
        reviews_today=by_day[0].reviews if 0 in by_day else 0,
        retention_30d=passed / (passed + failed) if passed + failed else None,
        streak_days=streak,
        last_review_at=datetime.fromtimestamp(last_id / 1000) if last_id else None,
        daily=[by_day[i] for i in sorted(by_day)],
    )


async def collection_stats(user_id: str, days: int = 30) -> CollectionStats:
    """Figures for the dashboard cache (user_stats / daily_reviews)."""
    return await _run(user_id, _stats, days)


# --------------------------------------------------------------------------- backup


async def backup_collection(user_id: str, dest_dir: Path) -> bool:
    """Copy the collection file to `dest_dir` (closed first so the copy is consistent).
    Returns False if the user has no collection."""
    path = collection_path(user_id)
    async with _lock(user_id):
        with _cache_lock:
            entry = _open.pop(user_id, None)

        def copy() -> bool:
            if entry is not None:
                entry.col.close()
            if not path.exists():
                return False
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest_dir / path.name)
            return True

        return await run_in_threadpool(copy)


def collection_ids() -> list[str]:
    """Ids of all collections on disk."""
    base = collections_dir()
    if not base.exists():
        return []
    return sorted(p.parent.name for p in base.glob("*/collection.anki2"))

"""The ONLY module that imports `anki`.

Every collection access runs inside the per-user asyncio lock and in the threadpool.
Routes call the public `async` functions below; the `_sync` helpers receive an open
Collection and must never be called from anywhere else.

Verified against anki==26.9.3 (see SPEC §6, spike results).
"""

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
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
from anki.deck_config_pb2 import UpdateDeckConfigsMode
from anki.decks import UpdateDeckConfigs
from anki.errors import NotFoundError, SearchError
from anki.import_export_pb2 import ImportAnkiPackageUpdateCondition
from anki.notes import NoteFieldsCheckResult
from anki.scheduler.v3 import CardAnswer
from anki.utils import ids2str
from starlette.concurrency import run_in_threadpool

from app.bible_memorizer import gen_notes as bm_gen
from app.bible_memorizer import model as bm_model
from app.config import get_settings
from app.i18n import N_, get_language, translate
from app.sanitize import MEDIA_NAME, plain_text

log = logging.getLogger(__name__)

ADMIN_COLLECTION = "_admin"
MAX_OPEN = 20
IDLE_SECONDS = 600
GROUP_TAG = "gruppendeck"
DEFAULT_DECK = N_("My cards")  # created in the user's language
BASIC = "Basic"
BIBLE_VERSE = "Bible Verse"
BIBLE_MEMORIZER = bm_model.MODEL_NAME  # "Bible Memorizer"
DEFAULT_NEW_PER_DAY = 5
DEFAULT_REVIEWS_PER_DAY = 200
BURY_SIBLINGS_KEY = "crosspointBurySiblings"  # collection config: one-time upgrade done
ALL_DECKS = 0  # pseudo deck id: review all top-level decks in one session
ALL_DECKS_NAME = N_("All decks")
MATURE_IVL = 21  # days; Anki's "mature" threshold, shown as "memorized"
DEFAULT_RETENTION = 0.9
# Collections are created with an English backend so stock names stay "Basic"/"Default"
# (a German backend would create "Einfach"); afterwards they are opened with a backend in
# the UI language so interval labels are localized.
CREATE_LANG = "en"
SEED_PATH = Path(__file__).resolve().parent / "seed" / "notetypes.apkg"
SEED_DECK = "_crosspoint_seed"  # baked into app/seed/notetypes.apkg
SEED_IDS = {BASIC: 1793123435605, BIBLE_VERSE: 1793123435615, BIBLE_MEMORIZER: 1793123435620}  # from `python -m app.cli build-seed`

# Field holding the expected answer for typing mode, per (notetype, card ordinal).
TYPE_FIELDS = {
    ("Bible Verse", 0): "Text",
    ("Bible Verse", 1): "Reference",
    ("Basic", 0): "Back",
    ("Bible Memorizer", 0): "answer",
    ("Bible Memorizer", 1): "answer",
    ("Bible Memorizer", 2): "answer",
}

RATINGS = {1: CardAnswer.AGAIN, 2: CardAnswer.HARD, 3: CardAnswer.GOOD, 4: CardAnswer.EASY}
QUEUE_SUSPENDED = -1
CARD_TYPE_NEW = 0

_USER_ID = re.compile(r"^[0-9A-Za-z_]{1,40}$")
# describe_next_states() wraps numbers in Unicode isolates (U+2068/U+2069).
_LABEL_MARKS = str.maketrans("", "", "⁨⁩")

BIBLE_VERSE_CSS = """\
.card {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 24px;
  line-height: 1.5;
  text-align: center;
  color: #1d1d1f;
  background-color: #ffffff;
  padding: 0 12px;
}
.translation { font-size: 14px; opacity: 0.6; margin-top: 4px; }
.text { text-align: left; display: inline-block; max-width: 36em; }
.card.nightMode, .nightMode .card { color: #ececef; background-color: #1f1f23; }
@media (prefers-color-scheme: dark) {
  .card { color: #ececef; background-color: #1f1f23; }
}
"""
BIBLE_VERSE_QFMT = (
    "{{Reference}}"
    "{{#Translation}}<div class=translation>{{Translation}}</div>{{/Translation}}"
)
BIBLE_VERSE_AFMT = "{{FrontSide}}\n\n<hr id=answer>\n\n<div class=text>{{Text}}</div>"
# Card 2 (optional, like Anki's "Basic (optional reversed card)"): only if Reverse is filled.
BIBLE_VERSE_REVERSE_FIELD = "Reverse"
BIBLE_VERSE_REVERSE_NAME = "Text → Reference"
BIBLE_VERSE_REVERSE_QFMT = "{{#Reverse}}<div class=text>{{Text}}</div>{{/Reverse}}"
BIBLE_VERSE_REVERSE_AFMT = (
    "{{FrontSide}}\n\n<hr id=answer>\n\n{{Reference}}"
    "{{#Translation}}<div class=translation>{{Translation}}</div>{{/Translation}}"
)


class AnkiServiceError(Exception):
    """Base class for errors the UI shows to the user."""


class EmptyNote(AnkiServiceError):
    pass


class ReadOnlyNote(AnkiServiceError):
    pass


class InvalidSearch(AnkiServiceError):
    pass


class EditConflict(AnkiServiceError):
    """The note was changed by someone else since the edit form was loaded."""


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
    total: int = 0  # active cards incl. subdecks (suspended excluded)
    mature: int = 0  # of those, interval >= MATURE_IVL
    group: bool = False  # contains group deck cards


@dataclass
class ReviewCard:
    card_id: int
    question: str
    answer: str
    labels: list[str]  # interval preview for Again/Hard/Good/Easy
    queue: int
    can_type: bool = False  # typing mode available (expected answer is known)


@dataclass
class ReviewState:
    deck_id: int
    deck_name: str
    card: ReviewCard | None
    new: int
    learn: int
    review: int
    can_undo: bool
    done_today: int = 0  # answers given today in this deck (incl. subdecks) – progress bar


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
    generated: bool = False  # Bible Memorizer notes are generated from a text, not edited
    deck_ids: list[int] = field(default_factory=list)  # decks of all cards
    mod: int = 0  # modification time (seconds): detects concurrent edits


@dataclass
class CardRow:
    card_id: int
    note_id: int
    front: str
    deck: str
    status: str  # "new" | "suspended" | "scheduled"
    due: date | None
    suspended: bool
    template: str = ""  # card type, e.g. "Reverse"
    siblings: int = 1  # cards of the note


@dataclass
class NoteRow:
    note_id: int
    front: str
    deck: str  # deck of the first card ("…" appended if the cards are spread over decks)
    cards: int
    suspended_cards: int
    status: str  # "new" | "suspended" | "scheduled"
    due: date | None  # earliest due date of the active scheduled cards


@dataclass
class DeckOptions:
    new_per_day: int
    reviews_per_day: int
    desired_retention: float
    bury_new: bool = True  # sibling cards of new cards on different days
    bury_reviews: bool = True  # sibling cards of reviews on different days


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
    lang: str


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


def _get_col(user_id: str, lang: str) -> Collection:
    """Return the open collection, opening (and creating) it if needed. The backend
    language follows the UI language; a cached collection is reopened if it differs.

    Must run in the threadpool while holding the user's lock.
    """
    with _cache_lock:
        entry = _open.get(user_id)
        if entry is not None:
            _open.move_to_end(user_id)
            entry.last_used = time.monotonic()
            if entry.lang == lang:
                return entry.col
            del _open[user_id]
    if entry is not None:
        entry.col.close()
    path = collection_path(user_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        col = Collection(str(path), backend=RustBackend(langs=[CREATE_LANG]))
        try:
            _setup_new_collection(col, translate(DEFAULT_DECK, lang))
        finally:
            col.close()
    col = Collection(str(path), backend=RustBackend(langs=[lang]))
    try:
        _ensure_notetypes(col)
        _upgrade_bury_siblings(col)
    except Exception:
        col.close()
        raise
    with _cache_lock:
        _open[user_id] = _Entry(col, time.monotonic(), lang)
    return col


async def _run(user_id: str, fn: Callable[..., T], *args: Any) -> T:
    lang = get_language()  # read on the event loop, where the request context lives
    async with _lock(user_id):
        result = await run_in_threadpool(lambda: fn(_get_col(user_id, lang), *args))
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


def _setup_new_collection(col: Collection, deck_name: str) -> None:
    col.set_config("fsrs", True)
    prefs = col.get_preferences()
    prefs.scheduling.rollover = 4
    col.set_preferences(prefs)
    _ensure_notetypes(col)

    conf = col.decks.config_dict_for_deck_id(1)
    conf["new"]["perDay"] = DEFAULT_NEW_PER_DAY
    conf["rev"]["perDay"] = DEFAULT_REVIEWS_PER_DAY
    conf["desiredRetention"] = DEFAULT_RETENTION
    conf["new"]["bury"] = conf["rev"]["bury"] = True
    col.decks.update_config(conf)
    col.set_config(BURY_SIBLINGS_KEY, True)

    col.decks.select(col.decks.id(deck_name))


def _upgrade_bury_siblings(col: Collection) -> None:
    """One-time upgrade of existing collections: show sibling cards (e.g. a verse and its
    reverse card) on different days, for new cards and reviews, in every options preset.
    Runs once per collection (marker in the collection config), so later choices stay."""
    if col.get_config(BURY_SIBLINGS_KEY, False):
        return
    for conf in col.decks.all_config():
        conf["new"]["bury"] = conf["rev"]["bury"] = True
        col.decks.update_config(conf)
    col.set_config(BURY_SIBLINGS_KEY, True)


def _ensure_notetypes(col: Collection) -> None:
    """Make sure Basic, Bible Verse and Bible Memorizer exist with the shared seed ids.

    Notetype ids must be identical in all collections, otherwise importing a group deck
    creates duplicates ("Basic+"). Ids cannot be set via the API, but an import keeps
    them – so the canonical notetypes come from the seed package (app/seed/).
    Unused notetypes with a foreign id (fresh collections) are replaced.
    """
    missing = False
    for name, nt_id in SEED_IDS.items():
        m = col.models.by_name(name)
        if m is not None and m["id"] == nt_id:
            continue
        if m is not None and col.models.use_count(m) == 0:
            col.models.remove(m["id"])
            m = None
        if m is None and col.models.get(nt_id) is None:
            missing = True
    if missing:
        _import_seed(col)
    _upgrade_notetypes(col)


def _upgrade_notetypes(col: Collection) -> None:
    """Bring the shared notetypes up to the current definition in place (ids stay).
    Idempotent; runs on every open and when building the seed."""
    mm = col.models
    m = mm.get(SEED_IDS[BIBLE_VERSE]) or mm.by_name(BIBLE_VERSE)
    if m is None:
        return
    changed = False
    if BIBLE_VERSE_REVERSE_FIELD not in mm.field_names(m):
        mm.add_field(m, mm.new_field(BIBLE_VERSE_REVERSE_FIELD))
        changed = True
    if not any(t["name"] == BIBLE_VERSE_REVERSE_NAME for t in m["tmpls"]):
        t = mm.new_template(BIBLE_VERSE_REVERSE_NAME)
        t["qfmt"] = BIBLE_VERSE_REVERSE_QFMT
        t["afmt"] = BIBLE_VERSE_REVERSE_AFMT
        mm.add_template(m, t)
        changed = True
    if changed:
        mm.save(m)


def _import_seed(col: Collection) -> None:
    col.import_anki_package(
        ImportAnkiPackageRequest(package_path=str(SEED_PATH), options=ImportAnkiPackageOptions())
    )
    seed_deck = col.decks.id(SEED_DECK, create=False)
    if seed_deck:
        col.remove_notes(col.find_notes(f"did:{seed_deck}"))
        col.decks.remove([seed_deck])


def build_seed_package(out_path: Path, fresh: bool = False) -> dict[str, int]:
    """Write the seed .apkg (one dummy note per notetype). Returns the notetype ids.

    By default the current seed is the starting point, so ids stay the same and only
    the notetype definitions are updated (_upgrade_notetypes). `fresh=True` creates new
    notetypes with NEW ids – only before any collection exists (breaks group decks).
    """
    with tempfile.TemporaryDirectory() as tmp:
        col = Collection(str(Path(tmp) / "collection.anki2"), backend=RustBackend(langs=[CREATE_LANG]))
        try:
            if fresh or not SEED_PATH.exists():
                _add_bible_verse_notetype(col)
                _add_bible_memorizer_notetype(col)
                _upgrade_notetypes(col)
            else:
                _ensure_notetypes(col)  # import the current seed, then upgrade in place
            did = col.decks.id(SEED_DECK)
            for name in SEED_IDS:
                note = col.new_note(col.models.by_name(name))
                note.fields[0] = "seed"
                col.add_note(note, did)
            tmp_out = Path(tmp) / "seed.apkg"
            col.export_anki_package(
                out_path=str(tmp_out),
                options=ExportAnkiPackageOptions(
                    with_scheduling=False, with_deck_configs=False, with_media=False, legacy=False
                ),
                limit=DeckIdLimit(deck_id=did),
            )
            shutil.move(str(tmp_out), out_path)
            return {name: int(col.models.by_name(name)["id"]) for name in SEED_IDS}
        finally:
            col.close()


def _add_bible_verse_notetype(col: Collection) -> None:
    mm = col.models
    m = mm.new(BIBLE_VERSE)
    for name in ("Reference", "Text", "Translation"):
        mm.add_field(m, mm.new_field(name))
    t = mm.new_template("Reference → Text")
    t["qfmt"] = BIBLE_VERSE_QFMT
    t["afmt"] = BIBLE_VERSE_AFMT
    mm.add_template(m, t)
    m["css"] = BIBLE_VERSE_CSS
    mm.add(m)


def _add_bible_memorizer_notetype(col: Collection) -> None:
    """Identical to the Anki add-on's note type (fields, templates, CSS, sort field)."""
    mm = col.models
    m = mm.new(bm_model.MODEL_NAME)
    for name in bm_model.FIELD_NAMES:
        mm.add_field(m, mm.new_field(name))
    for name, front, back in bm_model.TEMPLATES:
        t = mm.new_template(name)
        t["qfmt"] = front
        t["afmt"] = back
        mm.add_template(m, t)
    m["css"] = bm_model.CSS
    m["sortf"] = bm_model.FIELD_NAMES.index(bm_model.SORT_FIELD)
    mm.add(m)


async def ensure_collection(user_id: str) -> None:
    """Create the user's collection if it does not exist yet."""
    await _run(user_id, lambda col: None)


# --------------------------------------------------------------------------- decks


def _deck_list(col: Collection) -> list[DeckInfo]:
    result: list[DeckInfo] = []
    counts = {
        did: (total, mature)
        for did, total, mature in col.db.all(
            "select case when odid then odid else did end, count(), sum(type = 2 and ivl >= ?)"
            " from cards where queue != -1 group by 1",
            MATURE_IVL,
        )
    }
    group_dids = set(col.db.list(
        "select distinct case when c.odid then c.odid else c.did end from cards c"
        " join notes n on n.id = c.nid where n.tags like ?",
        f"% {GROUP_TAG} %",
    ))

    def walk(node, depth: int, prefix: str) -> tuple[int, int]:
        total = mature = 0
        for child in node.children:
            full = f"{prefix}::{child.name}" if prefix else child.name
            info = DeckInfo(
                id=child.deck_id,
                name=full,
                short_name=child.name,
                depth=depth,
                new=child.new_count,
                learn=child.learn_count,
                review=child.review_count,
                group=child.deck_id in group_dids,
            )
            result.append(info)
            sub_total, sub_mature = walk(child, depth + 1, full)
            own_total, own_mature = counts.get(child.deck_id, (0, 0))
            info.total, info.mature = own_total + sub_total, own_mature + sub_mature
            total += info.total
            mature += info.mature
        return total, mature

    walk(col.sched.deck_due_tree(), 0, "")
    return result


async def deck_list(user_id: str) -> list[DeckInfo]:
    """Deck tree flattened in display order, with new/learn/review counts and
    total/mature card counts (subdecks included)."""
    return await _run(user_id, _deck_list)


def _deck_names(col: Collection) -> list[tuple[int, str]]:
    return [
        (d.id, d.name)
        for d in col.decks.all_names_and_ids(skip_empty_default=True, include_filtered=False)
    ]


async def deck_names(user_id: str) -> list[tuple[int, str]]:
    """(id, full name) of all normal decks, for selects."""
    return await _run(user_id, _deck_names)


def _current_deck_id(col: Collection) -> int:
    did = int(col.decks.get_current_id())
    names = dict(_deck_names(col))
    return did if did in names else next(iter(names))


async def current_deck_id(user_id: str) -> int:
    """The last used deck (created as "My cards"); default for forms."""
    return await _run(user_id, _current_deck_id)


def _require_deck(col: Collection, deck_id: int) -> dict:
    deck = col.decks.get(deck_id, default=False)
    if deck is None:
        raise NotFound(N_("Deck not found."))
    return deck


def _create_deck(col: Collection, name: str) -> int:
    name = "::".join(part.strip() for part in name.split("::") if part.strip())
    if not name:
        raise AnkiServiceError(N_("Please enter a deck name."))
    return int(col.decks.id(name))


async def create_deck(user_id: str, name: str) -> int:
    """Create a deck (or return the existing one with that name)."""
    return await _run(user_id, _create_deck, name)


def _has_group_cards(col: Collection, deck_name: str) -> bool:
    return bool(col.find_cards(f"{deck_search(deck_name)} tag:{GROUP_TAG}"))


def _is_group_deck(col: Collection, deck_id: int) -> bool:
    return _has_group_cards(col, _require_deck(col, deck_id)["name"])


async def is_group_deck(user_id: str, deck_id: int) -> bool:
    """The deck or one of its subdecks contains group deck cards."""
    return await _run(user_id, _is_group_deck, deck_id)


def _rename_deck(col: Collection, deck_id: int, name: str, allow_group: bool,
                 keep_parent: bool) -> tuple[str, str]:
    deck = _require_deck(col, deck_id)
    old = deck["name"]
    name = "::".join(part.strip() for part in name.split("::") if part.strip())
    if not name:
        raise AnkiServiceError(N_("Please enter a deck name."))
    if keep_parent:
        parent, _sep, _last = old.rpartition("::")
        new_parent, _sep, last = name.rpartition("::")
        if new_parent and new_parent.casefold() != parent.casefold():
            raise AnkiServiceError(N_("Group decks can only be renamed, not moved to another "
                                      "parent deck."))
        name = f"{parent}::{last}" if parent else last
    if name == old:
        return old, name
    # Anki itself would append "+" to a taken name and accepts moving a deck below itself.
    if name.casefold().startswith(old.casefold() + "::"):
        raise AnkiServiceError(N_("A deck cannot be moved into its own subdeck."))
    existing = col.decks.id_for_name(name)
    if existing and existing != deck_id:
        raise AnkiServiceError(N_("A deck with this name already exists."))
    if not allow_group and _has_group_cards(col, old):
        # Distributing again would put new cards into a deck with the old name.
        raise AnkiServiceError(N_("Group decks cannot be renamed."))
    col.decks.rename(deck_id, name)
    return old, name


async def rename_deck(user_id: str, deck_id: int, name: str, allow_group: bool = False,
                      keep_parent: bool = False) -> tuple[str, str]:
    """Rename a deck (subdecks follow; `::` moves it unless `keep_parent`).
    Returns (old, new) full name. Decks with group deck cards only with `allow_group`."""
    return await _run(user_id, _rename_deck, deck_id, name, allow_group, keep_parent)


def _rename_deck_by_name(col: Collection, old: str, new: str) -> bool:
    deck = col.decks.by_name(old)
    if deck is None:
        return False
    existing = col.decks.id_for_name(new)
    if existing and existing != deck["id"]:
        raise AnkiServiceError(N_("A deck with this name already exists."))
    col.decks.rename(deck["id"], new)
    return True


async def rename_deck_by_name(user_id: str, old: str, new: str) -> bool:
    """Rename a group deck in a member collection (decks are matched by name when
    distributing). False if the member has no such deck."""
    return await _run(user_id, _rename_deck_by_name, old, new)


# --------------------------------------------------------------------------- review


def _can_undo_review(col: Collection) -> bool:
    return col.undo_status().undo == col.tr.actions_answer_card()


def _queue(col: Collection, deck_id: int):
    """Select the deck and fetch its next queued card.

    The v3 queue is scoped to the current deck, so ALL_DECKS serves the top-level decks one
    after another (the current one first, so a session stays in its deck) with the counts of
    the whole tree. Selecting another deck is an undo step of its own, so only the deck that
    serves the card is selected. Returns (deck name, queued card or None, (new, learn, review)).
    """
    if deck_id != ALL_DECKS:
        deck = _require_deck(col, deck_id)
        col.decks.select(deck_id)
        queued = col.sched.get_queued_cards(fetch_limit=1)
        first = queued.cards[0] if queued.cards else None
        return deck["name"], first, (queued.new_count, queued.learning_count, queued.review_count)
    tree = col.sched.deck_due_tree()
    counts = (tree.new_count, tree.learn_count, tree.review_count)
    current = col.decks.get_current_id()
    pending = [n for n in tree.children if n.new_count + n.learn_count + n.review_count]
    for node in sorted(pending, key=lambda n: n.deck_id != current):
        col.decks.select(node.deck_id)
        queued = col.sched.get_queued_cards(fetch_limit=1)
        if queued.cards:
            return ALL_DECKS_NAME, queued.cards[0], counts  # name translated by the route
    return ALL_DECKS_NAME, None, counts


def _answers_today(col: Collection, deck_id: int) -> int:
    since = (col.sched.day_cutoff - 86_400) * 1000
    if deck_id == ALL_DECKS:
        return col.db.scalar("select count() from revlog where id >= ? and ease > 0", since)
    dids = ids2str(col.decks.deck_and_child_ids(deck_id))
    return col.db.scalar(
        "select count() from revlog where id >= ? and ease > 0 and cid in "
        f"(select id from cards where did in {dids} or odid in {dids})", since)


def _review_state(col: Collection, deck_id: int) -> ReviewState:
    deck_name, qc, (new, learn, review) = _queue(col, deck_id)
    card_data = None
    if qc is not None:
        card = col.get_card(qc.card.id)
        card_data = ReviewCard(
            card_id=card.id,
            question=col.media.strip_av_tags(card.question()),
            answer=col.media.strip_av_tags(card.answer()),
            labels=[s.translate(_LABEL_MARKS) for s in col.sched.describe_next_states(qc.states)],
            queue=qc.queue,
            can_type=_type_field(card) is not None,
        )
    return ReviewState(
        deck_id=deck_id,
        deck_name=deck_name,
        card=card_data,
        new=new,
        learn=learn,
        review=review,
        can_undo=_can_undo_review(col),
        done_today=_answers_today(col, deck_id),
    )


async def review_state(user_id: str, deck_id: int) -> ReviewState:
    """Next card of the deck (or None) plus remaining counts; deck_id may be ALL_DECKS."""
    return await _run(user_id, _review_state, deck_id)


def _type_field(card) -> str | None:
    return TYPE_FIELDS.get((card.note_type()["name"], card.ord))


def _compare_typed(col: Collection, deck_id: int, card_id: int, typed: str):
    state = _review_state(col, deck_id)
    if state.card is None or state.card.card_id != card_id:
        return state, None
    card = col.get_card(card_id)
    field = _type_field(card)
    if field is None:
        return state, None
    expected = plain_text(card.note()[field])
    return state, col.compare_answer(expected, typed.strip())


async def compare_typed(user_id: str, deck_id: int, card_id: int, typed: str):
    """Typing mode: Anki's diff (HTML, input escaped by Anki) of the typed answer
    against the expected field. Returns (state, diff or None if not applicable)."""
    return await _run(user_id, _compare_typed, deck_id, card_id, typed)


def _answer(col: Collection, deck_id: int, card_id: int, rating: int, taken_secs: float):
    if rating not in RATINGS:
        raise ValueError(f"invalid rating {rating}")
    _name, qc, _counts = _queue(col, deck_id)
    answered = False
    # Only answer if the card is still the current one (protects against double taps).
    if qc is not None and qc.card.id == card_id:
        card = col.get_card(card_id)
        card.timer_started = time.time() - max(0.0, taken_secs)
        col.sched.answer_card(
            col.sched.build_answer(card=card, states=qc.states, rating=RATINGS[rating])
        )
        answered = True
        step = col.undo_status().last_step
    state = _review_state(col, deck_id)
    if answered:
        col.merge_undo_entries(step)  # a deck switch (ALL_DECKS) belongs to the answer
        state.can_undo = _can_undo_review(col)
    return state, answered


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
    for name in (BIBLE_VERSE, BASIC):
        m = col.models.by_name(name)
        if m is not None:
            result.append(NotetypeInfo(m["id"], m["name"], [f["name"] for f in m["flds"]]))
    return result


async def notetypes_for_adding(user_id: str) -> list[NotetypeInfo]:
    """The note types offered in the add form (Bible Verse, Basic)."""
    return await _run(user_id, _notetypes_for_adding)


def _get_note_obj(col: Collection, note_id: int):
    try:
        return col.get_note(note_id)
    except NotFoundError as exc:
        raise NotFound(N_("Note not found.")) from exc


def _set_fields(note, fields: dict[str, str]) -> None:
    for name in note.keys():
        if name in fields:
            note[name] = fields[name]
    if note.fields_check() == NoteFieldsCheckResult.EMPTY:
        raise EmptyNote(N_("The first field must not be empty."))


def _add_note(col: Collection, notetype: str, deck_id: int, fields: dict[str, str], tags):
    m = col.models.by_name(notetype)
    if m is None or notetype not in (BASIC, BIBLE_VERSE):
        raise NotFound(N_("Note type not found."))
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


def _add_bible_text(col: Collection, deck_id: int, title: str, recite: int, text: str,
                    table: bool, tags, lang: str) -> tuple[int, int]:
    """Mirror of add_notes() from bible-memorizer's gen_notes.py, using its helpers."""
    _require_deck(col, deck_id)
    model = col.models.get(SEED_IDS[BIBLE_MEMORIZER]) or col.models.by_name(BIBLE_MEMORIZER)
    data = bm_gen.process_lines(text)
    if not data:
        raise EmptyNote(N_("Please enter a Bible text."))
    sorted_data = bm_gen.sort_by_verse_number(data)
    ms = int(time.time() * 1000)
    cards = 0
    for idx, d in enumerate(data):
        html = ""
        if len(sorted_data) > 4 and table:
            html = bm_gen.create_html_table(sorted_data, d["verse_number"], d["verse_part"])
        front, back = bm_gen.get_verses(data, d["verse_number"], d["verse_part"], recite)
        if front == "[Beginning]":  # the only change to the add-on's output: localized
            front = translate(N_("[Beginning]"), lang)
        note = col.new_note(model)
        note["answer"] = d["answer"]
        note["verse_number"] = str(d["verse_number"])
        note["verse_part"] = d["verse_part"]
        note["table"] = html
        note["title"] = title
        note["front"] = front
        note["back"] = back
        note["first_letters"] = d["first_letters"]
        note["last_letters"] = d["last_letters"]
        note["id"] = f"{ms}-{idx}"
        note.tags = list(tags)
        col.add_note(note, deck_id)
        cards += len(note.card_ids())
    return len(data), cards


async def add_bible_text(
    user_id: str, deck_id: int, title: str, recite: int, text: str, table: bool,
    tags: tuple[str, ...] = (),
) -> tuple[int, int]:
    """Create Bible Memorizer notes from a formatted, sanitized text (one line per note).
    Returns (notes, cards)."""
    return await _run(user_id, _add_bible_text, deck_id, title, recite, text, table, tags,
                      get_language())


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
        generated=note.note_type()["name"] == BIBLE_MEMORIZER,
        deck_ids=sorted({int(c.did) for c in cards}),
        mod=int(note.mod),
    )


async def get_note(user_id: str, note_id: int) -> NoteData:
    return await _run(user_id, _note_data, note_id)


def _update_note(col: Collection, note_id, fields, deck_id, allow_group: bool,
                 expected_mod: int | None) -> None:
    note = _get_note_obj(col, note_id)
    if expected_mod is not None and int(note.mod) != expected_mod:
        raise EditConflict(N_("Someone else changed this note in the meantime. Check the "
                              "fields and save again to overwrite the other change."))
    if note.has_tag(GROUP_TAG) and not allow_group:
        raise ReadOnlyNote(N_("Cards from group decks cannot be edited."))
    if note.note_type()["name"] == BIBLE_MEMORIZER:
        raise ReadOnlyNote(N_("Bible text cards are generated from the text. "
                              "To change them, delete and import the passage again."))
    _require_deck(col, deck_id)
    _set_fields(note, fields)
    col.update_note(note)
    card_ids = [c.id for c in note.cards() if c.did != deck_id]
    if card_ids:
        col.set_deck(card_ids, deck_id)


async def update_note(
    user_id: str, note_id: int, fields: dict[str, str], deck_id: int, allow_group: bool = False,
    expected_mod: int | None = None,
) -> None:
    """Update fields (already sanitized) and move the cards to `deck_id`. With
    `expected_mod` (NoteData.mod when the form was loaded) a note changed in the meantime
    raises EditConflict instead of being overwritten."""
    await _run(user_id, _update_note, note_id, fields, deck_id, allow_group, expected_mod)


def _delete_note(col: Collection, note_id: int, allow_group: bool) -> None:
    note = _get_note_obj(col, note_id)
    if note.has_tag(GROUP_TAG) and not allow_group:
        raise ReadOnlyNote(N_("Cards from group decks cannot be deleted."))
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


def _due(col: Collection, card) -> tuple[str, date | None]:
    if card.queue == QUEUE_SUSPENDED:
        return "suspended", None
    if card.type == CARD_TYPE_NEW:
        return "new", None
    stats = col.card_stats_data(card.id)
    if stats.HasField("due_date"):
        return "scheduled", datetime.fromtimestamp(stats.due_date).date()
    return "scheduled", None


def _card_row(col: Collection, card) -> CardRow:
    note = card.note()
    status, due = _due(col, card)
    return CardRow(
        card_id=int(card.id),
        note_id=int(note.id),
        front=plain_text(note.fields[0])[:80],
        deck=col.decks.name(card.did),
        status=status,
        due=due,
        suspended=card.queue == QUEUE_SUSPENDED,
        template=card.template()["name"],
        siblings=len(note.card_ids()),
    )


def _search(col: Collection, query: str, limit: int) -> tuple[list[CardRow], int]:
    try:
        card_ids = col.find_cards(query.strip() or "deck:*", order=True)
    except SearchError as exc:
        raise InvalidSearch(str(exc)) from exc
    return [_card_row(col, col.get_card(cid)) for cid in card_ids[:limit]], len(card_ids)


def _search_notes(col: Collection, query: str, limit: int) -> tuple[list[NoteRow], int]:
    try:
        note_ids = col.find_notes(query.strip() or "deck:*", order=True)
    except SearchError as exc:
        raise InvalidSearch(str(exc)) from exc
    rows = []
    for nid in note_ids[:limit]:
        note = col.get_note(nid)
        cards = note.cards()
        states = [_due(col, c) for c in cards]
        active = [(st, due) for st, due in states if st != "suspended"]
        dues = [due for st, due in active if due is not None]
        status = ("suspended" if not active
                  else "new" if all(st == "new" for st, _due_date in active) else "scheduled")
        decks = {c.did for c in cards}
        deck = col.decks.name(cards[0].did) if cards else ""
        rows.append(
            NoteRow(
                note_id=int(nid),
                front=plain_text(note.fields[0])[:80],
                deck=deck + (" …" if len(decks) > 1 else ""),
                cards=len(cards),
                suspended_cards=len(states) - len(active),
                status=status,
                due=min(dues) if dues else None,
            )
        )
    return rows, len(note_ids)


async def search_notes(user_id: str, query: str, limit: int = 200) -> tuple[list[NoteRow], int]:
    """Notes matching an Anki search (one row per note), with the total number of hits."""
    return await _run(user_id, _search_notes, query, limit)


def _set_card_suspended(col: Collection, card_id: int, suspended: bool) -> CardRow:
    try:
        card = col.get_card(card_id)
    except NotFoundError as exc:
        raise NotFound(N_("Card not found.")) from exc
    if suspended:
        col.sched.suspend_cards([card.id])
    else:
        col.sched.unsuspend_cards([card.id])
    card.load()
    return _card_row(col, card)


async def set_card_suspended(user_id: str, card_id: int, suspended: bool) -> CardRow:
    """Suspend or unsuspend a single card (e.g. only the reverse card); returns its row."""
    return await _run(user_id, _set_card_suspended, card_id, suspended)


def _card_deck_id(col: Collection, card_id: int) -> int:
    try:
        return int(col.get_card(card_id).did)
    except NotFoundError as exc:
        raise NotFound(N_("Card not found.")) from exc


async def card_deck_id(user_id: str, card_id: int) -> int:
    """The deck a card is in (permission checks before changing it)."""
    return await _run(user_id, _card_deck_id, card_id)


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
        bury_new=bool(conf["new"].get("bury", False)),
        bury_reviews=bool(conf["rev"].get("bury", False)),
    )


async def get_deck_options(user_id: str, deck_id: int) -> DeckOptions:
    return await _run(user_id, _get_deck_options, deck_id)


def _own_config(col: Collection, deck_id: int) -> dict:
    """The deck's options preset; split off first if other decks share it (options are
    per deck in our UI)."""
    deck = _require_deck(col, deck_id)
    conf = col.decks.config_dict_for_deck_id(deck_id)
    sharing = [d for d in col.decks.all() if not d["dyn"] and d.get("conf") == conf["id"]]
    if len(sharing) > 1:
        new_id = col.decks.add_config_returning_id(deck["name"], clone_from=conf)
        col.decks.set_config_id_for_deck_dict(deck, new_id)
        conf = col.decks.get_config(new_id)
    return conf


def _set_deck_options(col: Collection, deck_id: int, opts: DeckOptions) -> None:
    conf = _own_config(col, deck_id)
    conf["new"]["perDay"] = opts.new_per_day
    conf["rev"]["perDay"] = opts.reviews_per_day
    conf["desiredRetention"] = opts.desired_retention
    conf["new"]["bury"] = opts.bury_new
    conf["rev"]["bury"] = opts.bury_reviews
    col.decks.update_config(conf)


async def set_deck_options(user_id: str, deck_id: int, opts: DeckOptions) -> None:
    """Save options for this deck only (splits off a preset if it was shared)."""
    await _run(user_id, _set_deck_options, deck_id, opts)


def _set_deck_options_by_name(col: Collection, name: str, opts: DeckOptions) -> bool:
    deck = col.decks.by_name(name)
    if deck is None or deck["dyn"]:
        return False
    _set_deck_options(col, deck["id"], opts)
    return True


async def set_deck_options_by_name(user_id: str, name: str, opts: DeckOptions) -> bool:
    """Apply a group deck's options in a member collection (the .apkg import does not
    update existing presets). False if the member has no such deck."""
    return await _run(user_id, _set_deck_options_by_name, name, opts)


@dataclass
class OptimizeResult:
    reviews: int  # review items FSRS could learn from
    changed: bool


MIN_OPTIMIZE_REVIEWS = 400  # below this the UI only offers the button with a hint


def _apply_fsrs_params(col: Collection, deck_id: int, params: list[float]) -> None:
    """Store FSRS parameters for the deck's preset the way Anki's deck options do
    (update_deck_configs recomputes the memory states of affected cards)."""
    conf_id = _own_config(col, deck_id)["id"]
    data = col.decks.get_deck_configs_for_update(deck_id)
    entry = next(c for c in data.all_config if c.config.id == conf_id)
    cfg = entry.config
    del cfg.config.fsrs_params_6[:]
    cfg.config.fsrs_params_6.extend(params)
    col.decks.update_deck_configs(UpdateDeckConfigs(
        target_deck_id=deck_id,
        configs=[cfg],
        removed_config_ids=[],
        mode=UpdateDeckConfigsMode.UPDATE_DECK_CONFIGS_MODE_NORMAL,
        card_state_customizer=data.card_state_customizer,
        limits=data.current_deck.limits,
        new_cards_ignore_review_limit=data.new_cards_ignore_review_limit,
        fsrs=True,
        apply_all_parent_limits=data.apply_all_parent_limits,
        fsrs_reschedule=False,
        fsrs_health_check=False,
    ))


def _optimize_fsrs(col: Collection, deck_id: int) -> OptimizeResult:
    deck = _require_deck(col, deck_id)
    conf = _own_config(col, deck_id)
    current = list(conf.get("fsrsParams6") or [])
    resp = col._backend.compute_fsrs_params(
        search=f'"deck:{_escape_search(deck["name"])}"',
        current_params=current,
        ignore_revlogs_before_ms=0,
        num_of_relearning_steps=len(conf["lapse"]["delays"]),
        health_check=False,
    )
    params = [round(p, 4) for p in resp.params]
    if not params or params == [round(p, 4) for p in current]:
        return OptimizeResult(reviews=resp.fsrs_items, changed=False)
    _apply_fsrs_params(col, deck_id, list(resp.params))
    return OptimizeResult(reviews=resp.fsrs_items, changed=True)


async def optimize_fsrs(user_id: str, deck_id: int) -> OptimizeResult:
    """Fit FSRS parameters to this deck's review history (Anki's "Optimize")."""
    return await _run(user_id, _optimize_fsrs, deck_id)


def _review_count(col: Collection, deck_id: int) -> int:
    deck = _require_deck(col, deck_id)
    cids = col.find_cards(f'"deck:{_escape_search(deck["name"])}"')
    if not cids:
        return 0
    return col.db.scalar(
        f"select count() from revlog where ease > 0 and cid in ({','.join(map(str, cids))})"
    )


async def review_count(user_id: str, deck_id: int) -> int:
    """Number of reviews in the deck (incl. subdecks)."""
    return await _run(user_id, _review_count, deck_id)


# --------------------------------------------------------------------------- export / import


def _export_collection(col: Collection, out_path: str) -> None:
    try:
        col.export_collection_package(out_path, include_media=True, legacy=False)
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
            with_media=True,
            legacy=False,
        ),
        limit=DeckIdLimit(deck_id=deck_id),
    )


async def export_deck(
    user_id: str, deck_id: int, out_path: Path, with_scheduling: bool = True
) -> int:
    """One deck (incl. subdecks) as .apkg. Returns the number of exported notes."""
    return await _run(user_id, _export_deck, deck_id, str(out_path), with_scheduling)


def _import_package(col: Collection, path: str, with_scheduling: bool,
                    clean_field: Callable[[str], str] | None) -> int:
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
    imported = [*log_.log.new, *log_.log.updated]
    if clean_field is not None:
        _clean_notes(col, [entry.id.nid for entry in imported], clean_field)
    return len(imported)


def _clean_notes(col: Collection, note_ids: list[int], clean_field: Callable[[str], str]) -> None:
    changed = []
    for nid in note_ids:
        note = col.get_note(nid)
        cleaned = [clean_field(value) for value in note.fields]
        if cleaned != note.fields:
            note.fields = cleaned
            changed.append(note)
    if changed:
        col.update_notes(changed)


async def import_package(user_id: str, path: Path, with_scheduling: bool = False,
                         clean_field: Callable[[str], str] | None = None) -> int:
    """Import an .apkg; existing notes are updated by GUID. Returns new+updated count.

    `clean_field` (e.g. sanitize.field_from_import) is applied to every field of the
    imported notes – for packages from untrusted sources."""
    return await _run(user_id, _import_package, str(path), with_scheduling, clean_field)


def _tag_deck_notes(col: Collection, deck_id: int, tag: str) -> int:
    deck = _require_deck(col, deck_id)
    note_ids = col.find_notes(f'"deck:{_escape_search(deck["name"])}" -tag:{tag}')
    if note_ids:
        col.tags.bulk_add(note_ids, tag)
    return len(note_ids)


def deck_search(deck_name: str) -> str:
    """Anki search for all cards of a deck (and its subdecks), e.g. for filter chips."""
    return f'"deck:{_escape_search(deck_name)}"'


def _escape_search(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*").replace("_", "\\_")


async def tag_deck_notes(user_id: str, deck_id: int, tag: str = GROUP_TAG) -> int:
    """Add `tag` to all notes of the deck (incl. subdecks) that lack it. Returns count."""
    return await _run(user_id, _tag_deck_notes, deck_id, tag)


async def delete_collection(user_id: str) -> None:
    """Close and delete the user's collection folder (irreversible)."""
    path = collection_path(user_id)
    async with _lock(user_id):
        with _cache_lock:
            entry = _open.pop(user_id, None)

        def remove() -> None:
            if entry is not None:
                entry.col.close()
            shutil.rmtree(path.parent, ignore_errors=True)

        await run_in_threadpool(remove)


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


@dataclass
class LearnerStats:
    heatmap: list[tuple[date, int]]  # reviews per day, oldest first, ending today
    forecast: list[tuple[date, int]]  # due cards per day, starting today (incl. overdue)
    new: int
    learning: int
    young: int
    mature: int
    suspended: int
    today_answered: int
    today_correct: int
    retention_month: float | None
    streak_days: int
    total_reviews: int


def _learner_stats(col: Collection, deck_id: int | None, history_days: int,
                   forecast_days: int) -> LearnerStats:
    search = "deck:*"
    if deck_id is not None:
        search = f'"deck:{_escape_search(_require_deck(col, deck_id)["name"])}"'
    g = col._backend.graphs(search=search, days=history_days)
    today = (datetime.fromtimestamp(col.sched.day_cutoff) - timedelta(days=1)).date()

    def total(r) -> int:
        return r.learn + r.relearn + r.young + r.mature + r.filtered

    per_day = {int(k): total(v) for k, v in g.reviews.count.items()}
    heatmap = [(today - timedelta(days=i), per_day.get(-i, 0)) for i in reversed(range(history_days))]
    due = {int(k): v for k, v in g.future_due.future_due.items()}
    overdue = sum(v for k, v in due.items() if k < 0)
    forecast = [(today + timedelta(days=i), due.get(i, 0) + (overdue if i == 0 else 0))
                for i in range(forecast_days)]
    counts = g.card_counts.excluding_inactive
    month = g.true_retention.month
    passed = month.young_passed + month.mature_passed
    failed = month.young_failed + month.mature_failed
    streak = 0
    day = 0 if per_day.get(0) else -1
    while per_day.get(day):
        streak += 1
        day -= 1
    return LearnerStats(
        heatmap=heatmap, forecast=forecast,
        new=counts.newCards, learning=counts.learn + counts.relearn, young=counts.young,
        mature=counts.mature, suspended=g.card_counts.including_inactive.suspended,
        today_answered=g.today.answer_count, today_correct=g.today.correct_count,
        retention_month=passed / (passed + failed) if passed + failed else None,
        streak_days=streak, total_reviews=sum(per_day.values()),
    )


async def learner_stats(user_id: str, deck_id: int | None = None, history_days: int = 365,
                        forecast_days: int = 30) -> LearnerStats:
    """Heatmap, forecast and card counts from Anki's own statistics (graphs)."""
    return await _run(user_id, _learner_stats, deck_id, history_days, forecast_days)


# --------------------------------------------------------------------------- media

MEDIA_TYPES = {"png": b"\x89PNG", "jpg": b"\xff\xd8\xff", "gif": b"GIF8"}
MAX_MEDIA_BYTES = 5 * 1024 * 1024


def media_type(data: bytes) -> str | None:
    """File extension for supported image data (by magic bytes), else None."""
    for ext, magic in MEDIA_TYPES.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _add_media(col: Collection, data: bytes, ext: str) -> str:
    name = f"{hashlib.sha1(data).hexdigest()}.{ext}"
    return col.media.write_data(name, data)


async def add_media(user_id: str, data: bytes) -> str:
    """Store an image in the collection's media folder. Returns the file name to use
    in <img src>. Raises AnkiServiceError for unsupported or too large files."""
    if len(data) > MAX_MEDIA_BYTES:
        raise AnkiServiceError(N_("The image is too large (max. 5 MB)."))
    ext = media_type(data)
    if ext is None:
        raise AnkiServiceError(N_("Only PNG, JPEG, GIF or WebP images are supported."))
    return await _run(user_id, _add_media, data, ext)


def media_file(user_id: str, filename: str) -> Path | None:
    """Path of a media file for serving (read-only, outside the collection DB)."""
    if not MEDIA_NAME.match(filename):
        return None
    path = collection_path(user_id).parent / "collection.media" / filename
    return path if path.is_file() else None


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
            media = path.parent / "collection.media"
            if media.is_dir():
                shutil.copytree(media, dest_dir / media.name, dirs_exist_ok=True)
            return True

        return await run_in_threadpool(copy)


def collection_ids() -> list[str]:
    """Ids of all collections on disk."""
    base = collections_dir()
    if not base.exists():
        return []
    return sorted(p.parent.name for p in base.glob("*/collection.anki2"))

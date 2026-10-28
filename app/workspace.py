"""Shared editing of group decks in the admin collection (SPEC §7.3).

Admins see and edit every deck of the admin collection. Group leaders see a deck when it
(or a parent deck) is distributed to one of their groups, and may edit it only when every
group receiving it is one of theirs. Changes to notes are logged in `group_deck_edits`.
"""

from dataclasses import dataclass, field
from datetime import datetime

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app import anki_service, distribution
from app.auth import Forbidden, current_collection, require_user, visible_group_ids
from app.db import get_db
from app.i18n import N_
from app.models import Group, GroupDeck, GroupDeckEdit, User
from app.sanitize import plain_text


class LeaderWorkspaceBlocked(Exception):
    """A page that group leaders cannot use while editing group decks (e.g. studying)."""


class GroupDeckError(anki_service.AnkiServiceError):
    pass


def deck_within(name: str, parent: str) -> bool:
    """Deck `name` is `parent` or one of its subdecks (deck names are case-insensitive)."""
    name, parent = name.casefold(), parent.casefold()
    return name == parent or name.startswith(parent + "::")


def normalize_deck_name(name: str) -> str:
    """Same normalization as anki_service.create_deck."""
    return "::".join(part.strip() for part in name.split("::") if part.strip())


@dataclass
class DeckAccess:
    """Which decks of the current collection the user may see and edit. `visible` is None
    when nothing is restricted (own collection, or an admin in the admin collection)."""

    visible: dict[int, str] | None = None  # id -> full name
    editable: set[int] | None = None
    groups: dict[int, str] = field(default_factory=dict)  # the leader's groups (id -> name)
    receiving: dict[int, set[int]] = field(default_factory=dict)  # deck id -> group ids

    @property
    def restricted(self) -> bool:
        return self.visible is not None

    def can_see(self, deck_id: int) -> bool:
        return self.visible is None or deck_id in self.visible

    def can_edit(self, deck_id: int) -> bool:
        return self.editable is None or deck_id in self.editable

    def require_see(self, deck_ids) -> None:
        """At least one of the decks (e.g. of a note's cards) must be visible."""
        if self.restricted and not any(self.can_see(d) for d in deck_ids):
            raise anki_service.NotFound(N_("Note not found."))

    def can_edit_all(self, deck_ids) -> bool:
        return not self.restricted or bool(deck_ids) and all(self.can_edit(d) for d in deck_ids)

    def require_edit(self, deck_ids) -> None:
        if not self.can_edit_all(deck_ids):
            raise Forbidden()

    def filter(self, decks: list[tuple[int, str]], *, edit: bool = False) -> list[tuple[int, str]]:
        check = self.can_edit if edit else self.can_see
        return [(did, name) for did, name in decks if check(did)]

    @property
    def roots(self) -> list[str]:
        """Visible decks without a visible parent (visibility includes all subdecks)."""
        names = sorted((self.visible or {}).values(), key=str.casefold)
        return [n for n in names if not any(deck_within(n, p) and n != p for p in names)]

    @property
    def editable_names(self) -> set[str] | None:
        if self.editable is None:
            return None
        return {name for did, name in (self.visible or {}).items() if did in self.editable}

    def search(self, query: str) -> str | None:
        """Restrict an Anki search to the visible decks; None = nothing visible."""
        if self.visible is None:
            return query
        roots = self.roots
        if not roots:
            return None
        decks = " OR ".join(anki_service.deck_search(name) for name in roots)
        return f"({query}) ({decks})" if query.strip() else f"({decks})"

    def deck_list(self, decks: list[anki_service.DeckInfo]) -> list[anki_service.DeckInfo]:
        """The deck tree reduced to visible decks; roots are shown with their full name."""
        if self.visible is None:
            return decks
        visible = [d for d in decks if d.id in self.visible]
        for d in visible:
            parents = [p for p in visible if p is not d and deck_within(d.name, p.name)]
            if not parents:
                d.short_name = d.name
            d.depth = len(parents)
        return visible


def receiving_groups(deck_names: dict[int, str], group_decks: list[tuple[int, int]],
                     name: str) -> set[int]:
    """Groups that receive deck `name`: group decks on the deck itself or a parent."""
    return {gid for did, gid in group_decks
            if did in deck_names and deck_within(name, deck_names[did])}


def _group_decks(db: DbSession) -> list[tuple[int, int]]:
    return [(r[0], r[1]) for r in db.execute(select(GroupDeck.admin_deck_id, GroupDeck.group_id))]


async def access_for(db: DbSession, user: User, collection_id: str) -> DeckAccess:
    if collection_id != anki_service.ADMIN_COLLECTION or user.is_admin:
        return DeckAccess()
    mine = visible_group_ids(db, user) or set()
    names = dict(await anki_service.deck_names(anki_service.ADMIN_COLLECTION))
    group_decks = _group_decks(db)
    visible, editable, receiving = {}, set(), {}
    for did, name in names.items():
        groups = receiving_groups(names, group_decks, name)
        if groups & mine:
            visible[did] = name
            receiving[did] = groups
            if groups <= mine:
                editable.add(did)
    group_names = dict(db.execute(select(Group.id, Group.name).where(Group.id.in_(mine))).all())
    return DeckAccess(visible, editable, group_names, receiving)


async def deck_access(request: Request, user: User = Depends(require_user),
                      col: str = Depends(current_collection),
                      db: DbSession = Depends(get_db)) -> DeckAccess:
    """Dependency: deck permissions for the collection the learner UI works on."""
    return await access_for(db, user, col)


def no_leader_workspace(request: Request, col: str = Depends(current_collection)) -> None:
    """Dependency for pages group leaders cannot use in the admin collection."""
    if getattr(request.state, "leader_workspace", False):
        raise LeaderWorkspaceBlocked()


async def create_deck(db: DbSession, access: DeckAccess, name: str,
                      group_id: int | None) -> int:
    """A group leader creates a deck in the admin collection: as a subdeck of a deck they
    may edit, or as a new deck assigned to one of their groups (not distributed yet)."""
    name = normalize_deck_name(name)
    if not name:
        raise GroupDeckError(N_("Please enter a deck name."))
    names = dict(await anki_service.deck_names(anki_service.ADMIN_COLLECTION))
    existing = next((did for did, n in names.items() if n.casefold() == name.casefold()), None)
    if existing is not None:
        if access.can_edit(existing):
            return existing
        raise GroupDeckError(N_("A deck with this name already exists."))
    groups = receiving_groups(names, _group_decks(db), name)
    if groups:
        if not groups <= set(access.groups):
            raise GroupDeckError(N_("This deck would also be distributed to groups you don't "
                                    "lead. Please choose another name."))
        return await anki_service.create_deck(anki_service.ADMIN_COLLECTION, name)
    if group_id not in access.groups:
        raise GroupDeckError(N_("Please choose the group the deck is for."))
    did = await anki_service.create_deck(anki_service.ADMIN_COLLECTION, name)
    names = dict(await anki_service.deck_names(anki_service.ADMIN_COLLECTION))
    db.add(GroupDeck(group_id=group_id, admin_deck_id=did, deck_name=names.get(did, name)))
    db.commit()
    return did


async def rename_deck(db: DbSession, access: DeckAccess, deck_id: int,
                      name: str) -> distribution.Job | None:
    """Rename a deck of the admin collection (admins: any, leaders: decks they may edit) and
    the distributed copies in all member collections (background job, None if the deck is
    not distributed). Only the last part of the name changes, so the receiving groups (and
    a leader's permissions) stay the same."""
    if not access.can_edit(deck_id):
        raise Forbidden()
    names = dict(await anki_service.deck_names(anki_service.ADMIN_COLLECTION))
    if deck_id not in names:
        raise anki_service.NotFound(N_("Deck not found."))
    groups = receiving_groups(names, _group_decks(db), names[deck_id])
    old, new = await anki_service.rename_deck(anki_service.ADMIN_COLLECTION, deck_id, name,
                                              allow_group=True, keep_parent=True)
    if old == new:
        return None
    for gd in db.scalars(select(GroupDeck)):
        current = names.get(gd.admin_deck_id, gd.deck_name)
        if deck_within(current, old):
            gd.deck_name = new + current[len(old):]
    db.commit()
    return distribution.rename_in_members(groups, old, new) if groups else None


# --------------------------------------------------------------------------- edit log


def log_edit(db: DbSession, user: User, collection_id: str, note_id: int, deck_id: int,
             action: str, front: str = "") -> None:
    """Record a change to a note of the admin collection (no-op for own collections)."""
    if collection_id != anki_service.ADMIN_COLLECTION:
        return
    db.add(GroupDeckEdit(user_id=user.id, note_id=note_id, deck_id=deck_id, action=action,
                         summary=plain_text(front)[:100]))
    db.commit()


@dataclass
class EditInfo:
    user_name: str | None  # None: the user was deleted
    note_id: int
    deck_id: int
    deck_name: str
    action: str
    summary: str
    created_at: datetime


def _edit_rows(db: DbSession, query) -> list[tuple[GroupDeckEdit, str | None]]:
    return list(db.execute(query.outerjoin(User, User.id == GroupDeckEdit.user_id)
                           .add_columns(User.display_name)).all())


def last_note_edit(db: DbSession, note_id: int) -> EditInfo | None:
    rows = _edit_rows(db, select(GroupDeckEdit).where(GroupDeckEdit.note_id == note_id)
                      .order_by(GroupDeckEdit.created_at.desc(), GroupDeckEdit.id.desc()).limit(1))
    if not rows:
        return None
    edit, user_name = rows[0]
    return EditInfo(user_name, edit.note_id, edit.deck_id, "", edit.action, edit.summary,
                    edit.created_at)


def edits_since(db: DbSession, since: datetime | None, limit: int = 2000) -> list[EditInfo]:
    """Newest first. Deck names are filled in by the caller."""
    query = select(GroupDeckEdit)
    if since is not None:
        query = query.where(GroupDeckEdit.created_at >= since)
    query = query.order_by(GroupDeckEdit.created_at.desc(), GroupDeckEdit.id.desc()).limit(limit)
    return [EditInfo(name, e.note_id, e.deck_id, "", e.action, e.summary, e.created_at)
            for e, name in _edit_rows(db, query)]

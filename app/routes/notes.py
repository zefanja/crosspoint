"""Adding and editing notes. All field input goes through sanitize.py."""

from fastapi import APIRouter, Depends, Form, Request
from starlette.datastructures import UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from app import anki_service, workspace
from app.auth import current_collection, require_user, verify_csrf
from app.db import get_db
from app.models import User
from app.sanitize import field_from_input, field_to_input, image_tag, plain_text
from app.templating import render
from app.i18n import N_, _
from app.workspace import DeckAccess, deck_access

router = APIRouter()


def _group_tags(col: str) -> tuple[str, ...]:
    """Notes in the admin collection are group-deck material."""
    return (anki_service.GROUP_TAG,) if col == anki_service.ADMIN_COLLECTION else ()


def _fields_from_form(form, names: list[str]) -> dict[str, str]:
    return {name: field_from_input(str(form.get(f"field_{i}", ""))) for i, name in enumerate(names)}


async def _attach_image(col: str, form, names: list[str], fields: dict[str, str]) -> None:
    """Optional image upload: stored in the collection's media folder and appended to the
    front (first) or back (second) field."""
    upload = form.get("image")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return
    data = await upload.read(anki_service.MAX_MEDIA_BYTES + 1)
    filename = await anki_service.add_media(col, data)
    target = names[0] if form.get("image_side") == "front" or len(names) < 2 else names[1]
    sep = "<br>" if fields.get(target) else ""
    fields[target] = f"{fields.get(target, '')}{sep}{image_tag(filename)}"


async def _add_form(request, col: str, access: DeckAccess, notetype: str | None,
                    deck_id: int | None, *, values=None, message=None, error=None,
                    status_code=200):
    notetypes = await anki_service.notetypes_for_adding(col)
    current = next((n for n in notetypes if n.name == notetype), notetypes[0])
    decks = access.filter(await anki_service.deck_names(col), edit=True)
    if deck_id not in dict(decks):
        deck_id = await anki_service.current_deck_id(col)
        if deck_id not in dict(decks):
            deck_id = decks[0][0] if decks else None
    if not decks and not error:
        error = _("There is no deck you can add cards to yet. Create one on the start page.")
    return render(
        request, "note_add.html",
        {"notetypes": notetypes, "notetype": current, "decks": decks, "deck_id": deck_id,
         "values": values or {}, "message": message, "error": error},
        status_code=status_code,
    )


@router.get("/notes/new")
async def add_page(request: Request, notetype: str | None = None, deck_id: int | None = None,
                   user: User = Depends(require_user),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access)):
    return await _add_form(request, col, access, notetype, deck_id)


@router.post("/notes/new")
async def add_submit(request: Request, notetype: str = Form(...), deck_id: int = Form(...),
                     user: User = Depends(verify_csrf),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access),
    db: DbSession = Depends(get_db)):
    access.require_edit([deck_id])
    form = await request.form()
    notetypes = {n.name: n for n in await anki_service.notetypes_for_adding(col)}
    nt = notetypes.get(notetype)
    if nt is None:
        raise anki_service.NotFound(N_("Note type not found."))
    fields = _fields_from_form(form, nt.fields)
    try:
        await _attach_image(col, form, nt.fields, fields)
        note_id = await anki_service.add_note(col, notetype, deck_id, fields, _group_tags(col))
    except anki_service.AnkiServiceError as exc:
        values = {i: str(form.get(f"field_{i}", "")) for i in range(len(nt.fields))}
        return await _add_form(request, col, access, notetype, deck_id, values=values,
                               error=_(str(exc)), status_code=400)
    workspace.log_edit(db, user, col, note_id, deck_id, "add", fields[nt.fields[0]])
    # Keep the form open for adding the next card (same note type and deck).
    first = plain_text(fields[nt.fields[0]]) or "🖼"
    keep = {}
    if notetype == anki_service.BIBLE_VERSE:  # keep translation and reverse for series entry
        keep = {i: str(form.get(f"field_{i}", "")) for i in range(2, len(nt.fields))}
    return await _add_form(request, col, access, notetype, deck_id, values=keep,
                           message=_("Added: %(front)s", front=first))


async def _get_visible_note(col: str, access: DeckAccess, note_id: int) -> anki_service.NoteData:
    note = await anki_service.get_note(col, note_id)
    access.require_see(note.deck_ids)
    if col == anki_service.ADMIN_COLLECTION:
        # group-deck material is editable here – for leaders only in their groups' decks
        note.readonly = not access.can_edit_all(note.deck_ids)
    return note


async def _edit_context(col: str, access: DeckAccess, db: DbSession, note) -> dict:
    decks = access.filter(await anki_service.deck_names(col), edit=not note.readonly)
    return {"note": note, "decks": decks,
            "shared_readonly": access.restricted and note.readonly,
            "last_edit": (workspace.last_note_edit(db, note.id)
                          if col == anki_service.ADMIN_COLLECTION else None)}


@router.get("/notes/{note_id}")
async def edit_page(request: Request, note_id: int, user: User = Depends(require_user),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access),
    db: DbSession = Depends(get_db), saved: int = 0):
    note = await _get_visible_note(col, access, note_id)
    return render(
        request, "note_edit.html",
        {**await _edit_context(col, access, db, note), "error": None, "saved": bool(saved),
         "values": {i: field_to_input(v) for i, (_name, v) in enumerate(note.fields)}},
    )


@router.post("/notes/{note_id}")
async def edit_submit(request: Request, note_id: int, deck_id: int = Form(...),
                      mod: int | None = Form(None), user: User = Depends(verify_csrf),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access),
    db: DbSession = Depends(get_db)):
    form = await request.form()
    note = await _get_visible_note(col, access, note_id)
    access.require_edit([*note.deck_ids, deck_id])
    names = [name for name, _ in note.fields]
    fields = _fields_from_form(form, names)
    try:
        if not note.generated:
            await _attach_image(col, form, names, fields)
        await anki_service.update_note(col, note_id, fields, deck_id,
                                       allow_group=col == anki_service.ADMIN_COLLECTION,
                                       expected_mod=mod)
    except anki_service.AnkiServiceError as exc:
        values = {i: str(form.get(f"field_{i}", "")) for i in range(len(note.fields))}
        return render(request, "note_edit.html",
                      {**await _edit_context(col, access, db, note), "error": _(str(exc)),
                       "saved": False, "values": values}, status_code=400)
    workspace.log_edit(db, user, col, note_id, deck_id, "edit", fields.get(names[0], ""))
    return RedirectResponse(f"/notes/{note_id}?saved=1", status_code=303)


@router.post("/notes/{note_id}/delete")
async def delete(request: Request, note_id: int, user: User = Depends(verify_csrf),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access),
    db: DbSession = Depends(get_db)):
    note = await _get_visible_note(col, access, note_id)
    access.require_edit(note.deck_ids)
    try:
        await anki_service.delete_note(col, note_id,
                                       allow_group=col == anki_service.ADMIN_COLLECTION)
    except anki_service.ReadOnlyNote as exc:
        return render(request, "error.html", {"message": _(str(exc))}, 403)
    workspace.log_edit(db, user, col, note_id, note.deck_id, "delete",
                       note.fields[0][1] if note.fields else "")
    return RedirectResponse("/browse", status_code=303)


@router.post("/notes/{note_id}/suspend")
async def suspend(request: Request, note_id: int, suspended: int = Form(...),
                  user: User = Depends(verify_csrf),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access)):
    note = await _get_visible_note(col, access, note_id)
    access.require_edit(note.deck_ids)
    await anki_service.set_suspended(col, note_id, bool(suspended))
    return RedirectResponse(f"/notes/{note_id}", status_code=303)

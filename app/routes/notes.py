"""Adding and editing notes. All field input goes through sanitize.py."""

from fastapi import APIRouter, Depends, Form, Request
from starlette.datastructures import UploadFile
from fastapi.responses import RedirectResponse

from app import anki_service
from app.auth import current_collection, require_user, verify_csrf
from app.models import User
from app.sanitize import field_from_input, field_to_input, image_tag, plain_text
from app.templating import render
from app.i18n import N_, _

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


async def _add_form(request, col: str, notetype: str | None, deck_id: int | None, *,
                    values=None, message=None, error=None, status_code=200):
    notetypes = await anki_service.notetypes_for_adding(col)
    current = next((n for n in notetypes if n.name == notetype), notetypes[0])
    decks = await anki_service.deck_names(col)
    if deck_id not in dict(decks):
        deck_id = await anki_service.current_deck_id(col)
    return render(
        request, "note_add.html",
        {"notetypes": notetypes, "notetype": current, "decks": decks, "deck_id": deck_id,
         "values": values or {}, "message": message, "error": error},
        status_code=status_code,
    )


@router.get("/notes/new")
async def add_page(request: Request, notetype: str | None = None, deck_id: int | None = None,
                   user: User = Depends(require_user),
    col: str = Depends(current_collection)):
    return await _add_form(request, col, notetype, deck_id)


@router.post("/notes/new")
async def add_submit(request: Request, notetype: str = Form(...), deck_id: int = Form(...),
                     user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)):
    form = await request.form()
    notetypes = {n.name: n for n in await anki_service.notetypes_for_adding(col)}
    nt = notetypes.get(notetype)
    if nt is None:
        raise anki_service.NotFound(N_("Note type not found."))
    fields = _fields_from_form(form, nt.fields)
    try:
        await _attach_image(col, form, nt.fields, fields)
        await anki_service.add_note(col, notetype, deck_id, fields, _group_tags(col))
    except anki_service.AnkiServiceError as exc:
        values = {i: str(form.get(f"field_{i}", "")) for i in range(len(nt.fields))}
        return await _add_form(request, col, notetype, deck_id, values=values,
                               error=_(str(exc)), status_code=400)
    # Keep the form open for adding the next card (same note type and deck).
    first = plain_text(fields[nt.fields[0]]) or "🖼"
    keep = {}
    if notetype == anki_service.BIBLE_VERSE:  # keep translation and reverse for series entry
        keep = {i: str(form.get(f"field_{i}", "")) for i in range(2, len(nt.fields))}
    return await _add_form(request, col, notetype, deck_id, values=keep,
                           message=_("Added: %(front)s", front=first))


@router.get("/notes/{note_id}")
async def edit_page(request: Request, note_id: int, user: User = Depends(require_user),
    col: str = Depends(current_collection),
                    saved: int = 0):
    note = await anki_service.get_note(col, note_id)
    if col == anki_service.ADMIN_COLLECTION:
        note.readonly = False
    decks = await anki_service.deck_names(col)
    return render(
        request, "note_edit.html",
        {"note": note, "decks": decks, "error": None, "saved": bool(saved),
         "values": {i: field_to_input(v) for i, (_name, v) in enumerate(note.fields)}},
    )


@router.post("/notes/{note_id}")
async def edit_submit(request: Request, note_id: int, deck_id: int = Form(...),
                      user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)):
    form = await request.form()
    note = await anki_service.get_note(col, note_id)
    names = [name for name, _ in note.fields]
    fields = _fields_from_form(form, names)
    try:
        if not note.generated:
            await _attach_image(col, form, names, fields)
        await anki_service.update_note(col, note_id, fields, deck_id,
                                       allow_group=col == anki_service.ADMIN_COLLECTION)
    except anki_service.AnkiServiceError as exc:
        decks = await anki_service.deck_names(col)
        values = {i: str(form.get(f"field_{i}", "")) for i in range(len(note.fields))}
        return render(request, "note_edit.html",
                      {"note": note, "decks": decks, "error": _(str(exc)), "saved": False,
                       "values": values}, status_code=400)
    return RedirectResponse(f"/notes/{note_id}?saved=1", status_code=303)


@router.post("/notes/{note_id}/delete")
async def delete(request: Request, note_id: int, user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)):
    try:
        await anki_service.delete_note(col, note_id,
                                       allow_group=col == anki_service.ADMIN_COLLECTION)
    except anki_service.ReadOnlyNote as exc:
        return render(request, "error.html", {"message": _(str(exc))}, 403)
    return RedirectResponse("/browse", status_code=303)


@router.post("/notes/{note_id}/suspend")
async def suspend(request: Request, note_id: int, suspended: int = Form(...),
                  user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)):
    await anki_service.set_suspended(col, note_id, bool(suspended))
    return RedirectResponse(f"/notes/{note_id}", status_code=303)

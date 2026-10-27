"""Adding and editing notes. All field input goes through sanitize.py."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import anki_service
from app.auth import require_user, verify_csrf
from app.models import User
from app.sanitize import field_from_input, field_to_input
from app.templating import render

router = APIRouter()


def _fields_from_form(form, names: list[str]) -> dict[str, str]:
    return {name: field_from_input(str(form.get(f"field_{i}", ""))) for i, name in enumerate(names)}


async def _add_form(request, user, notetype: str | None, deck_id: int | None, *,
                    values=None, message=None, error=None, status_code=200):
    notetypes = await anki_service.notetypes_for_adding(user.id)
    current = next((n for n in notetypes if n.name == notetype), notetypes[0])
    decks = await anki_service.deck_names(user.id)
    if deck_id not in dict(decks):
        deck_id = next((d for d, name in decks if name == anki_service.DEFAULT_DECK), decks[0][0])
    return render(
        request, "note_add.html",
        {"notetypes": notetypes, "notetype": current, "decks": decks, "deck_id": deck_id,
         "values": values or {}, "message": message, "error": error},
        status_code=status_code,
    )


@router.get("/notes/new")
async def add_page(request: Request, notetype: str | None = None, deck_id: int | None = None,
                   user: User = Depends(require_user)):
    return await _add_form(request, user, notetype, deck_id)


@router.post("/notes/new")
async def add_submit(request: Request, notetype: str = Form(...), deck_id: int = Form(...),
                     user: User = Depends(verify_csrf)):
    form = await request.form()
    notetypes = {n.name: n for n in await anki_service.notetypes_for_adding(user.id)}
    nt = notetypes.get(notetype)
    if nt is None:
        raise anki_service.NotFound("Notiztyp nicht gefunden")
    fields = _fields_from_form(form, nt.fields)
    try:
        await anki_service.add_note(user.id, notetype, deck_id, fields)
    except anki_service.AnkiServiceError as exc:
        values = {i: str(form.get(f"field_{i}", "")) for i in range(len(nt.fields))}
        return await _add_form(request, user, notetype, deck_id, values=values,
                               error=str(exc), status_code=400)
    # Keep the form open for adding the next card (same note type and deck).
    first = field_to_input(fields[nt.fields[0]])
    keep = {}
    if notetype == anki_service.BIBELVERS:  # keep the translation for series entry
        keep = {2: str(form.get("field_2", ""))}
    return await _add_form(request, user, notetype, deck_id, values=keep,
                           message=f"Hinzugefügt: {first}")


@router.get("/notes/{note_id}")
async def edit_page(request: Request, note_id: int, user: User = Depends(require_user),
                    saved: int = 0):
    note = await anki_service.get_note(user.id, note_id)
    decks = await anki_service.deck_names(user.id)
    return render(
        request, "note_edit.html",
        {"note": note, "decks": decks, "error": None, "saved": bool(saved),
         "values": {i: field_to_input(v) for i, (_, v) in enumerate(note.fields)}},
    )


@router.post("/notes/{note_id}")
async def edit_submit(request: Request, note_id: int, deck_id: int = Form(...),
                      user: User = Depends(verify_csrf)):
    form = await request.form()
    note = await anki_service.get_note(user.id, note_id)
    fields = _fields_from_form(form, [name for name, _ in note.fields])
    try:
        await anki_service.update_note(user.id, note_id, fields, deck_id)
    except anki_service.AnkiServiceError as exc:
        decks = await anki_service.deck_names(user.id)
        values = {i: str(form.get(f"field_{i}", "")) for i in range(len(note.fields))}
        return render(request, "note_edit.html",
                      {"note": note, "decks": decks, "error": str(exc), "saved": False,
                       "values": values}, status_code=400)
    return RedirectResponse(f"/notes/{note_id}?saved=1", status_code=303)


@router.post("/notes/{note_id}/delete")
async def delete(request: Request, note_id: int, user: User = Depends(verify_csrf)):
    try:
        await anki_service.delete_note(user.id, note_id)
    except anki_service.ReadOnlyNote as exc:
        return render(request, "error.html", {"message": str(exc)}, 403)
    return RedirectResponse("/browse", status_code=303)


@router.post("/notes/{note_id}/suspend")
async def suspend(request: Request, note_id: int, suspended: int = Form(...),
                  user: User = Depends(verify_csrf)):
    await anki_service.set_suspended(user.id, note_id, bool(suspended))
    return RedirectResponse(f"/notes/{note_id}", status_code=303)

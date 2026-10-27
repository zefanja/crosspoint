"""Learner flow through the HTTP layer."""

import re

import pytest

from app import anki_service

HX = {"HX-Request": "true"}


@pytest.fixture
def learner(login):
    user, csrf = login()
    return user, csrf


def default_deck(client):
    page = client.get("/")
    return int(re.search(r'href="/review/(\d+)">Meine Karten<', page.text).group(1))


def add_verse(client, csrf, deck, ref="Joh 3,16", text="Denn also hat Gott die Welt geliebt"):
    return client.post("/notes/new", data={
        "csrf_token": csrf, "notetype": "Bible Verse", "deck_id": deck,
        "field_0": ref, "field_1": text, "field_2": "LUT17"})


def test_deck_list_and_empty_hint(client, learner):
    r = client.get("/")
    assert r.status_code == 200
    assert "Meine Karten" in r.text and "Noch keine Karten" in r.text
    assert "Karte hinzufügen" in r.text and "Export" in r.text


def test_create_deck(client, learner):
    _, csrf = learner
    r = client.post("/decks", data={"csrf_token": csrf, "name": "Psalmen"})
    assert r.status_code == 200 and "Psalmen" in r.text


def test_add_note_keeps_form_and_sanitizes(client, learner):
    user, csrf = learner
    deck = default_deck(client)
    r = add_verse(client, csrf, deck, text="Zeile 1\n<script>alert(1)</script><b>fett</b>")
    assert r.status_code == 200
    assert "Hinzugefügt: Joh 3,16" in r.text
    assert 'value="LUT17"' in r.text  # translation kept for series entry
    rows, _ = client.portal.call(anki_service.search_cards, user.id, "")
    note = client.portal.call(anki_service.get_note, user.id, rows[0].note_id)
    assert dict(note.fields)["Text"] == "Zeile 1<br><b>fett</b>"


def test_add_note_empty_first_field(client, learner):
    _, csrf = learner
    r = add_verse(client, csrf, default_deck(client), ref="  ")
    assert r.status_code == 400 and "nicht leer" in r.text


def test_review_flow_with_undo(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck, ref="Joh 3,16")
    add_verse(client, csrf, deck, ref="Ps 23,1")

    page = client.get(f"/review/{deck}")
    assert page.status_code == 200
    assert "Antwort zeigen" in page.text and 'sandbox title="Vorderseite"' in page.text
    assert "allow-scripts" not in page.text
    card_id = int(re.search(r'name="card_id" value="(\d+)"', page.text).group(1))
    shown = re.search(r'name="shown_at" value="([\d.]+)"', page.text).group(1)

    r = client.post(f"/review/{deck}/answer", headers=HX, data={
        "csrf_token": csrf, "card_id": card_id, "rating": 3, "shown_at": shown})
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<div id="review"')  # partial only
    assert "<html" not in r.text and "Rückgängig" in r.text
    next_id = int(re.search(r'name="card_id" value="(\d+)"', r.text).group(1))
    assert next_id != card_id

    r = client.post(f"/review/{deck}/undo", headers=HX, data={"csrf_token": csrf})
    assert f'name="card_id" value="{card_id}"' in r.text

    # without htmx: redirect back to the review page
    r = client.post(f"/review/{deck}/answer", data={
        "csrf_token": csrf, "card_id": card_id, "rating": 4}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/review/{deck}"


def test_review_done_and_banner(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck)
    page = client.get(f"/review/{deck}")
    card_id = re.search(r'name="card_id" value="(\d+)"', page.text).group(1)
    r = client.post(f"/review/{deck}/answer", headers=HX,
                    data={"csrf_token": csrf, "card_id": card_id, "rating": 4})
    assert "Fertig für heute" in r.text
    assert "Fertig für heute 🎉" in client.get("/").text


def test_stats_cache_updated_after_review(client, learner, db):
    from app.models import UserStats
    user, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck)
    page = client.get(f"/review/{deck}")
    card_id = re.search(r'name="card_id" value="(\d+)"', page.text).group(1)
    client.post(f"/review/{deck}/answer", headers=HX,
                data={"csrf_token": csrf, "card_id": card_id, "rating": 3})
    db.expire_all()
    row = db.get(UserStats, user.id)
    assert row.reviews_today == 1 and row.total_cards == 1 and row.streak_days == 1


def test_browse_edit_suspend_delete(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck, ref="Röm 8,28", text="Alle Dinge")
    r = client.get("/browse?q=Dinge")
    assert "Röm 8,28" in r.text and "neu" in r.text
    note_id = int(re.search(r'href="/notes/(\d+)"', r.text).group(1))

    partial = client.get("/browse?q=Dinge", headers={**HX, "HX-Target": "results"})
    assert "<html" not in partial.text and "Röm 8,28" in partial.text
    assert "ungültig" in client.get("/browse?q=deck:(").text

    edit = client.get(f"/notes/{note_id}")
    assert "Alle Dinge" in edit.text
    r = client.post(f"/notes/{note_id}", data={
        "csrf_token": csrf, "deck_id": deck, "field_0": "Röm 8,28",
        "field_1": "Alle Dinge\nzum Besten", "field_2": ""})
    assert r.status_code == 200 and "Gespeichert" in r.text
    assert "Alle Dinge\nzum Besten" in r.text

    r = client.post(f"/notes/{note_id}/suspend", data={"csrf_token": csrf, "suspended": 1})
    assert "ausgesetzt" in r.text and "Wieder aktivieren" in r.text

    r = client.post(f"/notes/{note_id}/delete", data={"csrf_token": csrf})
    assert r.status_code == 200 and "Keine Karten gefunden" in r.text
    assert client.get(f"/notes/{note_id}").status_code == 404


def test_group_note_readonly_in_ui(client, learner):
    user, csrf = learner
    deck = default_deck(client)
    nid = client.portal.call(lambda: anki_service.add_note(
        user.id, "Basic", deck, {"Front": "G", "Back": "B"}, (anki_service.GROUP_TAG,)))
    page = client.get(f"/notes/{nid}")
    assert "Gruppendeck" in page.text and "Löschen" not in page.text
    assert client.post(f"/notes/{nid}/delete", data={"csrf_token": csrf}).status_code == 403
    r = client.post(f"/notes/{nid}", data={"csrf_token": csrf, "deck_id": deck,
                                            "field_0": "X", "field_1": "Y"})
    assert r.status_code == 400


def test_deck_options(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    page = client.get(f"/decks/{deck}/options")
    assert 'value="10"' in page.text and 'value="0.90"' in page.text
    r = client.post(f"/decks/{deck}/options", data={
        "csrf_token": csrf, "new_per_day": 5, "reviews_per_day": 150, "desired_retention": 0.85})
    assert "Gespeichert" in r.text and 'value="5"' in r.text and 'value="0.85"' in r.text
    r = client.post(f"/decks/{deck}/options", data={
        "csrf_token": csrf, "new_per_day": 5, "reviews_per_day": 150, "desired_retention": 0.99})
    assert r.status_code == 400 and "0,97" in r.text


def test_exports_download_and_cleanup(client, learner, data_dir):
    _, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck)
    assert "Alles herunterladen" in client.get("/export").text

    r = client.post("/export/collection", data={"csrf_token": csrf})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert ".colpkg" in r.headers["content-disposition"]

    r = client.post("/export/deck", data={"csrf_token": csrf, "deck_id": deck})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "Meine_Karten" in r.headers["content-disposition"]
    assert list((data_dir / "exports").iterdir()) == []  # temp files removed

    assert client.post("/export/deck", data={"csrf_token": csrf, "deck_id": 1234}).status_code == 404


def test_users_are_isolated(client, login):
    _, csrf_a = login("Anna")
    deck = default_deck(client)
    add_verse(client, csrf_a, deck, ref="Nur Anna")
    client.cookies.clear()
    login("Ben")
    assert "Nur Anna" not in client.get("/browse").text


def test_security_headers_and_pwa(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/manifest.webmanifest").json()["short_name"] == "CrossPoint"
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
    assert "Keine Verbindung" in client.get("/offline").text
    assert "Datenschutz" in client.get("/privacy").text


def test_bible_text_import(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    page = client.get("/notes/bible")
    assert page.status_code == 200 and "Bibeltext" in page.text

    raw = "¹ Der HERR ist mein Hirte. ² Er weidet mich <script>x</script>auf grüner Aue."
    preview = client.post("/notes/bible/preview", headers=HX, data={"csrf_token": csrf, "text": raw})
    assert "2 Abschnitte" in preview.text and "<script" not in preview.text

    r = client.post("/notes/bible", data={"csrf_token": csrf, "deck_id": deck, "title": "Psalm 23",
                                          "recite": 2, "text": raw})
    assert r.status_code == 200 and "2 Abschnitte mit 6 Karten" in r.text
    rows = client.get('/browse?q="note:Bible Memorizer"').text
    assert "6 Karten" in rows
    note_id = int(re.search(r'href="/notes/(\d+)"', rows).group(1))
    edit = client.get(f"/notes/{note_id}")
    assert "aus dem Text erzeugt" in edit.text and "Speichern" not in edit.text
    assert "<script>x" not in edit.text


def test_bible_text_requires_title(client, learner):
    _, csrf = learner
    r = client.post("/notes/bible", data={"csrf_token": csrf, "deck_id": default_deck(client),
                                          "title": " ", "text": "1 Jesus weinte."})
    assert r.status_code == 400 and "Titel" in r.text


def test_basic_is_question_answer(client, learner):
    r = client.get("/notes/new?notetype=Basic")
    assert "Frage" in r.text and "Antwort" in r.text and 'aria-current="page">Frage & Antwort' in r.text


def test_english_and_language_switch(client, login, monkeypatch):
    monkeypatch.setenv("DEFAULT_LANGUAGE", "en")
    from app.config import get_settings
    get_settings.cache_clear()
    page = client.get("/login")
    assert '<html lang="en">' in page.text and "Log in" in page.text
    # Accept-Language beats the default
    de = client.get("/login", headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.5"})
    assert '<html lang="de">' in de.text and "Anmelden" in de.text
    # the switcher cookie beats Accept-Language
    r = client.get("/lang/en", headers={"referer": "http://testserver/browse?q=x"},
                   follow_redirects=False)
    assert r.headers["location"] == "/browse?q=x"
    assert "Log in" in client.get("/login", headers={"Accept-Language": "de"}).text
    client.get("/lang/xx")  # unknown languages are ignored
    assert client.cookies.get("vf_lang") == "en"


def test_english_collection_and_labels(client, login, monkeypatch):
    monkeypatch.setenv("DEFAULT_LANGUAGE", "en")
    from app.config import get_settings
    get_settings.cache_clear()
    _, csrf = login()
    page = client.get("/")
    assert "My cards" in page.text and "Add card" in page.text
    deck = int(re.search(r'href="/review/(\d+)">My cards<', page.text).group(1))
    client.post("/notes/new", data={"csrf_token": csrf, "notetype": "Basic", "deck_id": deck,
                                    "field_0": "Who led Israel through the Red Sea?",
                                    "field_1": "Moses"})
    review = client.get(f"/review/{deck}")
    assert "Show answer" in review.text and "<10m" in review.text.replace("&lt;", "<")
    # switching to German reopens the collection with a German backend
    client.get("/lang/de")
    assert "Min." in client.get(f"/review/{deck}").text


def test_optimize_button(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    page = client.get(f"/decks/{deck}/options")
    assert "FSRS" in page.text and "disabled" in page.text  # no reviews yet
    r = client.post(f"/decks/{deck}/optimize", data={"csrf_token": csrf})
    assert r.status_code == 200 and "unverändert" in r.text

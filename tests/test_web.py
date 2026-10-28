"""Learner flow through the HTTP layer."""

import html
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
    return int(re.search(r'href="/review/(\d+)">\s*<span class="deck-info">\s*<span class="deck-title"><span class="deck-name">Meine Karten<', page.text).group(1))


def add_verse(client, csrf, deck, ref="Joh 3,16", text="Denn also hat Gott die Welt geliebt"):
    return client.post("/notes/new", data={
        "csrf_token": csrf, "notetype": "Bible Verse", "deck_id": deck,
        "field_0": ref, "field_1": text, "field_2": "LUT17"})


def test_deck_list_and_empty_hint(client, learner):
    r = client.get("/")
    assert r.status_code == 200
    assert "Meine Karten" in r.text and "Noch keine Karten" in r.text
    assert "Erste Karte anlegen" in r.text and 'aria-label="Karte hinzufügen"' in r.text
    assert 'href="/me"' in r.text and "Abmelden" not in r.text  # log out moved to "Me"


def test_start_page_mission_and_review_all(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    client.post("/decks", data={"csrf_token": csrf, "name": "Psalmen"})
    page = client.get("/")
    other = int(re.search(r'href="/review/(\d+)">\s*<span class="deck-info">\s*<span class="deck-title"><span class="deck-name">Psalmen<', page.text).group(1))
    add_verse(client, csrf, deck, ref="Joh 3,16")
    add_verse(client, csrf, other, ref="Ps 23,1")

    page = client.get("/")
    assert 'class="mission-num">2<' in page.text
    assert "2 neu" in page.text and "Los geht's · ~1 Min." in page.text
    assert "0 von 1 gelernt" in page.text and 'href="/review/0"' in page.text

    for _i in range(2):
        r = client.get("/review/0")
        assert "‹ Alle Decks" in r.text
        card_id = re.search(r'name="card_id" value="(\d+)"', r.text).group(1)
        r = client.post("/review/0/answer", headers=HX,
                        data={"csrf_token": csrf, "card_id": card_id, "rating": 4})
        assert r.status_code == 200
    assert "Fertig<br>für heute" in r.text and "2 Karten wiederholt" not in r.text  # per deck
    page = client.get("/")
    assert "Fertig<br>für heute" in page.text and "2 Karten wiederholt." in page.text
    assert "1 Tag" in page.text  # streak pill
    assert re.search(r'<a class="streak-pill" href="/stats"', page.text)


def test_me_page(client, learner, db):
    from app.models import Group, GroupMember
    user, _csrf = learner
    group = Group(name="Jugendfreizeit 2026")
    db.add(group)
    db.flush()
    db.add(GroupMember(group_id=group.id, user_id=user.id))
    db.commit()
    r = client.get("/me")
    assert r.status_code == 200
    assert "Abmelden" in r.text and 'href="/export"' in r.text and "Sprache" in r.text
    assert 'aria-current="page"' in r.text and "Jugendfreizeit 2026" in r.text
    assert 'class="streak-pill"' in r.text and "TEAM" not in r.text  # no admin row for learners


def test_done_screen_offers_next_deck(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    client.post("/decks", data={"csrf_token": csrf, "name": "Psalmen"})
    page = client.get("/")
    other = int(re.search(r'href="/review/(\d+)">\s*<span class="deck-info">\s*<span class="deck-title"><span class="deck-name">Psalmen<', page.text).group(1))
    add_verse(client, csrf, other, ref="Ps 23,1")
    r = client.get(f"/review/{deck}")  # nothing due in "Meine Karten"
    assert "Fertig<br>für heute" in r.text
    assert f'href="/review/{other}"' in r.text and "Nächstes Deck: Psalmen · 1" in r.text


def test_static_fonts_allow_card_frames(client):
    r = client.get("/static/fonts/archivo-latin.woff2")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-origin" not in client.get("/static/app.css").headers


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
    assert "Fertig<br>für heute" in r.text and "Karte wiederholt" in r.text
    assert "Tag in Folge" in r.text and "Nächstes Deck" not in r.text
    assert "Fertig<br>für heute" in client.get("/").text


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
    assert r.status_code == 200 and "Keine Notizen gefunden" in r.text
    assert client.get(f"/notes/{note_id}").status_code == 404


def test_browse_notes_and_cards_suspend_single_card(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    client.post("/notes/new", data={
        "csrf_token": csrf, "notetype": "Bible Verse", "deck_id": deck,
        "field_0": "Joh 3,16", "field_1": "Denn also", "field_2": "", "field_3": "y"})
    notes = client.get("/browse")
    assert "1 Notiz" in notes.text and "2 Karten" in notes.text and 'aria-current="page">Notizen<' in notes.text

    cards = client.get("/browse?view=cards&q=Joh")
    assert "2 Karten" in cards.text and "Text → Reference" in cards.text
    assert 'name="view" value="cards"' in cards.text
    card_id = re.findall(r'action="/cards/(\d+)/suspend"', cards.text)[1]
    r = client.post(f"/cards/{card_id}/suspend", headers=HX,
                    data={"csrf_token": csrf, "suspended": 1, "q": "Joh"})
    assert r.text.lstrip().startswith('<li class="browserow">') and "Wieder aktivieren" in r.text
    assert "1 ausgesetzt" in client.get("/browse").text
    assert "1 Karte" in client.get("/browse?view=cards&q=is:suspended").text

    r = client.post(f"/cards/{card_id}/suspend", data={"csrf_token": csrf, "suspended": 0, "q": "Joh"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/browse?view=cards&q=Joh"
    assert "ausgesetzt" not in client.get("/browse").text
    assert client.post("/cards/999/suspend", data={"csrf_token": csrf, "suspended": 1}).status_code == 404


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
    assert 'value="5"' in page.text and 'value="0.90"' in page.text
    assert page.text.count(" checked>") == 2  # siblings on different days, new + reviews
    r = client.post(f"/decks/{deck}/options", data={
        "csrf_token": csrf, "new_per_day": 7, "reviews_per_day": 150, "desired_retention": 0.85,
        "bury_reviews": "1"})
    assert "Gespeichert" in r.text and 'value="7"' in r.text and 'value="0.85"' in r.text
    assert 'name="bury_new" value="1">' in r.text and 'name="bury_reviews" value="1" checked>' in r.text
    r = client.post(f"/decks/{deck}/options", data={
        "csrf_token": csrf, "new_per_day": 5, "reviews_per_day": 150, "desired_retention": 0.99})
    assert r.status_code == 400 and "0,97" in r.text


def test_rename_deck(client, learner):
    user, csrf = learner
    deck = default_deck(client)
    page = client.get(f"/decks/{deck}/options")
    assert f'action="/decks/{deck}/rename"' in page.text
    r = client.post(f"/decks/{deck}/rename", data={"csrf_token": csrf, "name": "Bibel::Verse"})
    assert r.status_code == 200 and "Deck umbenannt" in r.text and 'value="Bibel::Verse"' in r.text
    assert dict(client.portal.call(anki_service.deck_names, user.id))[deck] == "Bibel::Verse"
    r = client.post(f"/decks/{deck}/rename", data={"csrf_token": csrf, "name": " :: "})
    assert r.status_code == 400 and "Deckname" in r.text
    assert client.post(f"/decks/{deck}/rename", data={"name": "X"}).status_code == 403

    group = client.portal.call(anki_service.create_deck, user.id, "Gruppe")
    client.portal.call(lambda: anki_service.add_note(
        user.id, "Basic", group, {"Front": "G", "Back": "B"}, (anki_service.GROUP_TAG,)))
    assert "/rename" not in client.get(f"/decks/{group}/options").text
    r = client.post(f"/decks/{group}/rename", data={"csrf_token": csrf, "name": "Meins"})
    assert r.status_code == 400 and "Gruppendecks" in r.text


def test_exports_download_and_cleanup(client, learner, data_dir):
    _, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck)
    assert ".colpkg herunterladen" in client.get("/export").text

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
    rows = client.get('/browse?view=cards&q="note:Bible Memorizer"').text
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
    deck = int(re.search(r'href="/review/(\d+)">\s*<span class="deck-info">\s*<span class="deck-title"><span class="deck-name">My cards<', page.text).group(1))
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


def test_stats_page(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck)
    page = client.get(f"/review/{deck}")
    card_id = re.search(r'name="card_id" value="(\d+)"', page.text).group(1)
    client.post(f"/review/{deck}/answer", headers=HX, data={"csrf_token": csrf, "card_id": card_id, "rating": 3})
    r = client.get("/stats")
    assert r.status_code == 200 and "Fortschritt" in r.text
    assert r.text.count('class="hm2') == 16 * 7 and 'class="fc-col"' in r.text
    assert 'class="score-num">1<' in r.text and "Tag in Folge." in r.text and "Für heute gesichert" in r.text
    assert 'class="on"' in r.text  # today in the week strip
    assert client.get(f"/stats?deck={deck}").status_code == 200
    assert client.get("/stats?deck=999").status_code == 200  # unknown deck → all decks


def test_typing_mode(client, learner):
    _, csrf = learner
    deck = default_deck(client)
    add_verse(client, csrf, deck, ref="Ps 23,1", text="Der HERR ist mein Hirte")
    assert 'name="typed"' not in client.get(f"/review/{deck}").text  # off by default
    page = client.get(f"/review/{deck}/typing?on=1")
    assert 'name="typed"' in page.text and 'aria-pressed="true"' in page.text
    card_id = re.search(r'name="card_id" value="(\d+)"', page.text).group(1)
    r = client.post(f"/review/{deck}/check", headers=HX, data={
        "csrf_token": csrf, "card_id": card_id, "typed": "Der Herr ist mein <script>x</script>"})
    assert 'class="typed-diff"' in r.text and "typeGood" in r.text and "typeBad" in r.text
    assert "<script>x" not in r.text
    assert 'class="show-toggle" aria-hidden="true" tabindex="-1" checked' in r.text
    assert f'name="card_id" value="{card_id}"' in r.text  # same card, ready to rate
    client.get(f"/review/{deck}/typing?on=0")
    assert 'name="typed"' not in client.get(f"/review/{deck}").text


def test_image_upload_and_media_route(client, learner):
    from tests.test_anki_service import PNG
    from app.auth import media_token
    user, csrf = learner
    deck = default_deck(client)
    r = client.post("/notes/new", data={"csrf_token": csrf, "notetype": "Basic", "deck_id": deck,
                                        "field_0": "Wer ist das?", "field_1": "Mose",
                                        "image_side": "front"},
                    files={"image": ("bild.png", PNG, "image/png")})
    assert r.status_code == 200 and "Hinzugefügt: Wer ist das?" in r.text
    review = html.unescape(client.get(f"/review/{deck}").text)
    token = media_token(user.id)
    name = re.search(rf'<img src="/media/{user.id}/{token}/([0-9a-f]+\.png)">', review)
    assert name, "image not in card"
    url = f"/media/{user.id}/{token}/{name.group(1)}"
    client.cookies.clear()  # sandboxed iframe: no cookies
    img = client.get(url)
    assert img.status_code == 200 and img.content == PNG and img.headers["content-type"] == "image/png"
    assert client.get(f"/media/{user.id}/{'0' * 32}/{name.group(1)}").status_code == 404
    assert client.get(f"/media/{user.id}/{token}/collection.anki2").status_code == 404


def test_image_upload_rejects_non_images(client, learner):
    _, csrf = learner
    r = client.post("/notes/new", data={"csrf_token": csrf, "notetype": "Basic",
                                        "deck_id": default_deck(client), "field_0": "Q", "field_1": "A"},
                    files={"image": ("x.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")})
    assert r.status_code == 400 and "PNG" in r.text


def test_apkg_import_into_own_collection_sanitizes(client, learner, tmp_path):
    user, csrf = learner
    r = client.get("/import?deck_id=5")
    assert r.status_code == 200 and 'aria-current="page">Anki-Deck<' in r.text
    assert 'href="/notes/bible?deck_id=5"' in client.get("/import?deck_id=5").text
    assert 'href="/import?deck_id=' in client.get("/notes/new").text
    did = client.portal.call(anki_service.create_deck, "SRC", "Johannes")
    client.portal.call(anki_service.add_note, "SRC", "Basic", did,
                       {"Front": "<b>F</b><script>alert(1)</script>", "Back": '<a href="x">B</a>'})
    pkg = tmp_path / "joh.apkg"
    client.portal.call(anki_service.export_deck, "SRC", did, pkg, False)
    r = client.post("/import", data={"csrf_token": csrf},
                    files={"file": ("joh.apkg", pkg.read_bytes())})
    assert r.status_code == 200 and "importiert" in r.text
    rows, total = client.portal.call(anki_service.search_cards, user.id, "deck:Johannes")
    note = client.portal.call(anki_service.get_note, user.id, rows[0].note_id)
    assert total == 1 and dict(note.fields) == {"Front": "<b>F</b>", "Back": "B"}
    for name, data in (("x.colpkg", pkg.read_bytes()), ("x.apkg", b"not a zip")):
        r = client.post("/import", data={"csrf_token": csrf}, files={"file": (name, data)})
        assert r.status_code == 400
    r = client.post("/import", files={"file": ("joh.apkg", pkg.read_bytes())})
    assert r.status_code == 403

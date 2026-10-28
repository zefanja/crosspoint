"""Shared editing of group decks by admins and group leaders (SPEC §7.3)."""

import html
import re
import time

import pytest

from app import anki_service, distribution
from app.auth import SESSION_COOKIE, csrf_token_for
from app.models import Group, GroupDeck, GroupDeckEdit, GroupLeader, GroupMember

A = anki_service.ADMIN_COLLECTION


def wait_job(client, response):
    m = re.search(r"/admin/jobs/([0-9a-f]+)", response.headers.get("location", ""))
    assert m, "no job started"
    return client.portal.call(distribution.wait, m.group(1))


@pytest.fixture
def setup(client, make_user, db):
    """Lea leads Jugend and Senioren; Hauskreis belongs to someone else.

    Psalmen → Jugend (editable), Sprüche → Jugend + Hauskreis (view only),
    Geheim → Hauskreis (invisible), Bibel::Johannes → Senioren (editable, parent unassigned).
    """
    groups = {}
    for name in ("Jugend", "Senioren", "Hauskreis"):
        g = Group(name=name)
        db.add(g)
        db.flush()
        groups[name] = g.id
    call = client.portal.call
    decks, notes = {}, {}
    for name in ("Psalmen", "Sprüche", "Geheim", "Bibel::Johannes"):
        decks[name] = call(anki_service.create_deck, A, name)
        notes[name] = call(anki_service.add_note, A, "Basic", decks[name],
                           {"Front": f"Frage {name}", "Back": "Antwort"}, (anki_service.GROUP_TAG,))
    for deck, group in (("Psalmen", "Jugend"), ("Sprüche", "Jugend"), ("Sprüche", "Hauskreis"),
                        ("Geheim", "Hauskreis"), ("Bibel::Johannes", "Senioren")):
        db.add(GroupDeck(group_id=groups[group], admin_deck_id=decks[deck], deck_name=deck))
    lea, code = make_user("Lea", "leader")
    member, _code = make_user("Mia")
    db.add_all([GroupLeader(group_id=groups["Jugend"], user_id=lea.id),
                GroupLeader(group_id=groups["Senioren"], user_id=lea.id),
                GroupMember(group_id=groups["Jugend"], user_id=member.id)])
    db.commit()
    client.get(f"/login?c={code}")
    csrf = csrf_token_for(client.cookies[SESSION_COOKIE])
    r = client.post("/admin/workspace", data={"csrf_token": csrf, "ws": "admin"},
                    follow_redirects=False)
    assert r.status_code == 303
    return {"csrf": csrf, "groups": groups, "decks": decks, "notes": notes, "lea": lea,
            "member": member}


def test_leader_sees_only_own_groups_decks(client, setup):
    page = client.get("/")
    assert "Gruppendecks deiner Gruppen" in page.text
    for name in ("Psalmen", "Sprüche", "Bibel::Johannes"):
        assert f'<span class="deck-name">{name}</span>' in page.text, name
    assert "Geheim" not in page.text and "Meine Karten" not in page.text
    assert page.text.count("NUR ANSEHEN") == 1  # Sprüche also goes to Hauskreis

    browse = client.get("/browse").text
    assert "Frage Psalmen" in browse and "Frage Sprüche" in browse and "Frage Bibel" in browse
    assert "Frage Geheim" not in browse
    assert "Frage Geheim" not in client.get("/browse?q=deck:Geheim").text
    assert "Frage Geheim" not in client.get("/browse?q=Frage&view=cards").text
    assert "Geheim" not in client.get("/export").text


def test_leader_edit_permissions(client, setup):
    csrf, decks, notes = setup["csrf"], setup["decks"], setup["notes"]
    assert "Speichern" in client.get(f"/notes/{notes['Psalmen']}").text
    readonly = client.get(f"/notes/{notes['Sprüche']}").text
    assert "nur ansehen" in readonly and "Speichern" not in readonly
    assert client.get(f"/notes/{notes['Geheim']}").status_code == 404

    def edit(note, deck, **extra):
        return client.post(f"/notes/{note}", data={"csrf_token": csrf, "deck_id": deck,
                                                   "field_0": "Neu", "field_1": "B", **extra},
                           follow_redirects=False)

    assert edit(notes["Sprüche"], decks["Sprüche"]).status_code == 403
    assert edit(notes["Psalmen"], decks["Geheim"]).status_code == 403  # no moving out
    assert edit(notes["Geheim"], decks["Psalmen"]).status_code == 404
    assert client.post(f"/notes/{notes['Sprüche']}/delete", data={"csrf_token": csrf}).status_code == 403
    assert client.post(f"/notes/{notes['Geheim']}/delete", data={"csrf_token": csrf}).status_code == 404
    assert client.post(f"/notes/{notes['Sprüche']}/suspend",
                       data={"csrf_token": csrf, "suspended": 1}).status_code == 403
    rows, _total = client.portal.call(anki_service.search_cards, A, f"nid:{notes['Sprüche']}")
    assert client.post(f"/cards/{rows[0].card_id}/suspend",
                       data={"csrf_token": csrf, "suspended": 1}).status_code == 403

    assert edit(notes["Psalmen"], decks["Bibel::Johannes"]).status_code == 303
    note = client.portal.call(anki_service.get_note, A, notes["Psalmen"])
    assert dict(note.fields)["Front"] == "Neu" and note.deck_ids == [decks["Bibel::Johannes"]]
    assert "Zuletzt geändert von Lea" in client.get(f"/notes/{notes['Psalmen']}").text

    # adding: only into editable decks, tagged as group-deck material
    form = client.get("/notes/new").text
    assert "Psalmen" in form and "Sprüche" not in form and "Geheim" not in form
    add = {"csrf_token": csrf, "notetype": "Basic", "field_0": "Q2", "field_1": "A2"}
    assert client.post("/notes/new", data={**add, "deck_id": decks["Sprüche"]}).status_code == 403
    assert client.post("/notes/new", data={**add, "deck_id": decks["Psalmen"]}).status_code == 200
    rows, _total = client.portal.call(anki_service.search_cards, A, "Q2")
    assert client.portal.call(anki_service.get_note, A, rows[0].note_id).readonly  # tagged
    assert client.post("/notes/bible", data={"csrf_token": csrf, "deck_id": decks["Geheim"],
                                             "title": "Ps 1", "text": "1 Wohl dem"}).status_code == 403


def test_leader_workspace_blocks_study_and_import(client, setup):
    csrf, deck = setup["csrf"], setup["decks"]["Psalmen"]
    for path in (f"/review/{deck}", "/review/0", "/stats", "/import", f"/decks/{deck}/options"):
        r = client.get(path)
        assert r.status_code == 403 and "Gruppendecks bearbeitest" in r.text, path
    assert client.post("/export/collection", data={"csrf_token": csrf}).status_code == 403
    assert client.post("/import", data={"csrf_token": csrf},
                       files={"file": ("x.apkg", b"x")}).status_code == 403
    r = client.post("/export/deck", data={"csrf_token": csrf, "deck_id": setup["decks"]["Geheim"]})
    assert r.status_code == 404
    assert client.post("/export/deck", data={"csrf_token": csrf, "deck_id": deck}).status_code == 200
    assert "/admin/group-decks" in client.get("/").text  # tab bar: distribute instead of stats

    # back in the own collection everything works as before
    client.post("/admin/workspace", data={"csrf_token": csrf, "ws": "own"})
    assert client.get("/stats").status_code == 200
    assert "Meine Karten" in client.get("/").text


def test_leader_creates_decks(client, setup, db):
    csrf, groups = setup["csrf"], setup["groups"]

    def create(name, group=None):
        data = {"csrf_token": csrf, "name": name}
        if group:
            data["group_id"] = groups[group]
        return client.post("/decks", data=data, follow_redirects=False)

    assert create("Neu", "Senioren").status_code == 303
    gd = db.query(GroupDeck).filter_by(deck_name="Neu").one()
    assert gd.group_id == groups["Senioren"] and gd.last_distributed_at is None
    assert create("Psalmen::Teil 2").status_code == 303  # inherits Jugend
    assert db.query(GroupDeck).filter_by(deck_name="Psalmen::Teil 2").count() == 0
    assert "Teil 2" in client.get("/").text
    assert "gibt es schon" in create("geheim", "Jugend").text  # case-insensitive
    assert "nicht leitest" in create("Sprüche::X").text
    assert "Gruppe" in create("Anders", "Hauskreis").text  # not Lea's group
    names = dict(client.portal.call(anki_service.deck_names, A)).values()
    assert "Sprüche::X" not in names and "Anders" not in names


def test_group_decks_page_distribute_and_log(client, setup, db):
    csrf, decks, member = setup["csrf"], setup["decks"], setup["member"]
    client.post("/notes/new", data={"csrf_token": csrf, "notetype": "Basic",
                                    "deck_id": decks["Psalmen"], "field_0": "Ps 23", "field_1": "Hirte"})
    page = html.unescape(client.get("/admin/group-decks").text)
    assert "Psalmen" in page and "Bibel::Johannes" in page and "Geheim" not in page
    assert "Lea hat „Ps 23“ hinzugefügt" in page and "1 Änderung noch nicht verteilt" in page

    own = db.query(GroupDeck).filter_by(deck_name="Psalmen").one()
    foreign = db.query(GroupDeck).filter_by(deck_name="Geheim").one()
    assert client.post(f"/admin/group-decks/{foreign.id}/distribute",
                       data={"csrf_token": csrf}).status_code == 404
    job = wait_job(client, client.post(f"/admin/group-decks/{own.id}/distribute",
                                       data={"csrf_token": csrf}, follow_redirects=False))
    assert job.finished and job.errors == [] and job.done == 1
    assert client.portal.call(anki_service.search_cards, member.id, '"deck:Psalmen"')[1] == 2
    assert "Änderung noch nicht verteilt" not in client.get("/admin/group-decks").text
    assert db.query(GroupDeckEdit).filter_by(user_id=setup["lea"].id, action="add").count() == 1


def test_leader_renames_group_deck_for_members(client, setup, db):
    csrf, decks, member = setup["csrf"], setup["decks"], setup["member"]
    call = client.portal.call
    own = db.query(GroupDeck).filter_by(deck_name="Psalmen").one()
    wait_job(client, client.post(f"/admin/group-decks/{own.id}/distribute",
                                 data={"csrf_token": csrf}, follow_redirects=False))
    sub = call(anki_service.create_deck, A, "Psalmen::Teil 1")  # not distributed yet

    page = client.get("/")
    assert f'href="/decks/{decks["Psalmen"]}/rename"' in page.text
    assert f'href="/decks/{decks["Sprüche"]}/rename"' not in page.text  # view only
    assert "Nur der letzte Teil" in client.get(f"/decks/{decks['Psalmen']}/rename").text
    for deck in ("Sprüche", "Geheim"):
        r = client.post(f"/decks/{decks[deck]}/rename", data={"csrf_token": csrf, "name": "X"})
        assert r.status_code in (403, 404)
    r = client.post(f"/decks/{decks['Psalmen']}/rename", data={"csrf_token": csrf, "name": "Sprüche"})
    assert r.status_code == 400 and "gibt es schon" in r.text

    job = wait_job(client, client.post(f"/decks/{decks['Psalmen']}/rename", data={
        "csrf_token": csrf, "name": "Psalter"}, follow_redirects=False))
    assert job.finished and job.errors == [] and job.done == 1
    names = dict(call(anki_service.deck_names, A))
    assert names[decks["Psalmen"]] == "Psalter" and names[sub] == "Psalter::Teil 1"
    db.expire_all()
    assert own.deck_name == "Psalter"
    member_names = [n for _id, n in call(anki_service.deck_names, member.id)]
    assert "Psalter" in member_names and "Psalmen" not in member_names

    # distributing again updates the renamed deck instead of creating "Psalmen" again
    call(anki_service.add_note, A, "Basic", decks["Psalmen"], {"Front": "Neu", "Back": "B"},
         (anki_service.GROUP_TAG,))
    wait_job(client, client.post(f"/admin/group-decks/{own.id}/distribute",
                                 data={"csrf_token": csrf}, follow_redirects=False))
    member_names = [n for _id, n in call(anki_service.deck_names, member.id)]
    assert "Psalmen" not in member_names
    assert call(anki_service.search_cards, member.id, '"deck:Psalter"')[1] == 2

    # subdecks too; moving to another parent deck is refused
    job = wait_job(client, client.post(f"/decks/{sub}/rename", data={
        "csrf_token": csrf, "name": "Psalter::Teil B"}, follow_redirects=False))
    assert job.errors == [] and dict(call(anki_service.deck_names, A))[sub] == "Psalter::Teil B"
    r = client.post(f"/decks/{sub}/rename", data={"csrf_token": csrf, "name": "Geheim::Teil B"})
    assert r.status_code == 400 and "nicht in ein anderes Elterndeck" in r.text


def test_concurrent_edit_is_not_overwritten(client, setup):
    csrf, deck, nid = setup["csrf"], setup["decks"]["Psalmen"], setup["notes"]["Psalmen"]
    form = client.get(f"/notes/{nid}").text
    mod = re.search(r'name="mod" value="(\d+)"', form).group(1)
    time.sleep(1.1)  # mod has a resolution of seconds
    client.portal.call(lambda: anki_service.update_note(  # another leader saves first
        A, nid, {"Front": "Von Ben"}, deck, allow_group=True))
    data = {"csrf_token": csrf, "deck_id": deck, "field_0": "Von Lea", "field_1": "B", "mod": mod}
    r = client.post(f"/notes/{nid}", data=data, follow_redirects=False)
    assert r.status_code == 400 and "inzwischen geändert" in r.text
    note = client.portal.call(anki_service.get_note, A, nid)
    assert dict(note.fields)["Front"] == "Von Ben"
    data["mod"] = re.search(r'name="mod" value="(\d+)"', r.text).group(1)  # save again
    assert client.post(f"/notes/{nid}", data=data, follow_redirects=False).status_code == 303
    assert dict(client.portal.call(anki_service.get_note, A, nid).fields)["Front"] == "Von Lea"


def test_admin_keeps_full_access(client, setup, login):
    client.cookies.clear()
    _admin, csrf = login("Admin", role="admin")
    client.post("/admin/workspace", data={"csrf_token": csrf, "ws": "admin"})
    page = client.get("/")
    assert "Geheim" in page.text and "Admin-Sammlung" in page.text
    assert "Speichern" in client.get(f"/notes/{setup['notes']['Geheim']}").text
    assert client.get(f"/review/{setup['decks']['Geheim']}").status_code == 200
    groups = client.get("/admin/group-decks").text
    assert "Geheim" in groups and "Hauskreis" in groups

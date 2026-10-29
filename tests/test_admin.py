"""Phase 2: admin dashboard, codes, groups, distribution, progress."""

import re

import pytest

from app import anki_service, distribution
from app.auth import SESSION_COOKIE, csrf_token_for, format_code

HX = {"HX-Request": "true"}


@pytest.fixture
def admin(client, login):
    user, csrf = login("Admin", role="admin")
    return user, csrf


def wait_job(client, response):
    m = re.search(r"/admin/jobs/([0-9a-f]+)", response.headers.get("location", "") or response.text)
    assert m, "no job started"
    return client.portal.call(distribution.wait, m.group(1))


def codes_from_sheet(html):
    return re.findall(r'class="lc-code">([A-Z0-9-]+)<', html)


def test_learner_cannot_access_admin(client, login):
    _, csrf = login("Anna")
    for path in ("/admin", "/admin/users", "/admin/groups", "/admin/progress", "/admin/import"):
        assert client.get(path).status_code == 403, path
    assert client.post("/admin/users", data={"csrf_token": csrf, "names": "X"}).status_code == 403
    assert client.post("/admin/workspace", data={"csrf_token": csrf, "ws": "admin"}).status_code == 403
    assert "/admin" not in client.get("/").text


def test_create_users_print_sheet_and_login(client, admin):
    _, csrf = admin
    r = client.post("/admin/users", data={"csrf_token": csrf, "names": "Anna\nBen\n\n Cleo "})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    codes = codes_from_sheet(r.text)
    assert len(codes) == 3 and "<svg" in r.text and "/login?c=" not in r.text  # QR only as SVG
    assert all(re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", c) for c in codes)
    client.cookies.clear()
    assert client.get(f"/login?c={codes[1]}", follow_redirects=False).headers["location"] == "/"


def test_reset_code_and_bulk_print(client, admin, make_user, db):
    _, csrf = admin
    user, old = make_user("Anna")
    r = client.post(f"/admin/users/{user.id}/reset-code", data={"csrf_token": csrf})
    new = codes_from_sheet(r.text)[0]
    assert new != format_code(old)
    r = client.post("/admin/users/print", data={"csrf_token": csrf, "user_ids": [user.id]})
    assert len(codes_from_sheet(r.text)) == 1 and codes_from_sheet(r.text)[0] != new


def test_disable_delete_and_self_protection(client, admin, make_user):
    me, csrf = admin
    user, code = make_user("Ben")
    client.portal.call(anki_service.ensure_collection, user.id)
    client.post(f"/admin/users/{user.id}/toggle-disabled", data={"csrf_token": csrf})
    assert "deaktiviert" in client.get(f"/admin/users/{user.id}").text
    r = client.post(f"/admin/users/{user.id}/delete", data={"csrf_token": csrf, "confirm_name": "ben"})
    assert r.status_code == 400
    r = client.post(f"/admin/users/{user.id}/delete", data={"csrf_token": csrf, "confirm_name": "Ben"})
    assert r.status_code == 200 and "Ben" not in r.text.split("Nutzer anlegen")[0]
    assert not anki_service.collection_path(user.id).exists()
    assert client.post(f"/admin/users/{me.id}/delete",
                       data={"csrf_token": csrf, "confirm_name": "Admin"}).status_code == 400
    assert client.post(f"/admin/users/{me.id}/toggle-disabled",
                       data={"csrf_token": csrf}).status_code == 400
    r = client.post(f"/admin/users/{me.id}", data={"csrf_token": csrf, "display_name": "Admin",
                                                  "role": "learner"})
    assert r.status_code == 400


def test_workspace_admin_collection(client, admin):
    _, csrf = admin
    client.post("/admin/workspace", data={"csrf_token": csrf, "ws": "admin"})
    page = client.get("/")
    assert "Admin-Sammlung" in page.text
    deck = int(re.search(r'href="/review/(\d+)">\s*<span class="deck-info">\s*<span class="deck-title"><span class="deck-name">Meine Karten<', page.text).group(1))
    client.post("/notes/new", data={"csrf_token": csrf, "notetype": "Basic", "deck_id": deck,
                                    "field_0": "Q", "field_1": "A"})
    rows, _ = client.portal.call(anki_service.search_cards, anki_service.ADMIN_COLLECTION,
                                 f"tag:{anki_service.GROUP_TAG}")
    assert len(rows) == 1
    edit = client.get(f"/notes/{rows[0].note_id}")
    assert "Speichern" in edit.text  # admins may edit group notes in the admin collection
    assert "auch bei allen Gruppenmitgliedern" in client.get(f"/decks/{deck}/options").text
    r = client.post(f"/decks/{deck}/rename", data={"csrf_token": csrf, "name": "Gruppe"},
                    follow_redirects=False)  # not distributed yet: no job
    assert r.headers["location"] == f"/decks/{deck}/options?renamed=1"
    assert dict(client.portal.call(anki_service.deck_names, anki_service.ADMIN_COLLECTION))[deck] == "Gruppe"
    client.post("/admin/workspace", data={"csrf_token": csrf, "ws": "own"})
    assert "Admin-Sammlung" not in client.get("/").text


def test_import_apkg_into_admin_collection(client, admin, tmp_path):
    _, csrf = admin
    did = client.portal.call(anki_service.create_deck, "SRC", "Psalmen")
    client.portal.call(anki_service.add_note, "SRC", "Basic", did, {"Front": "F", "Back": "B"})
    pkg = tmp_path / "x.apkg"
    client.portal.call(anki_service.export_deck, "SRC", did, pkg)
    r = client.post("/admin/import", data={"csrf_token": csrf},
                    files={"file": ("psalmen.apkg", pkg.read_bytes())})
    assert r.status_code == 200 and "importiert" in r.text
    names = dict(client.portal.call(anki_service.deck_names, anki_service.ADMIN_COLLECTION))
    assert "Psalmen" in names.values()
    r = client.post("/admin/import", data={"csrf_token": csrf},
                    files={"file": ("x.apkg", b"not a zip")})
    assert r.status_code == 400


def _review_all(client, user_id, deck_id, n):
    for _ in range(n):
        state = client.portal.call(anki_service.review_state, user_id, deck_id)
        if state.card is None:
            return
        client.portal.call(anki_service.answer_card, user_id, deck_id, state.card.card_id, 3, 2.0)


def test_acceptance_two_groups_distribute_fix_redistribute(client, admin, db):
    """SPEC §12 Phase 2 acceptance, smaller: users in 2 groups, distribute, correct the
    deck, distribute again – learning progress stays."""
    _, csrf = admin
    A = anki_service.ADMIN_COLLECTION
    adeck = client.portal.call(anki_service.create_deck, A, "Psalm 23")
    nid = client.portal.call(anki_service.add_note, A, "Bible Verse", adeck,
                             {"Reference": "Ps 23,1", "Text": "Der HERR ist mein Hirt"})
    client.portal.call(anki_service.add_note, A, "Bible Verse", adeck,
                       {"Reference": "Ps 23,2", "Text": "Er weidet mich"})

    for name in ("Jugend", "Hauskreis"):
        client.post("/admin/groups", data={"csrf_token": csrf, "name": name})
    from app.models import Group
    group_ids = [db.query(Group).filter_by(name=n).one().id for n in ("Jugend", "Hauskreis")]
    r = client.post("/admin/users", data={"csrf_token": csrf, "names": "A1\nA2\nA3",
                                          "group_id": group_ids[0]})
    assert len(codes_from_sheet(r.text)) == 3
    client.post("/admin/users", data={"csrf_token": csrf, "names": "B1\nB2\nB3",
                                      "group_id": group_ids[1]})
    from app.models import GroupMember, User
    members = {g: [u for (u,) in db.query(GroupMember.user_id).filter_by(group_id=g)] for g in group_ids}
    assert all(len(v) == 3 for v in members.values())

    page = client.get(f"/admin/groups/{group_ids[0]}")
    assert "Psalm 23" in page.text
    for gid in group_ids:
        job = wait_job(client, client.post(f"/admin/groups/{gid}/decks",
                                           data={"csrf_token": csrf, "admin_deck_id": adeck},
                                           follow_redirects=False))
        assert job.finished and job.errors == [] and job.done == 3

    # by name: member order is undefined, and the progress check below expects A1
    learner = db.query(User).filter_by(display_name="A1").one().id
    udeck = next(d.id for d in client.portal.call(anki_service.deck_list, learner) if d.name == "Psalm 23")
    _review_all(client, learner, udeck, 4)
    before = client.portal.call(anki_service.collection_stats, learner)
    assert before.reviews_today >= 2

    # correct the deck in the admin collection, add a verse, redistribute
    import time
    time.sleep(1.1)  # anki skips notes with an unchanged mtime (seconds)
    client.portal.call(lambda: anki_service.update_note(
        A, nid, {"Text": "Der HERR ist mein Hirte"}, adeck, allow_group=True))
    client.portal.call(anki_service.add_note, A, "Bible Verse", adeck, {"Reference": "Ps 23,3", "Text": "Er erquicket"})
    gd = int(re.search(rf'/admin/groups/{group_ids[0]}/decks/(\d+)/distribute', client.get(f"/admin/groups/{group_ids[0]}").text).group(1))
    job = wait_job(client, client.post(f"/admin/groups/{group_ids[0]}/decks/{gd}/distribute",
                                       data={"csrf_token": csrf}, follow_redirects=False))
    assert job.errors == []

    rows, total = client.portal.call(anki_service.search_cards, learner, '"deck:Psalm 23"')
    assert total == 3
    texts = {}
    for row in rows:
        note = client.portal.call(anki_service.get_note, learner, row.note_id)
        texts[dict(note.fields)["Reference"]] = (dict(note.fields)["Text"], row.status, note.readonly)
    assert texts["Ps 23,1"][0] == "Der HERR ist mein Hirte"
    assert texts["Ps 23,1"][1] == "scheduled"  # learning progress kept
    assert texts["Ps 23,3"][1] == "new" and all(t[2] for t in texts.values())
    after = client.portal.call(anki_service.collection_stats, learner)
    assert after.reviews_today == before.reviews_today
    names = client.portal.call(anki_service._run, learner, lambda col: [m["name"] for m in col.models.all()])
    assert not [n for n in names if n.endswith("+")]

    # other group untouched by the second distribution: still 2 notes
    other = members[group_ids[1]][0]
    assert client.portal.call(anki_service.search_cards, other, '"deck:Psalm 23"')[1] == 2

    # a new member gets all group decks automatically
    new_user = db.query(User).filter_by(display_name="B1").one()
    r = client.post(f"/admin/groups/{group_ids[0]}/members", data={
        "csrf_token": csrf, "user_ids": members[group_ids[0]] + [new_user.id]}, follow_redirects=False)
    wait_job(client, r)
    assert client.portal.call(anki_service.search_cards, new_user.id, '"deck:Psalm 23"')[1] == 3

    # progress table
    prog = client.get(f"/admin/progress?group={group_ids[0]}&sort=today&desc=1")
    assert prog.status_code == 200 and ">A1</a>" in prog.text
    # match the link text: user ids (ULIDs) in the hrefs may contain "A2" by chance
    assert prog.text.index(">A1</a>") < prog.text.index(">A2</a>")  # A1 reviewed today
    job = wait_job(client, client.post("/admin/progress/refresh",
                                       data={"csrf_token": csrf, "group": group_ids[0]},
                                       follow_redirects=False))
    assert job.done == 4
    assert "fertig" in client.get(f"/admin/jobs/{job.id}", headers=HX).text


def test_group_delete_requires_name(client, admin):
    _, csrf = admin
    client.post("/admin/groups", data={"csrf_token": csrf, "name": "Test"})
    gid = re.search(r'href="/admin/groups/(\d+)"', client.get("/admin/groups").text).group(1)
    r = client.post(f"/admin/groups/{gid}/delete", data={"csrf_token": csrf, "confirm_name": "x"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("?error=name")
    client.post(f"/admin/groups/{gid}/delete", data={"csrf_token": csrf, "confirm_name": "Test"})
    assert "Test" not in client.get("/admin/groups").text
    dup = client.post("/admin/groups", data={"csrf_token": csrf, "name": "Neu"})
    dup = client.post("/admin/groups", data={"csrf_token": csrf, "name": "Neu"})
    assert "gibt es schon" in dup.text


def test_group_leader_sees_only_own_group(client, login, make_user, db):
    from app.auth import SESSION_COOKIE
    from app.models import Group, GroupMember, User

    _, admin_csrf = login("Admin", role="admin")
    for name in ("Jugend", "Senioren"):
        client.post("/admin/groups", data={"csrf_token": admin_csrf, "name": name})
    g1, g2 = (db.query(Group).filter_by(name=n).one().id for n in ("Jugend", "Senioren"))
    anna, _ = make_user("Anna")
    otto, _ = make_user("Otto")
    leader, leader_code = make_user("Lea")
    db.add_all([GroupMember(group_id=g1, user_id=anna.id), GroupMember(group_id=g2, user_id=otto.id)])
    db.commit()
    client.post(f"/admin/groups/{g1}/leaders", data={"csrf_token": admin_csrf, "user_ids": [leader.id]})
    db.expire_all()
    assert db.get(User, leader.id).role == "leader"
    assert "Gruppenleitung" in client.get(f"/admin/groups/{g1}").text

    client.cookies.clear()
    client.get(f"/login?c={leader_code}")
    csrf = csrf_token_for(client.cookies[SESSION_COOKIE])
    assert "Verwaltung" in client.get("/").text
    home = client.get("/admin")
    assert home.status_code == 200 and "Admin-Sammlung" not in home.text
    prog = client.get("/admin/progress")
    assert "Anna" in prog.text and "Otto" not in prog.text
    assert "Otto" not in client.get(f"/admin/progress?group={g2}").text  # foreign group ignored
    users = client.get("/admin/users")
    assert "Anna" in users.text and "Otto" not in users.text and "Nutzer anlegen" not in users.text

    # codes only for own members
    r = client.post("/admin/users/print", data={"csrf_token": csrf, "user_ids": [anna.id, otto.id]})
    assert len(codes_from_sheet(r.text)) == 1 and "Anna" in r.text and "Otto" not in r.text

    for path in ("/admin/groups", f"/admin/groups/{g1}", f"/admin/users/{anna.id}", "/admin/import"):
        assert client.get(path).status_code == 403, path
    assert client.post("/admin/users", data={"csrf_token": csrf, "names": "X"}).status_code == 403
    job = wait_job(client, client.post("/admin/progress/refresh", data={"csrf_token": csrf},
                                       follow_redirects=False))
    assert job.done == 1  # only Anna

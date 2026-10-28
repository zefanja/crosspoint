"""Every anki_service function against a real temporary collection."""

import time

import pytest
from anki._backend import RustBackend
from anki.collection import Collection, ImportAnkiPackageOptions, ImportAnkiPackageRequest

from app import anki_service as svc

U = "01TESTUSER0000000000000000"
DECK = "Meine Karten"  # "My cards" in German (DEFAULT_LANGUAGE=de)


def add_verse(run, deck_id, ref="Joh 3,16", text="Denn also hat Gott die Welt geliebt", tags=()):
    return run(svc.add_note(U, svc.BIBLE_VERSE, deck_id,
                            {"Reference": ref, "Text": text, "Translation": "LUT17"}, tags))


@pytest.fixture
def deck(run):
    decks = run(svc.deck_list(U))
    return next(d.id for d in decks if d.name == DECK)


def test_new_collection_setup(run, data_dir):
    run(svc.ensure_collection(U))
    assert svc.collection_path(U).exists()
    assert svc.collection_ids() == [U]
    run(svc.close_all())
    col = Collection(str(svc.collection_path(U)))
    try:
        assert col.get_config("fsrs") is True
        assert col.get_preferences().scheduling.rollover == 4
        assert col.models.by_name("Bible Verse") is not None
        assert col.models.by_name("Basic") is not None
        assert col.decks.by_name(DECK) is not None
        conf = col.decks.config_dict_for_deck_id(1)
        assert conf["new"]["perDay"] == 5
        assert conf["new"]["bury"] is True and conf["rev"]["bury"] is True
    finally:
        col.close()


def test_invalid_user_id_rejected():
    with pytest.raises(ValueError):
        svc.collection_path("../etc")


def test_deck_list_hides_empty_default(run):
    names = [d.name for d in run(svc.deck_list(U))]
    assert names == [DECK]


def test_create_deck_and_names(run, deck):
    did = run(svc.create_deck(U, " Bibel :: Psalmen "))
    names = dict(run(svc.deck_names(U)))
    assert names[did] == "Bibel::Psalmen"
    tree = run(svc.deck_list(U))
    child = next(d for d in tree if d.id == did)
    assert (child.short_name, child.depth) == ("Psalmen", 1)
    with pytest.raises(svc.AnkiServiceError):
        run(svc.create_deck(U, " :: "))


def test_deck_list_card_counts(run, deck):
    child = run(svc.create_deck(U, f"{DECK}::Psalmen"))
    add_verse(run, deck, ref="Joh 3,16")
    nid = add_verse(run, child, ref="Ps 23,1", tags=(svc.GROUP_TAG,))
    add_verse(run, child, ref="Ps 23,2")

    def mature_one(col):
        cid = col.card_ids_of_note(nid)[0]
        col.db.execute("update cards set type = 2, queue = 2, ivl = 30 where id = ?", cid)

    run(svc._run(U, mature_one))
    rows = {d.id: d for d in run(svc.deck_list(U))}
    assert (rows[deck].total, rows[deck].mature, rows[deck].group) == (3, 1, False)
    assert (rows[child].total, rows[child].mature, rows[child].group) == (2, 1, True)


def test_review_all_decks(run, deck):
    other = run(svc.create_deck(U, "Psalmen"))
    add_verse(run, deck, ref="Joh 3,16")
    add_verse(run, other, ref="Ps 23,1")
    state = run(svc.review_state(U, svc.ALL_DECKS))
    assert (state.new, state.learn, state.review) == (2, 0, 0)
    assert state.deck_name == svc.ALL_DECKS_NAME and state.card is not None
    seen = set()
    while state.card is not None:
        seen.add(state.card.card_id)
        state, answered = run(svc.answer_card(U, svc.ALL_DECKS, state.card.card_id, 4, 3.0))
        assert answered and state.can_undo  # also right after switching decks
    assert len(seen) == 2 and (state.new, state.learn, state.review) == (0, 0, 0)
    state, undone = run(svc.undo_review(U, svc.ALL_DECKS))
    assert undone and state.card is not None and state.new == 1


def test_notetypes_for_adding(run):
    nts = run(svc.notetypes_for_adding(U))
    assert [n.name for n in nts] == ["Bible Verse", "Basic"]
    assert nts[0].fields == ["Reference", "Text", "Translation", "Reverse"]


def test_add_get_update_delete_note(run, deck):
    nid = add_verse(run, deck)
    note = run(svc.get_note(U, nid))
    assert note.notetype == "Bible Verse"
    assert dict(note.fields)["Reference"] == "Joh 3,16"
    assert note.deck_id == deck and not note.readonly and not note.suspended

    other = run(svc.create_deck(U, "Andere"))
    run(svc.update_note(U, nid, {"Reference": "Joh 3,17"}, other))
    note = run(svc.get_note(U, nid))
    assert dict(note.fields)["Reference"] == "Joh 3,17" and note.deck_id == other

    run(svc.delete_note(U, nid))
    with pytest.raises(svc.NotFound):
        run(svc.get_note(U, nid))


def test_add_note_empty_first_field(run, deck):
    with pytest.raises(svc.EmptyNote):
        run(svc.add_note(U, svc.BASIC, deck, {"Front": "", "Back": "x"}))


def test_add_note_unknown_notetype(run, deck):
    with pytest.raises(svc.NotFound):
        run(svc.add_note(U, "Cloze", deck, {"Text": "x"}))


def test_group_notes_are_readonly(run, deck):
    nid = add_verse(run, deck, tags=(svc.GROUP_TAG,))
    assert run(svc.get_note(U, nid)).readonly
    with pytest.raises(svc.ReadOnlyNote):
        run(svc.update_note(U, nid, {"Text": "x"}, deck))
    with pytest.raises(svc.ReadOnlyNote):
        run(svc.delete_note(U, nid))
    run(svc.update_note(U, nid, {"Text": "x"}, deck, allow_group=True))
    run(svc.set_suspended(U, nid, True))  # suspending is allowed
    assert run(svc.get_note(U, nid)).suspended


def test_suspend_and_search(run, deck):
    nid = add_verse(run, deck)
    add_verse(run, deck, ref="Ps 23,1", text="Der HERR ist mein Hirte")
    rows, total = run(svc.search_cards(U, ""))
    assert total == 2 and {r.status for r in rows} == {"new"}
    run(svc.set_suspended(U, nid, True))
    rows, total = run(svc.search_cards(U, "is:suspended"))
    assert total == 1 and rows[0].note_id == nid and rows[0].status == "suspended"
    run(svc.set_suspended(U, nid, False))
    assert run(svc.search_cards(U, "is:suspended"))[1] == 0
    rows, _ = run(svc.search_cards(U, "Hirte"))
    assert rows[0].front == "Ps 23,1" and rows[0].deck == DECK
    with pytest.raises(svc.InvalidSearch):
        run(svc.search_cards(U, "deck:("))


def test_search_notes_and_suspend_single_card(run, deck):
    nid = run(svc.add_note(U, svc.BIBLE_VERSE, deck,
                           {"Reference": "Joh 3,16", "Text": "Denn also", "Reverse": "y"}, ()))
    add_verse(run, deck, ref="Ps 23,1", text="Der HERR ist mein Hirte")
    rows, total = run(svc.search_notes(U, ""))
    assert total == 2
    row = next(r for r in rows if r.note_id == nid)
    assert (row.cards, row.suspended_cards, row.status, row.deck) == (2, 0, "new", DECK)

    cards, _total = run(svc.search_cards(U, "Joh"))
    reverse = next(c for c in cards if c.template == svc.BIBLE_VERSE_REVERSE_NAME)
    assert reverse.siblings == 2
    updated = run(svc.set_card_suspended(U, reverse.card_id, True))
    assert updated.suspended and updated.status == "suspended"
    row = run(svc.search_notes(U, "Joh"))[0][0]
    assert (row.suspended_cards, row.status) == (1, "new")  # the front card stays active
    run(svc.set_card_suspended(U, cards[0].card_id if cards[0] is not reverse else cards[1].card_id, True))
    assert run(svc.search_notes(U, "Joh"))[0][0].status == "suspended"
    assert not run(svc.set_card_suspended(U, reverse.card_id, False)).suspended
    with pytest.raises(svc.NotFound):
        run(svc.set_card_suspended(U, 999, True))
    with pytest.raises(svc.InvalidSearch):
        run(svc.search_notes(U, "deck:("))


def test_review_answer_undo(run, deck):
    for i in range(3):
        add_verse(run, deck, ref=f"Vers {i}")
    state = run(svc.review_state(U, deck))
    assert (state.new, state.learn, state.review) == (3, 0, 0)
    assert state.card is not None and len(state.card.labels) == 4
    assert "⁨" not in "".join(state.card.labels)
    assert state.card.labels[0].endswith("Min.")  # German backend
    assert "Vers" in state.card.question and "<hr id=answer>" in state.card.answer
    assert not state.can_undo

    first = state.card.card_id
    state, answered = run(svc.answer_card(U, deck, first, 3, 5.0))
    assert answered and state.can_undo and state.card.card_id != first

    # stale card id (double tap) is ignored
    _, answered = run(svc.answer_card(U, deck, first, 3, 5.0))
    assert not answered

    state, undone = run(svc.undo_review(U, deck))
    assert undone and state.card.card_id == first and state.new == 3
    state, undone = run(svc.undo_review(U, deck))
    assert not undone

    with pytest.raises(ValueError):
        run(svc.answer_card(U, deck, first, 5, 1.0))


def test_undo_does_not_revert_note_edits(run, deck):
    nid = add_verse(run, deck)
    state, undone = run(svc.undo_review(U, deck))
    assert not undone
    run(svc.get_note(U, nid))


def test_review_unknown_deck(run):
    with pytest.raises(svc.NotFound):
        run(svc.review_state(U, 999999))


def test_deck_options_are_per_deck(run, deck):
    other = run(svc.create_deck(U, "Andere"))
    assert run(svc.get_deck_options(U, deck)) == svc.DeckOptions(5, 200, 0.9, True, True)
    run(svc.set_deck_options(U, deck, svc.DeckOptions(5, 100, 0.85, False, True)))
    assert run(svc.get_deck_options(U, deck)) == svc.DeckOptions(5, 100, 0.85, False, True)
    assert run(svc.get_deck_options(U, other)) == svc.DeckOptions(5, 200, 0.9, True, True)
    run(svc.set_deck_options(U, deck, svc.DeckOptions(7, 100, 0.85)))
    assert run(svc.get_deck_options(U, deck)).new_per_day == 7


def test_bury_siblings_upgrade_runs_once(run, deck):
    def reset(col):  # an existing collection from before the upgrade
        for conf in col.decks.all_config():
            conf["new"]["bury"] = conf["rev"]["bury"] = False
            col.decks.update_config(conf)
        col.remove_config(svc.BURY_SIBLINGS_KEY)

    run(svc._run(U, reset))
    run(svc.close_all())
    opts = run(svc.get_deck_options(U, deck))  # reopens and upgrades
    assert (opts.bury_new, opts.bury_reviews) == (True, True)
    run(svc.set_deck_options(U, deck, svc.DeckOptions(5, 200, 0.9, False, False)))
    run(svc.close_all())
    opts = run(svc.get_deck_options(U, deck))  # the user's choice survives reopening
    assert (opts.bury_new, opts.bury_reviews) == (False, False)


def test_bury_new_siblings_on_different_days(run, deck):
    run(svc.add_note(U, svc.BIBLE_VERSE, deck, {"Reference": "Joh 3,16", "Text": "Denn also",
                                               "Reverse": "y"}, ()))
    assert run(svc.review_state(U, deck)).new == 1  # the reverse card waits for tomorrow
    run(svc.set_deck_options(U, deck, svc.DeckOptions(5, 200, 0.9, False, True)))
    assert run(svc.review_state(U, deck)).new == 2


def _review_all(run, deck, n):
    for _ in range(n):
        state = run(svc.review_state(U, deck))
        if state.card is None:
            break
        run(svc.answer_card(U, deck, state.card.card_id, 3, 3.0))


def test_export_roundtrip(run, deck, tmp_path):
    for i in range(5):
        add_verse(run, deck, ref=f"Vers {i}")
    _review_all(run, deck, 8)
    colpkg, apkg = tmp_path / "x.colpkg", tmp_path / "x.apkg"
    run(svc.export_collection(U, colpkg))
    assert run(svc.export_deck(U, deck, apkg)) == 5
    # collection still usable after colpkg export (it gets reopened)
    assert run(svc.review_state(U, deck)).deck_name == DECK

    (tmp_path / "fresh").mkdir()
    target = Collection(str(tmp_path / "fresh" / "collection.anki2"))
    try:
        target.import_anki_package(ImportAnkiPackageRequest(
            package_path=str(apkg),
            options=ImportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True)))
        assert target.db.scalar("select count() from revlog") == 8
        assert target.card_count() == 5
        reviewed = [target.get_card(c) for c in target.find_cards("-is:new")]
        assert reviewed and all(c.memory_state is not None for c in reviewed)
    finally:
        target.close()

    restored = tmp_path / "restored" / "collection.anki2"
    restored.parent.mkdir()
    RustBackend().import_collection_package(
        col_path=str(restored), backup_path=str(colpkg),
        media_folder=str(restored.parent / "m"), media_db=str(restored.parent / "m.db"))
    col = Collection(str(restored))
    try:
        assert col.db.scalar("select count() from revlog") == 8
        assert col.get_config("fsrs") is True
    finally:
        col.close()


def test_import_package_updates_by_guid_and_keeps_progress(run, deck, tmp_path):
    admin = svc.ADMIN_COLLECTION
    adeck = run(svc.create_deck(admin, "Gruppe"))
    nid = run(svc.add_note(admin, svc.BIBLE_VERSE, adeck,
                           {"Reference": "Ps 1,1", "Text": "alt"}, (svc.GROUP_TAG,)))
    pkg = tmp_path / "g.apkg"
    run(svc.export_deck(admin, adeck, pkg, with_scheduling=False))
    assert run(svc.import_package(U, pkg)) == 1

    gdeck = next(d.id for d in run(svc.deck_list(U)) if d.name == "Gruppe")
    _review_all(run, gdeck, 3)
    before = run(svc.collection_stats(U)).reviews_today

    # anki skips notes whose mtime (in seconds) equals the existing one
    time.sleep(1.1)
    run(svc.update_note(admin, nid, {"Text": "neu"}, adeck, allow_group=True))
    run(svc.export_deck(admin, adeck, pkg, with_scheduling=False))
    assert run(svc.import_package(U, pkg)) == 1
    rows, total = run(svc.search_cards(U, "deck:Gruppe"))
    note = run(svc.get_note(U, rows[0].note_id))
    assert total == 1 and dict(note.fields)["Text"] == "neu" and note.readonly
    assert rows[0].status == "scheduled" and rows[0].due is not None  # learning progress kept
    assert run(svc.collection_stats(U)).reviews_today == before


def test_collection_stats(run, deck):
    empty = run(svc.collection_stats(U))
    assert (empty.total_cards, empty.reviews_today, empty.streak_days) == (0, 0, 0)
    assert empty.retention_30d is None and empty.last_review_at is None
    for i in range(4):
        add_verse(run, deck, ref=f"Vers {i}")
    _review_all(run, deck, 3)
    stats = run(svc.collection_stats(U))
    assert stats.total_cards == 4
    assert stats.reviews_today == 3 and stats.streak_days == 1
    assert stats.new_today == 1  # 4 new, 3 started, daily limit 10
    assert stats.daily[0].reviews == 3 and stats.daily[0].new == 3
    assert stats.last_review_at is not None


def test_cache_eviction_and_idle_close(run, monkeypatch):
    monkeypatch.setattr(svc, "MAX_OPEN", 2)
    for i in range(4):
        run(svc.ensure_collection(f"U{i}"))
    assert svc.open_count() == 2
    assert run(svc.close_idle(max_idle=-1)) == 2
    assert svc.open_count() == 0


def test_backup_collection(run, deck, tmp_path):
    add_verse(run, deck)
    assert run(svc.backup_collection(U, tmp_path / "bk"))
    col = Collection(str(tmp_path / "bk" / "collection.anki2"))
    try:
        assert col.note_count() == 1
    finally:
        col.close()
    assert not run(svc.backup_collection("NOBODY", tmp_path / "bk2"))
    # collection reopens transparently after the backup closed it
    assert run(svc.get_note(U, run(svc.search_cards(U, ""))[0][0].note_id))


def test_notetypes_have_shared_seed_ids(run):
    run(svc.ensure_collection(U))
    run(svc.ensure_collection("OTHER"))
    ids = lambda col: {m["name"]: m["id"] for m in col.models.all()}  # noqa: E731
    for uid in (U, "OTHER"):
        got = run(svc._run(uid, ids))
        assert got["Basic"] == svc.SEED_IDS["Basic"]
        assert got["Bible Verse"] == svc.SEED_IDS["Bible Verse"]
    names = run(svc.deck_names(U))
    assert svc.SEED_DECK not in dict(names).values()
    assert run(svc.search_cards(U, ""))[1] == 0


def test_group_deck_import_does_not_duplicate_notetypes(run, tmp_path):
    adeck = run(svc.create_deck(svc.ADMIN_COLLECTION, "Gruppe"))
    run(svc.add_note(svc.ADMIN_COLLECTION, svc.BIBLE_VERSE, adeck, {"Reference": "Ps 1", "Text": "x"}))
    run(svc.add_note(svc.ADMIN_COLLECTION, svc.BASIC, adeck, {"Front": "F", "Back": "B"}))
    pkg = tmp_path / "g.apkg"
    run(svc.export_deck(svc.ADMIN_COLLECTION, adeck, pkg, with_scheduling=False))
    run(svc.import_package(U, pkg))
    names = run(svc._run(U, lambda col: [m["name"] for m in col.models.all()]))
    assert not [n for n in names if n.endswith("+")], names


def test_seed_package_matches_constants(tmp_path):
    # rebuilding from the committed seed keeps the ids
    assert svc.build_seed_package(tmp_path / "seed.apkg") == svc.SEED_IDS
    (tmp_path / "c").mkdir()
    col = Collection(str(tmp_path / "c" / "collection.anki2"))
    try:
        col.import_anki_package(ImportAnkiPackageRequest(
            package_path=str(svc.SEED_PATH), options=ImportAnkiPackageOptions()))
        got = {m["name"]: m["id"] for m in col.models.all()}
        assert got["Bible Verse"] == svc.SEED_IDS["Bible Verse"]
        assert svc.SEED_IDS["Basic"] in got.values()
    finally:
        col.close()


def test_existing_unused_notetype_is_replaced(run, data_dir):
    path = svc.collection_path("OLD")
    path.parent.mkdir(parents=True)
    col = Collection(str(path))  # Phase-1 style collection: random notetype ids
    svc._add_bible_verse_notetype(col)
    col.close()
    run(svc.ensure_collection("OLD"))
    got = run(svc._run("OLD", lambda c: {m["name"]: m["id"] for m in c.models.all()}))
    assert got["Basic"] == svc.SEED_IDS["Basic"] and got["Bible Verse"] == svc.SEED_IDS["Bible Verse"]


PSALM = """1 Der HERR ist mein Hirte, mir wird nichts mangeln.
2 Er weidet mich auf einer grünen Aue
und führet mich zum frischen Wasser.
3 Er erquicket meine Seele.
4 Und ob ich schon wanderte im finstern Tal,
5 Du bereitest vor mir einen Tisch"""


def test_add_bible_text(run, deck):
    notes, cards = run(svc.add_bible_text(U, deck, "Psalm 23", 2, PSALM, table=True))
    assert notes == 6
    assert cards == 6 * 4  # last letters, first letters, table (5 verses > 4), context
    rows, total = run(svc.search_cards(U, '"note:Bible Memorizer"'))
    assert total == 24
    note = run(svc.get_note(U, rows[0].note_id))
    f = dict(note.fields)
    assert note.generated and note.notetype == "Bible Memorizer"
    assert f["title"] == "Psalm 23" and f["first_letters"] and "<table" in f["table"]
    fronts = [dict(run(svc.get_note(U, r.note_id)).fields)["front"] for r in rows]
    assert "[Anfang]" in fronts and "[Beginning]" not in fronts
    with pytest.raises(svc.ReadOnlyNote):
        run(svc.update_note(U, note.id, {"title": "x"}, deck))


def test_add_bible_text_without_table_and_short(run, deck):
    notes, cards = run(svc.add_bible_text(U, deck, "Kurz", 1, "1 Jesus weinte.", table=True))
    assert (notes, cards) == (1, 3)  # no table for <= 4 verses
    with pytest.raises(svc.EmptyNote):
        run(svc.add_bible_text(U, deck, "Leer", 2, "  \n ", table=False))


def test_bible_memorizer_notetype_matches_addon(run):
    from app.bible_memorizer import model as bm
    m = run(svc._run(U, lambda col: col.models.by_name("Bible Memorizer")))
    assert m["id"] == svc.SEED_IDS["Bible Memorizer"]
    assert [f["name"] for f in m["flds"]] == bm.FIELD_NAMES
    assert [(t["name"], t["qfmt"], t["afmt"]) for t in m["tmpls"]] == [tuple(t) for t in bm.TEMPLATES]
    assert m["css"] == bm.CSS and m["sortf"] == bm.FIELD_NAMES.index("id")


def test_tag_deck_notes(run, deck):
    sub = run(svc.create_deck(U, f"{DECK}::Unter"))
    other = run(svc.create_deck(U, "Andere"))
    add_verse(run, deck)
    add_verse(run, sub, ref="Sub")
    add_verse(run, other, ref="Nicht")
    assert run(svc.tag_deck_notes(U, deck)) == 2  # incl. subdeck
    assert run(svc.tag_deck_notes(U, deck)) == 0  # idempotent
    assert run(svc.search_cards(U, f"tag:{svc.GROUP_TAG}"))[1] == 2


def test_delete_collection(run, deck):
    add_verse(run, deck)
    run(svc.delete_collection(U))
    assert not svc.collection_path(U).parent.exists() and svc.open_count() == 0
    assert run(svc.search_cards(U, ""))[1] == 0  # a fresh one is created on next access


FSRS6_DEFAULTS = [0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666,
                  0.796, 1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658,
                  0.1542]


def test_optimize_fsrs_without_enough_data(run, deck):
    add_verse(run, deck)
    _review_all(run, deck, 1)
    assert run(svc.review_count(U, deck)) == 1
    result = run(svc.optimize_fsrs(U, deck))
    assert result.changed is False


def test_apply_fsrs_params_recomputes_memory_state_for_this_deck_only(run, deck):
    other = run(svc.create_deck(U, "Andere"))
    add_verse(run, deck)
    _review_all(run, deck, 1)

    def memory(col):
        cid = col.find_cards("-is:new")[0]
        return col.get_card(cid).memory_state.stability

    before = run(svc._run(U, memory))
    params = list(FSRS6_DEFAULTS)
    params[2] = 9.0  # initial stability for "Good"
    run(svc._run(U, svc._apply_fsrs_params, deck, params))
    after = run(svc._run(U, memory))
    assert after != before and abs(after - 9.0) < 0.5
    confs = run(svc._run(U, lambda col: (col.decks.config_dict_for_deck_id(deck),
                                          col.decks.config_dict_for_deck_id(other))))
    assert confs[0]["fsrsParams6"][2] == pytest.approx(9.0)
    assert confs[0]["id"] != confs[1]["id"] and not confs[1].get("fsrsParams6")


def test_learner_stats(run, deck):
    empty = run(svc.learner_stats(U))
    assert len(empty.heatmap) == 365 and len(empty.forecast) == 30
    assert empty.total_reviews == 0 and empty.retention_month is None
    for i in range(4):
        add_verse(run, deck, ref=f"Vers {i}")
    _review_all(run, deck, 3)
    stats = run(svc.learner_stats(U, deck))
    assert stats.heatmap[-1][1] == 3 and stats.total_reviews == 3 and stats.streak_days == 1
    assert stats.today_answered == 3 and stats.new == 1
    assert sum(n for _, n in stats.forecast) >= 3  # all reviewed cards come back
    assert stats.heatmap[-1][0] == stats.forecast[0][0]  # both anchored at "today"


def test_bible_verse_optional_reverse_card(run, deck):
    nts = run(svc.notetypes_for_adding(U))
    assert nts[0].fields == ["Reference", "Text", "Translation", "Reverse"]
    one = run(svc.add_note(U, svc.BIBLE_VERSE, deck, {"Reference": "Ps 1", "Text": "Wohl dem"}))
    two = run(svc.add_note(U, svc.BIBLE_VERSE, deck,
                           {"Reference": "Ps 2", "Text": "Warum toben", "Reverse": "y"}))
    count = lambda nid: run(svc._run(U, lambda col: len(col.get_note(nid).card_ids())))  # noqa: E731
    assert count(one) == 1 and count(two) == 2
    rows, _ = run(svc.search_cards(U, '"card:Text → Reference"'))
    card = run(svc._run(U, lambda col: col.get_card(rows[0].card_id)))
    q = run(svc._run(U, lambda col: card.question()))
    assert "Warum toben" in q and "Ps 2" not in q


def test_upgrade_adds_reverse_to_existing_bible_verse(run, data_dir):
    # a collection whose Bible Verse notetype predates the reverse card
    run(svc.ensure_collection(U))
    deck = run(svc.current_deck_id(U))
    nid = run(svc.add_note(U, svc.BIBLE_VERSE, deck, {"Reference": "Joh 1", "Text": "Im Anfang"}))

    def strip(col):
        mm = col.models
        m = mm.get(svc.SEED_IDS[svc.BIBLE_VERSE])
        mm.remove_template(m, m["tmpls"][1])
        mm.remove_field(m, m["flds"][3])
        mm.save(m)
    run(svc._run(U, strip))
    run(svc.close_all())
    names = run(svc._run(U, lambda col: col.models.field_names(col.models.get(svc.SEED_IDS[svc.BIBLE_VERSE]))))
    assert names[-1] == "Reverse"
    assert dict(run(svc.get_note(U, nid)).fields)["Text"] == "Im Anfang"


def test_compare_typed(run, deck):
    add_verse(run, deck, ref="Joh 3,16", text="Denn also hat <b>Gott</b> die Welt geliebt")
    state = run(svc.review_state(U, deck))
    assert state.card.can_type
    _, diff = run(svc.compare_typed(U, deck, state.card.card_id, "Denn also hat Gott die Welt geliebt"))
    assert "typeBad" not in diff and "typeMissed" not in diff  # markup of the field is ignored
    _, diff = run(svc.compare_typed(U, deck, 12345, "x"))
    assert diff is None  # not the current card


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
       b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
       b"\x00\x00IEND\xaeB`\x82")


def test_media_add_export_backup(run, deck, tmp_path):
    name = run(svc.add_media(U, PNG))
    assert name.endswith(".png") and svc.media_file(U, name).read_bytes() == PNG
    assert run(svc.add_media(U, PNG)) == name  # same content, same file
    assert svc.media_file(U, "../collection.anki2") is None
    with pytest.raises(svc.AnkiServiceError):
        run(svc.add_media(U, b"<svg></svg>"))
    run(svc.add_note(U, svc.BASIC, deck, {"Front": "Wer?", "Back": f'Mose<br><img src="{name}">'}))

    import zipfile
    pkg = tmp_path / "x.apkg"
    run(svc.export_deck(U, deck, pkg))
    with zipfile.ZipFile(pkg) as z:
        assert len(z.namelist()) > 2  # collection, media map, media file
    run(svc.import_package("OTHER", pkg))
    assert svc.media_file("OTHER", name) is not None

    assert run(svc.backup_collection(U, tmp_path / "bk"))
    assert (tmp_path / "bk" / "collection.media" / name).exists()

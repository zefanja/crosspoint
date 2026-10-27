"""Every anki_service function against a real temporary collection."""

import time

import pytest
from anki._backend import RustBackend
from anki.collection import Collection, ImportAnkiPackageOptions, ImportAnkiPackageRequest

from app import anki_service as svc

U = "01TESTUSER0000000000000000"


def add_verse(run, deck_id, ref="Joh 3,16", text="Denn also hat Gott die Welt geliebt", tags=()):
    return run(svc.add_note(U, svc.BIBELVERS, deck_id,
                            {"Referenz": ref, "Text": text, "Übersetzung": "LUT17"}, tags))


@pytest.fixture
def deck(run):
    decks = run(svc.deck_list(U))
    return next(d.id for d in decks if d.name == svc.DEFAULT_DECK)


def test_new_collection_setup(run, data_dir):
    run(svc.ensure_collection(U))
    assert svc.collection_path(U).exists()
    assert svc.collection_ids() == [U]
    run(svc.close_all())
    col = Collection(str(svc.collection_path(U)))
    try:
        assert col.get_config("fsrs") is True
        assert col.get_preferences().scheduling.rollover == 4
        assert col.models.by_name("Bibelvers") is not None
        assert col.models.by_name("Basic") is not None
        assert col.decks.by_name(svc.DEFAULT_DECK) is not None
        conf = col.decks.config_dict_for_deck_id(1)
        assert conf["new"]["perDay"] == 10
    finally:
        col.close()


def test_invalid_user_id_rejected():
    with pytest.raises(ValueError):
        svc.collection_path("../etc")


def test_deck_list_hides_empty_default(run):
    names = [d.name for d in run(svc.deck_list(U))]
    assert names == [svc.DEFAULT_DECK]


def test_create_deck_and_names(run, deck):
    did = run(svc.create_deck(U, " Bibel :: Psalmen "))
    names = dict(run(svc.deck_names(U)))
    assert names[did] == "Bibel::Psalmen"
    tree = run(svc.deck_list(U))
    child = next(d for d in tree if d.id == did)
    assert (child.short_name, child.depth) == ("Psalmen", 1)
    with pytest.raises(svc.AnkiServiceError):
        run(svc.create_deck(U, " :: "))


def test_notetypes_for_adding(run):
    nts = run(svc.notetypes_for_adding(U))
    assert [n.name for n in nts] == ["Bibelvers", "Basic"]
    assert nts[0].fields == ["Referenz", "Text", "Übersetzung"]


def test_add_get_update_delete_note(run, deck):
    nid = add_verse(run, deck)
    note = run(svc.get_note(U, nid))
    assert note.notetype == "Bibelvers"
    assert dict(note.fields)["Referenz"] == "Joh 3,16"
    assert note.deck_id == deck and not note.readonly and not note.suspended

    other = run(svc.create_deck(U, "Andere"))
    run(svc.update_note(U, nid, {"Referenz": "Joh 3,17"}, other))
    note = run(svc.get_note(U, nid))
    assert dict(note.fields)["Referenz"] == "Joh 3,17" and note.deck_id == other

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
    assert total == 2 and {r.due for r in rows} == {"neu"}
    run(svc.set_suspended(U, nid, True))
    rows, total = run(svc.search_cards(U, "is:suspended"))
    assert total == 1 and rows[0].note_id == nid and rows[0].due == "ausgesetzt"
    run(svc.set_suspended(U, nid, False))
    assert run(svc.search_cards(U, "is:suspended"))[1] == 0
    rows, _ = run(svc.search_cards(U, "Hirte"))
    assert rows[0].front == "Ps 23,1" and rows[0].deck == svc.DEFAULT_DECK
    with pytest.raises(svc.InvalidSearch):
        run(svc.search_cards(U, "deck:("))


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
    assert run(svc.get_deck_options(U, deck)) == svc.DeckOptions(10, 200, 0.9)
    run(svc.set_deck_options(U, deck, svc.DeckOptions(5, 100, 0.85)))
    assert run(svc.get_deck_options(U, deck)) == svc.DeckOptions(5, 100, 0.85)
    assert run(svc.get_deck_options(U, other)) == svc.DeckOptions(10, 200, 0.9)
    run(svc.set_deck_options(U, deck, svc.DeckOptions(7, 100, 0.85)))
    assert run(svc.get_deck_options(U, deck)).new_per_day == 7


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
    assert run(svc.review_state(U, deck)).deck_name == svc.DEFAULT_DECK

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
    nid = run(svc.add_note(admin, svc.BIBELVERS, adeck,
                           {"Referenz": "Ps 1,1", "Text": "alt"}, (svc.GROUP_TAG,)))
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
    assert rows[0].due != "neu"  # learning progress kept
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

"""Phase 0b spike: verify the `anki` library works for our use case.

Run inside the built image:
    docker run --rm <image> python spike/check_anki.py
Keep the export files for the manual Anki Desktop / AnkiDroid import test:
    docker run --rm -v "$PWD/spike/out:/out" <image> python spike/check_anki.py --out /out

Exits non-zero on the first failed check. Standalone on purpose: the app itself only
talks to anki through app/anki_service.py.
"""

import argparse
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from anki._backend import RustBackend
from anki.collection import (
    Collection,
    DeckIdLimit,
    ExportAnkiPackageOptions,
    ImportAnkiPackageOptions,
    ImportAnkiPackageRequest,
)
from anki.buildinfo import version as anki_version
from anki.scheduler.v3 import CardAnswer

VERSES = [
    ("Joh 3,16", "Denn also hat Gott die Welt geliebt, dass er seinen eingeborenen Sohn gab."),
    ("Ps 23,1", "Der HERR ist mein Hirte, mir wird nichts mangeln."),
    ("Röm 8,28", "Wir wissen aber, dass denen, die Gott lieben, alle Dinge zum Besten dienen."),
    ("Phil 4,13", "Ich vermag alles durch den, der mich mächtig macht, Christus."),
    ("Jes 41,10", "Fürchte dich nicht, ich bin mit dir; weiche nicht, denn ich bin dein Gott."),
    ("Spr 3,5", "Verlass dich auf den HERRN von ganzem Herzen."),
    ("Mt 11,28", "Kommt her zu mir, alle, die ihr mühselig und beladen seid."),
    ("Jos 1,9", "Sei getrost und unverzagt."),
    ("1Kor 13,13", "Nun aber bleiben Glaube, Hoffnung, Liebe, diese drei."),
    ("Hebr 11,1", "Es ist aber der Glaube eine feste Zuversicht dessen, was man hofft."),
]

BIBLE_VERSE_CSS = """\
.card {
  font-family: system-ui, sans-serif;
  font-size: 24px;
  line-height: 1.5;
  text-align: center;
  color: #1d1d1f;
  background: #ffffff;
}
.card.nightMode, .nightMode .card { color: #ececef; background: #1f1f23; }
.translation { font-size: 14px; opacity: 0.6; }
"""

LABEL_MARKS = str.maketrans("", "", "⁨⁩")
failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        failures += 1
        raise SystemExit(f"\nSpike failed at: {msg}")


def section(title: str) -> None:
    print(f"\n== {title}")


def add_bibelvers_notetype(col: Collection) -> dict:
    mm = col.models
    m = mm.new("Bible Verse")
    for name in ("Reference", "Text", "Translation"):
        mm.add_field(m, mm.new_field(name))
    t = mm.new_template("Reference → Text")
    t["qfmt"] = (
        "{{Reference}}"
        "{{#Translation}}<div class=translation>{{Translation}}</div>{{/Translation}}"
    )
    t["afmt"] = "{{FrontSide}}\n\n<hr id=answer>\n\n{{Text}}"
    mm.add_template(m, t)
    m["css"] = BIBLE_VERSE_CSS
    mm.add(m)
    return mm.by_name("Bible Verse")


def open_col(path: Path) -> Collection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return Collection(str(path))


def setup_collection(path: Path) -> tuple[Collection, int]:
    col = open_col(path)
    col.set_config("fsrs", True)
    prefs = col.get_preferences()
    prefs.scheduling.rollover = 4
    col.set_preferences(prefs)

    did = col.decks.id("Meine Karten")
    conf = col.decks.config_dict_for_deck_id(did)
    conf["new"]["perDay"] = 50
    conf["rev"]["perDay"] = 200
    conf["desiredRetention"] = 0.9
    col.decks.update_config(conf)
    return col, did


def answer_one(col: Collection, rating) -> tuple[int, list[str]] | None:
    queued = col.sched.get_queued_cards(fetch_limit=1)
    if not queued.cards:
        return None
    qc = queued.cards[0]
    card = col.get_card(qc.card.id)
    card.start_timer()
    labels = [s.translate(LABEL_MARKS) for s in col.sched.describe_next_states(qc.states)]
    col.sched.answer_card(col.sched.build_answer(card=card, states=qc.states, rating=rating))
    return card.id, labels


def revlog_count(col: Collection) -> int:
    return col.db.scalar("select count() from revlog")


def fsrs_cards(col: Collection) -> int:
    return sum(
        1 for cid in col.find_cards("-is:new") if col.get_card(cid).memory_state is not None
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="keep export files in this directory")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="anki-spike-"))
    out = Path(args.out) if args.out else work / "out"
    out.mkdir(parents=True, exist_ok=True)

    section("1. Environment")
    print(f"  anki version: {anki_version}")
    print(f"  python: {sys.version.split()[0]}  TZ={os.environ.get('TZ')}  "
          f"local tz={time.tzname}")

    section("2. Collection, FSRS, notetype 'Bible Verse', 20 notes")
    col, did = setup_collection(work / "user" / "collection.anki2")
    check(col.get_config("fsrs") is True, "FSRS enabled globally")
    check(col.get_preferences().scheduling.rollover == 4, "day starts at 4:00")
    m = add_bibelvers_notetype(col)
    check(m is not None and [f["name"] for f in m["flds"]] == ["Reference", "Text", "Translation"],
          "notetype 'Bible Verse' with fields Reference/Text/Translation")
    check(col.models.by_name("Basic") is not None, "stock notetype 'Basic' present")
    for i in range(20):
        ref, text = VERSES[i % len(VERSES)]
        note = col.new_note(m)
        note["Reference"] = f"{ref} ({i + 1})"
        note["Text"] = text
        note["Translation"] = "LUT17"
        col.add_note(note, did)
    check(len(col.find_notes(f"did:{did}")) == 20, "20 notes in deck 'Meine Karten'")
    card = col.get_card(col.find_cards(f"did:{did}")[0])
    check("Joh 3,16" in card.question() and "LUT17" in card.question(), "question renders")
    check("<hr id=answer>" in card.answer() and "geliebt" in card.answer(), "answer renders")

    section("3. 50 reviews via v3 scheduler, interval preview, undo")
    col.decks.select(did)  # the v3 queue is scoped to the current deck
    rng = random.Random(42)
    ratings = [CardAnswer.AGAIN, CardAnswer.HARD, CardAnswer.GOOD, CardAnswer.EASY]
    first = answer_one(col, CardAnswer.GOOD)
    check(first is not None and len(first[1]) == 4, f"4 interval labels: {first and first[1]}")
    done = 1
    while done < 50:
        res = answer_one(col, rng.choices(ratings, weights=[1, 1, 5, 2])[0])
        if res is None:
            break
        done += 1
    check(done == 50, f"50 reviews answered (got {done})")
    check(revlog_count(col) == 50, "50 revlog entries")

    # After 50 answers all 20 cards may have graduated (learning steps get a random delay
    # that the seed above does not control), so add a fresh card for the undo test.
    note = col.new_note(m)
    note["Reference"], note["Text"] = VERSES[0]
    col.add_note(note, did)
    before_counts = col.sched.counts()
    res = answer_one(col, CardAnswer.GOOD)
    check(res is not None, "51st card answered for undo test")
    status = col.undo_status()
    check(bool(status.undo), f"undo available: {status.undo!r}")
    col.undo()
    check(revlog_count(col) == 50, "undo removed the revlog entry")
    check(col.sched.counts() == before_counts, f"undo restored counts {before_counts}")
    check(fsrs_cards(col) > 0, f"FSRS memory state on {fsrs_cards(col)} reviewed cards")
    stats = (revlog_count(col), fsrs_cards(col))

    section("4. Export .colpkg and .apkg (with scheduling)")
    apkg = out / "spike-deck.apkg"
    colpkg = out / "spike-collection.colpkg"
    n = col.export_anki_package(
        out_path=str(apkg),
        options=ExportAnkiPackageOptions(
            with_scheduling=True, with_deck_configs=True, with_media=False, legacy=False
        ),
        limit=DeckIdLimit(deck_id=did),
    )
    check(apkg.stat().st_size > 0 and n == 21, f".apkg written ({n} notes)")
    col.export_collection_package(str(colpkg), include_media=False, legacy=False)
    check(colpkg.stat().st_size > 0, ".colpkg written")
    col.close()

    section("5. Re-open exports with anki")
    target = work / "from-colpkg" / "collection.anki2"
    target.parent.mkdir(parents=True)
    RustBackend().import_collection_package(
        col_path=str(target),
        backup_path=str(colpkg),
        media_folder=str(target.parent / "collection.media"),
        media_db=str(target.parent / "collection.media.db2"),
    )
    col2 = open_col(target)
    check((revlog_count(col2), fsrs_cards(col2)) == stats,
          f".colpkg: revlog={revlog_count(col2)}, FSRS cards={fsrs_cards(col2)}")
    check(col2.get_config("fsrs") is True, ".colpkg: FSRS still enabled")
    col2.close()

    col3 = open_col(work / "from-apkg" / "collection.anki2")
    col3.import_anki_package(
        ImportAnkiPackageRequest(
            package_path=str(apkg),
            options=ImportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True),
        )
    )
    check((revlog_count(col3), fsrs_cards(col3)) == stats,
          f".apkg into fresh collection: revlog={revlog_count(col3)}, FSRS cards={fsrs_cards(col3)}")
    check(col3.decks.by_name("Meine Karten") is not None, ".apkg: deck 'Meine Karten' present")
    col3.close()

    section("7. Day rollover / time zone")
    col4 = open_col(target)
    cutoff = datetime.fromtimestamp(col4.sched.day_cutoff)
    now = datetime.now()
    print(f"  now (local): {now:%Y-%m-%d %H:%M}  next day starts: {cutoff:%Y-%m-%d %H:%M}")
    check((cutoff.hour, cutoff.minute) == (4, 0), "next day starts at 04:00 local time")
    check(timedelta(0) < cutoff - now <= timedelta(hours=25), "cutoff is within the next 25 h")
    col4.close()

    print(f"\nAll checks passed. Exports: {out}")
    print("Manual step 6: import the .colpkg into Anki Desktop and AnkiDroid and compare "
          "due dates/intervals.")
    if not args.out:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

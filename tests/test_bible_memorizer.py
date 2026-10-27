"""Ported from app/test_logic.mjs of zefanja/bible-memorizer."""

import pytest

from app.bible_memorizer.formatter import auto_format_verses as fmt, parse_for_preview
from app.bible_memorizer.gen_notes import (
    get_verses,
    keep_first_letter_only,
    process_lines,
    replace_last_letter_with_underscore,
)

JAK = """1 Meine Brüder, verbindet den Glauben an unseren Herrn Jesus Christus,
[den Herrn] der Herrlichkeit, nicht mit Ansehen der Person!
2 Denn wenn in eure Versammlung ein Mann käme
mit goldenen Ringen und in prächtiger Kleidung,"""


def test_letters():
    s = "Meine Brüder, verbindet den Glauben an unseren Herrn Jesus Christus,"
    assert keep_first_letter_only(s) == "M B, v d G a u H J C,"
    assert replace_last_letter_with_underscore(s) == \
        "Mein_ Brüde_, verbinde_ de_ Glaube_ a_ unsere_ Herr_ Jesu_ Christu_,"


def test_process_lines():
    lines = process_lines(JAK)
    assert len(lines) == 4
    assert [(d["verse_number"], d["verse_part"]) for d in lines] == \
        [("1", "a"), ("1", "b"), ("2", "a"), ("2", "b")]
    assert lines[0]["answer"].startswith("1 Meine Brüder")
    assert lines[1]["answer"] == "[den Herrn] der Herrlichkeit, nicht mit Ansehen der Person!"


def test_get_verses_context():
    data = process_lines(JAK)
    front, back = get_verses(data, "1", "a", 2)
    assert front == "[Beginning]" and "[den Herrn]" in back
    front, _ = get_verses(data, "1", "b", 2)
    assert "1 Meine Brüder" in front


@pytest.mark.parametrize("raw, want", [
    ("1 Foo bar.\n2 Baz qux.", "1 Foo bar.\n2 Baz qux."),
    ("1 Foo bar. 2 Baz qux. 3 Quux quux.", "1 Foo bar.\n2 Baz qux.\n3 Quux quux."),
    ("¹ Foo bar. ² Baz qux.", "1 Foo bar.\n2 Baz qux."),
    ("Vers eins.²Baz qux.", "Vers eins.\n2 Baz qux."),
    ("Es gab 5 Brote und 2 Fische.", "Es gab 5 Brote und 2 Fische."),
    ("Wie in Galater 1:3 steht: Gnade sei mit euch.", "Wie in Galater 1:3 steht: Gnade sei mit euch."),
    ("1 Im Anfang.", "1 Im Anfang."),
    ("", ""),
    ("1 Foo[1] bar. 2 Baz[2] qux.", "1 Foo[1] bar.\n2 Baz[2] qux."),
    ("1 Foo bar.« 2 Baz qux.", "1 Foo bar.«\n2 Baz qux."),
    ("1 Foo bar. – 2 Baz qux.", "1 Foo bar. –\n2 Baz qux."),
    ("1 Es gab 5 Brote. 2 Sie aßen.", "1 Es gab 5 Brote.\n2 Sie aßen."),
    ("1 Wie in Galater 1:3 steht. 2 Lobet den Herrn.", "1 Wie in Galater 1:3 steht.\n2 Lobet den Herrn."),
    ("9 Letzter einstelliger. 10 Zehnter. 11 Elfter.", "9 Letzter einstelliger.\n10 Zehnter.\n11 Elfter."),
    ("1 Foo bar. 12 Baz qux.", "1 Foo bar. 12 Baz qux."),
])
def test_formatter(raw, want):
    assert fmt(raw) == want


def test_formatter_idempotent():
    assert fmt(fmt("1 Foo bar. 2 Baz qux.")) == "1 Foo bar.\n2 Baz qux."


def test_nikodemus():
    raw = ("1 Einer der führenden Männer des jüdischen Volkes, ein Pharisäer namens Nikodemus, "
           "2 suchte Jesus einmal bei Nacht auf. »Rabbi«, sagte er zu ihm, »wir wissen, dass du ein "
           "Lehrer bist, den Gott gesandt hat. Denn niemand kann solche Wunder tun wie du, wenn Gott "
           "nicht mit ihm ist.« 3 Jesus entgegnete: »Ich sage dir: Wenn jemand nicht von neuem[1] "
           "geboren wird, kann er das Reich Gottes nicht sehen.« – 4 »Wie kann ein Mensch, wenn er alt "
           "geworden ist, noch einmal geboren werden?«, wandte Nikodemus ein. »Er kann doch nicht in den "
           "Leib seiner Mutter zurückkehren und ein zweites Mal auf die Welt kommen!« 5 Jesus erwiderte: "
           "»Ich sage dir eins: Wenn jemand nicht aus Wasser und Geist geboren wird, kann er nicht ins "
           "Reich Gottes hineinkommen. 6 Natürliches Leben bringt natürliches Leben hervor; geistliches "
           "Leben wird aus dem Geist geboren.[2] 7 Darum sei nicht erstaunt, wenn ich dir sage: Ihr müsst "
           "von neuem geboren werden. 8 Der Wind weht, wo er will. Du hörst zwar sein Rauschen[3], aber "
           "woher er kommt und wohin er geht, weißt du nicht. So ist es bei jedem, der aus dem Geist "
           "geboren ist.« 9 »Aber wie kann das geschehen?«, fragte Nikodemus.")
    lines = fmt(raw).split("\n")
    assert len(lines) == 9
    assert lines[0].startswith("1 Einer der führenden") and lines[0].endswith("Nikodemus,")
    assert lines[1].startswith("2 suchte Jesus")
    assert "neuem[1] geboren" in lines[2]
    assert lines[3].startswith("4 »Wie kann")
    assert "geboren.[2]" in lines[5]
    assert lines[6].startswith("7 Darum sei nicht")
    assert "Rauschen[3]" in lines[7]
    assert lines[8].startswith("9 »Aber wie")


def test_parse_for_preview():
    rows = parse_for_preview("1 Foo bar.\nzweite Zeile.\n2 Baz.")
    assert [(r["verse"], r["part"], r["continuation"]) for r in rows] == \
        [("1", "a", False), ("1", "b", True), ("2", "a", False)]

from app.sanitize import field_from_input, field_to_input, plain_text


def test_script_does_not_survive():
    out = field_from_input('<script>alert(1)</script><img src=x onerror=alert(1)>Hallo')
    assert "<script" not in out and "onerror" not in out and "<img" not in out
    assert out.endswith("Hallo")


def test_allowed_tags_kept_attributes_dropped():
    out = field_from_input('<b onclick="x()">fett</b> <i>k</i> <u>u</u> <a href="x">l</a>')
    assert out == "<b>fett</b> <i>k</i> <u>u</u> l"


def test_newlines_roundtrip():
    out = field_from_input("Zeile 1\r\nZeile 2\n")
    assert out == "Zeile 1<br>Zeile 2"
    assert field_to_input(out) == "Zeile 1\nZeile 2"


def test_plain_text():
    assert plain_text("<b>Joh</b><br>3,16 &amp; mehr") == "Joh 3,16 & mehr"

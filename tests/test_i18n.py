"""Translation catalogs: complete, valid placeholders, and up to date with the code."""

import re
from pathlib import Path

import pytest
from babel.messages.frontend import CommandLineInterface
from babel.messages.pofile import read_po

from app import i18n

LOCALES = Path(i18n.LOCALES_DIR)
PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def catalogs():
    return sorted(LOCALES.glob("*/LC_MESSAGES/messages.po"))


@pytest.mark.parametrize("po", catalogs(), ids=lambda p: p.parent.parent.name)
def test_catalog_complete_and_placeholders_match(po):
    with po.open("rb") as f:
        cat = read_po(f)
    for msg in cat:
        if not msg.id:
            continue
        ids = msg.id if isinstance(msg.id, tuple) else (msg.id,)
        strings = msg.string if isinstance(msg.string, tuple) else (msg.string,)
        assert all(strings), f"untranslated: {msg.id!r}"
        assert "fuzzy" not in msg.flags, f"fuzzy: {msg.id!r}"
        want = set(PLACEHOLDER.findall(ids[-1]))
        for s in strings:
            assert set(PLACEHOLDER.findall(s)) <= want | {"num"}, f"placeholders: {msg.id!r}"


def test_catalogs_cover_all_strings(tmp_path):
    """Fails if code/templates contain strings missing from the catalogs:
    run `pybabel extract` + `pybabel update` (docs/translations.md)."""
    pot = tmp_path / "messages.pot"
    CommandLineInterface().run(
        ["pybabel", "-q", "extract", "-F", "babel.cfg", "-k", "N_", "-o", str(pot), "app"])
    with pot.open("rb") as f:
        wanted = {m.id for m in read_po(f) if m.id}
    for po in catalogs():
        with po.open("rb") as f:
            have = {m.id for m in read_po(f) if m.id}
        assert not wanted - have, f"{po}: missing {sorted(map(str, wanted - have))[:5]}"


def test_negotiate(data_dir):
    assert i18n.negotiate("en", "de") == "en"
    assert i18n.negotiate(None, "de-AT,en;q=0.5") == "de"
    assert i18n.negotiate("xx", "fr") == "de"  # DEFAULT_LANGUAGE=de in tests
    assert set(i18n.available_languages()) >= {"en", "de"}

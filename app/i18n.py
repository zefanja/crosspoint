"""Translations (gettext via Babel). English is the source language.

Catalogs: app/locales/<lang>/LC_MESSAGES/messages.po. They are compiled in memory at
startup, so no .mo build step is needed. Workflow (see docs/translations.md):

    pybabel extract -F babel.cfg -o app/locales/messages.pot app
    pybabel update -i app/locales/messages.pot -d app/locales
    pybabel init -i app/locales/messages.pot -d app/locales -l fr   # new language

The current language lives in a context variable set per request (middleware).
"""

import io
from contextvars import ContextVar
from datetime import date
from functools import lru_cache
from pathlib import Path

from babel import Locale, UnknownLocaleError
from babel.dates import format_date as _babel_format_date
from babel.dates import format_skeleton as _babel_format_skeleton
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.numbers import format_decimal as _babel_format_decimal
from babel.support import NullTranslations, Translations

from app.config import get_settings

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
SOURCE_LANGUAGE = "en"
LANG_COOKIE = "vf_lang"

_current: ContextVar[str | None] = ContextVar("language", default=None)


@lru_cache
def _catalogs() -> dict[str, NullTranslations]:
    catalogs: dict[str, NullTranslations] = {SOURCE_LANGUAGE: NullTranslations()}
    for po in sorted(LOCALES_DIR.glob("*/LC_MESSAGES/messages.po")):
        with po.open("rb") as f:
            catalog = read_po(f)
        buf = io.BytesIO()
        write_mo(buf, catalog, use_fuzzy=False)
        buf.seek(0)
        catalogs[po.parent.parent.name] = Translations(fp=buf)
    return catalogs


def available_languages() -> dict[str, str]:
    """{code: native name}, e.g. {"en": "English", "de": "Deutsch"}."""
    result = {}
    for code in _catalogs():
        try:
            result[code] = Locale.parse(code).get_display_name(code).capitalize()
        except (ValueError, UnknownLocaleError):
            result[code] = code
    return result


def default_language() -> str:
    lang = get_settings().default_language
    return lang if lang in _catalogs() else SOURCE_LANGUAGE


def get_language() -> str:
    return _current.get() or default_language()


def set_language(lang: str | None):
    """Set the language for the current context. Returns a token for reset()."""
    return _current.set(lang if lang in _catalogs() else None)


def negotiate(cookie: str | None, accept_language: str | None) -> str:
    """Cookie → Accept-Language → DEFAULT_LANGUAGE."""
    catalogs = _catalogs()
    if cookie in catalogs:
        return cookie
    for part in (accept_language or "").split(","):
        tag = part.split(";")[0].strip().replace("-", "_")
        for candidate in (tag, tag.split("_")[0]):
            if candidate in catalogs:
                return candidate
    return default_language()


def translate(message: str, lang: str) -> str:
    """Translate into an explicit language (outside the request context)."""
    catalogs = _catalogs()
    return catalogs.get(lang, catalogs[SOURCE_LANGUAGE]).gettext(message)


def gettext(message: str) -> str:
    return _catalogs()[get_language()].gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _catalogs()[get_language()].ngettext(singular, plural, n)


def _(message: str, **kwargs) -> str:
    """Translate and format with %(name)s placeholders."""
    text = gettext(message)
    return text % kwargs if kwargs else text


def N_(message: str) -> str:
    """Mark a string for extraction without translating it now."""
    return message


def format_date(value: date) -> str:
    return _babel_format_date(value, format="medium", locale=get_language())


def format_short_date(value: date) -> str:
    """Day and month only, e.g. "28.10." / "10/28"."""
    return _babel_format_skeleton("Md", value, locale=get_language())


def format_decimal(value: float, digits: int = 2) -> str:
    return _babel_format_decimal(value, format=f"0.{'0' * digits}", locale=get_language())

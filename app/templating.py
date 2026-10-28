"""Jinja2 setup and render helpers shared by all routes."""

import re
from pathlib import Path

from fastapi import Request
from markupsafe import escape
from fastapi.templating import Jinja2Templates

from app import i18n

BASE_DIR = Path(__file__).resolve().parent
SOURCE_URL = "https://github.com/zefanja/crosspoint"

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Wrapper for card HTML inside <iframe sandbox srcdoc>. The note type CSS comes with
# the card HTML; the dark fallback mirrors Anki's night mode for plain templates.
_CARD_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="color-scheme" content="light dark">'
    "<style>html,body{margin:0}body{padding:24px 12px;overflow-wrap:anywhere;"
    "min-height:100vh;box-sizing:border-box}img{max-width:100%;height:auto}"
    "hr#answer{margin:20px 0}</style></head><body class=\"card card1\"><div class=\"cp-card\">"
)
# App look inside the card frame (after the note type CSS, so the font wins): Archivo,
# content centered vertically. The note types themselves stay untouched (Anki export).
_CARD_TAIL = (
    "</div><style>@font-face{font-family:Archivo;font-weight:400 900;font-stretch:62% 125%;"
    "src:url(/static/fonts/archivo-latin.woff2) format(\"woff2\")}"
    "body.card{display:flex;flex-direction:column;justify-content:center;"
    "font-family:Archivo,system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif;line-height:1.4;"
    "color:#001a3f;background-color:#fff}.cp-card{width:100%}"
    "@media (prefers-color-scheme: dark){html,body,.card{background-color:#0a2143;"
    "color:#eaf4fb}.back{color:#f5d742}td:last-child,.highlight{color:#1d1d1f}}</style></body></html>"
)


# src="file.png" / src='file.png' / src=file.png – relative names only (Anki media).
_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc=)(["']?)([^"'\s>/:]+)\2""", re.IGNORECASE)


def card_document(html: str, media_base: str | None = None) -> str:
    """Full HTML document for a card; passed (escaped) into srcdoc. Relative image
    sources are pointed at the collection's media URL (a <base> tag would be blocked
    by the inherited CSP base-uri 'none')."""
    if media_base:
        prefix = escape(media_base)
        html = _IMG_SRC.sub(lambda m: f'{m.group(1)}"{prefix}{escape(m.group(3))}"', html)
    return _CARD_HEAD + html + _CARD_TAIL


APP_NAME = "CrossPoint"

templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(i18n.gettext, i18n.ngettext, newstyle=True)
templates.env.filters["card_document"] = card_document
templates.env.filters["date"] = i18n.format_date
templates.env.filters["datetime"] = i18n.format_datetime
templates.env.filters["short_date"] = i18n.format_short_date
templates.env.filters["decimal"] = i18n.format_decimal
templates.env.globals.update(
    source_url=SOURCE_URL,
    app_name=APP_NAME,
    get_language=i18n.get_language,
    available_languages=i18n.available_languages,
)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200,
           headers: dict | None = None):
    ctx = {
        "user": getattr(request.state, "user", None),
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "admin_workspace": getattr(request.state, "admin_workspace", False),
        "leader_workspace": getattr(request.state, "leader_workspace", False),
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)

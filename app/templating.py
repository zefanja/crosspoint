"""Jinja2 setup and render helpers shared by all routes."""

from pathlib import Path

from fastapi import Request
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
    "hr#answer{margin:20px 0}</style></head><body class=\"card card1\">"
)
_CARD_TAIL = (
    "<style>@media (prefers-color-scheme: dark){html,body,.card{background-color:#1f1f23;"
    "color:#ececef}.back{color:#f5d742}td:last-child,.highlight{color:#1d1d1f}}</style></body></html>"
)


def card_document(html: str) -> str:
    """Full HTML document for a card; passed (escaped) into srcdoc."""
    return _CARD_HEAD + html + _CARD_TAIL


APP_NAME = "CrossPoint"

templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(i18n.gettext, i18n.ngettext, newstyle=True)
templates.env.filters["card_document"] = card_document
templates.env.filters["date"] = i18n.format_date
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
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)

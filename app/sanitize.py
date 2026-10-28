"""Sanitizing of user input for card fields (nh3)."""

import html as htmllib
import re

import nh3

ALLOWED_TAGS = {"b", "i", "u", "br", "div", "span", "sup", "sub", "ul", "ol", "li",
                # a little more structure, mainly for imported packages (e.g. from an LLM)
                "p", "strong", "em", "hr", "blockquote", "code", "pre", "h1", "h2", "h3", "h4",
                "table", "thead", "tbody", "tr", "th", "td"}
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
# Images only as files from the collection's media folder (no URLs, no paths).
_EMPTY_IMG = re.compile(r"<img\s*/?>", re.IGNORECASE)
MEDIA_NAME = re.compile(r"^[\w-]+\.(png|jpe?g|gif|webp)$", re.IGNORECASE)


def _attribute_filter(tag: str, attr: str, value: str) -> str | None:
    if tag == "img" and attr == "src":
        return value if MEDIA_NAME.match(value) else None
    return value


def field_from_input(text: str) -> str:
    """Textarea input -> safe field HTML. Line breaks become <br>."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return field_from_import(text.replace("\n", "<br>"))


def field_from_import(html: str) -> str:
    """Field HTML (e.g. from an imported package) -> safe field HTML (no scripts, styles,
    links, external images)."""
    html = nh3.clean(html, tags=ALLOWED_TAGS | {"img"}, attributes={"img": {"src"}},
                     attribute_filter=_attribute_filter, url_schemes=set(), link_rel=None)
    return _EMPTY_IMG.sub("", html)  # images whose src was rejected


def image_tag(filename: str) -> str:
    """<img> for a media file name produced by anki_service.add_media."""
    if not MEDIA_NAME.match(filename):
        raise ValueError("invalid media file name")
    return f'<img src="{filename}">'


def field_to_input(html: str) -> str:
    """Field HTML -> textarea content (inverse of field_from_input for <br>)."""
    return _BR.sub("\n", html)


def plain_text(html: str) -> str:
    """Strip all markup, e.g. for list previews."""
    text = htmllib.unescape(nh3.clean(_BR.sub(" ", html), tags=set(), attributes={}))
    return " ".join(text.split())


def plain_text_lines(text: str) -> str:
    """Pasted Bible text -> plain text, line breaks kept.

    All markup is removed; <, > and & are replaced by look-alikes because the Bible
    Memorizer generator puts the text into HTML fields and tokenizes it per word
    (an entity like &amp; would be cut into pieces).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = htmllib.unescape(nh3.clean(_BR.sub("\n", text), tags=set(), attributes={}))
    return text.replace("&", "＆").replace("<", "‹").replace(">", "›")

"""Sanitizing of user input for card fields (nh3)."""

import html as htmllib
import re

import nh3

ALLOWED_TAGS = {"b", "i", "u", "br", "div", "span", "sup", "sub", "ul", "ol", "li"}
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def field_from_input(text: str) -> str:
    """Textarea input -> safe field HTML. Line breaks become <br>."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    html = text.replace("\n", "<br>")
    return nh3.clean(html, tags=ALLOWED_TAGS, attributes={}, link_rel=None)


def field_to_input(html: str) -> str:
    """Field HTML -> textarea content (inverse of field_from_input for <br>)."""
    return _BR.sub("\n", html)


def plain_text(html: str) -> str:
    """Strip all markup, e.g. for list previews."""
    text = htmllib.unescape(nh3.clean(_BR.sub(" ", html), tags=set(), attributes={}))
    return " ".join(text.split())

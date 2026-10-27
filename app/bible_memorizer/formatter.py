"""Python port of app/js/formatter.js from zefanja/bible-memorizer.

Puts every verse number at the start of a line, the format gen_notes expects.
Superscript digits (¹²³…) always count as verse markers; normal numbers only via the
n+1 heuristic (start verse at the beginning, then search for n+1, n+2, … in a context
that looks like a verse boundary). Footnotes like [1], "1:3" references and
"5 Brote" are left alone. Idempotent.
"""

import re

_SUPERSCRIPT_DIGITS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
}
_SUPERSCRIPT_RE = re.compile("[⁰¹²³⁴-⁹]+")
MAX_VERSE_NUM = 200  # Psalm 119 has 176

_START_RE = re.compile(r"^(\d{1,3})\s+(?=[A-Za-zÄÖÜäöüß\"„'(\[‘“»])")
_WORD_CHAR = re.compile(r"[A-Za-zÄÖÜäöüß0-9]")
_AFTER_RE = re.compile(r"^\s+[A-Za-zÄÖÜäöüß\"„'(\[‘“„»«–—-]")


def _normalize_superscripts(text: str) -> str:
    return _SUPERSCRIPT_RE.sub(
        lambda m: "\n" + "".join(_SUPERSCRIPT_DIGITS.get(ch, "") for ch in m.group()) + " ",
        text,
    )


def auto_format_verses(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = raw_text.replace(" ", " ")
    text = _normalize_superscripts(text)
    text = _split_normal_verse_numbers(text)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in re.split(r"\r?\n", text))
    return "\n".join(line for line in lines if line)


def _split_normal_verse_numbers(text: str) -> str:
    lead = 0
    while lead < len(text) and text[lead].isspace():
        lead += 1
    match = _START_RE.match(text[lead:])
    if not match:
        return text
    start = int(match.group(1))
    if not 1 <= start <= MAX_VERSE_NUM:
        return text

    result = f"{start} "
    expected = start + 1
    pos = lead + len(match.group(0))
    while pos < len(text) and expected <= MAX_VERSE_NUM:
        needle = str(expected)
        found = _find_verse_marker(text, needle, pos)
        if found == -1:
            result += text[pos:]
            break
        end_prev = found - 1
        while end_prev >= pos and text[end_prev].isspace():
            end_prev -= 1
        result += text[pos : end_prev + 1] + f"\n{needle} "
        pos = found + len(needle)
        while pos < len(text) and text[pos].isspace():
            pos += 1
        expected += 1
    return result


def _find_verse_marker(text: str, needle: str, start: int) -> int:
    search_from = start
    while search_from < len(text):
        idx = text.find(needle, search_from)
        if idx == -1:
            return -1
        before = text[idx - 1] if idx > 0 else None
        end = idx + len(needle)
        after = text[end] if end < len(text) else None

        # (a) not part of a larger number
        if before is not None and before.isdigit():
            search_from = end
            continue
        if after is not None and after.isdigit():
            search_from = idx + 1
            continue
        # (b) whitespace before and after
        if before is None or not before.isspace() or after is None or not after.isspace():
            search_from = idx + 1
            continue
        # (c) the character before the whitespace must not be a word character
        j = idx - 1
        while j >= 0 and text[j] in " \t":
            j -= 1
        if j >= 0 and _WORD_CHAR.match(text[j]):
            search_from = idx + 1
            continue
        # (d) a letter or opening quote/bracket/dash must follow
        if not _AFTER_RE.match(text[end:]):
            search_from = idx + 1
            continue
        return idx
    return -1


def parse_for_preview(formatted: str) -> list[dict]:
    """[{verse, part, text, continuation}] for the live preview."""
    result = []
    verse, sub = "0", 0
    for line in (ln for ln in re.split(r"\r?\n", formatted) if ln.strip()):
        m = re.match(r"^\s*(\d+)\s+(.*)", line)
        if m:
            verse, sub, body = m.group(1), 0, m.group(2)
        else:
            body, sub = line.strip(), sub + 1
        result.append({"verse": verse, "part": chr(ord("a") + sub % 26), "text": body,
                       "continuation": not m})
    return result

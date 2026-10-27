"""Bible Memorizer: note type and note generator from
https://github.com/zefanja/bible-memorizer (GPL-3.0, same author; combined under AGPL-3.0).

- gen_notes.py: vendored verbatim from src/gen_notes.py (only `add_notes` is not used,
  note creation happens in app/anki_service.py).
- model.py: field names, templates and CSS copied 1:1 from src/model.py (version 1.5).
- formatter.py: Python port of app/js/formatter.js (auto line break before verse numbers).

No module in this package imports `anki`.
"""

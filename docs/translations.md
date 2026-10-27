# Translations

CrossPoint uses gettext via [Babel](https://babel.pocoo.org/). English is the source
language: all UI strings are written in English inside `_("…")` (Python and Jinja
templates). Translations live in `app/locales/<lang>/LC_MESSAGES/messages.po` and are
compiled in memory when the app starts – there is no build step and no `.mo` file.

## Update after changing texts

```bash
pybabel extract -F babel.cfg -k N_ -o app/locales/messages.pot --no-wrap app
pybabel update -i app/locales/messages.pot -d app/locales --no-wrap
```

Then translate the new entries (empty `msgstr`, or marked `#, fuzzy`) in every
`messages.po`. `tests/test_i18n.py` fails if a catalog is incomplete, has fuzzy entries,
uses unknown placeholders, or misses strings from the code.

## Add a language

```bash
pybabel init -i app/locales/messages.pot -d app/locales -l fr --no-wrap
```

Translate `app/locales/fr/LC_MESSAGES/messages.po` (any PO editor works, e.g. Poedit or
Weblate). The language appears automatically in the footer switcher. Anki's own texts
(interval labels like "10 min") follow the language as long as Anki supports it.

## Rules for strings

- Placeholders: `_("Added: %(front)s", front=x)` in Python, `{{ _("Options: %(deck)s", deck=x) }}`
  in templates. Translations must use the same placeholder names.
- Plurals: `{{ ngettext("%(num)s card", "%(num)s cards", n) }}`.
- Strings defined outside a request (constants, exceptions) are marked with `N_("…")` and
  translated where they are shown (`_(str(exc))`).
- Translations may contain simple HTML where the English text does (links, `<em>`).
- Note type and field names (`Bible Verse`, `Reference`, …) are Anki data and stay English;
  only their labels in the UI are translated.

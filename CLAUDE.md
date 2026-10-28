# CLAUDE.md

Web-Lernplattform mit FSRS, kompatibel mit Anki. Vollständige Anforderungen: **SPEC.md** –
vor jeder größeren Änderung den betroffenen Abschnitt lesen.

## Stack
Python 3.12 · FastAPI · uvicorn (1 Worker) · SQLAlchemy 2 + Alembic · Jinja2 + htmx ·
`anki` (PyPI, gepinnt) · pytest · nh3

## Befehle
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head
DATA_DIR=./data SECRET_KEY=dev BASE_URL=http://localhost:8000 TZ=Europe/Berlin uvicorn app.main:app --reload
pytest -q
python -m app.cli create-admin "Name"     # gibt Admin-Code aus
python -m app.cli create-user "Anna" "Ben" # Lerner anlegen (auch im Container per Konsole)
python -m app.cli build-seed               # Seed-Notiztypen aktualisieren (IDs bleiben!)

# Docker lokal
docker compose -f docker-compose.dev.yml up --build
docker build -t crosspoint:local . && docker run --rm crosspoint:local python spike/check_anki.py

# Translations (docs/translations.md)
pybabel extract -F babel.cfg -k N_ -o app/locales/messages.pot --no-wrap app
pybabel update -i app/locales/messages.pot -d app/locales --no-wrap
```

## Deployment
Produktiv läuft der Stack in **Portainer direkt aus diesem Repo** (`docker-compose.yml`).
GitHub Actions testet, baut das Image nach GHCR und setzt per Bot-Commit (`[skip ci]`) das
neue Image-Tag in `docker-compose.yml`; Portainer zieht das per Polling. Details: SPEC §10,
Anleitung: `docs/deploy.md`.

## Harte Regeln
1. **Nur `app/anki_service.py` importiert `anki`.** Routen und andere Module nutzen ausschließlich
   dessen Funktionen.
2. **Jeder Collection-Zugriff** läuft innerhalb des Nutzer-Locks und im Threadpool
   (`run_in_threadpool`). Nie eine Collection außerhalb von `anki_service` öffnen.
3. **Anki-API nie raten.** Vor Verwendung einer Funktion gegen die installierte Version prüfen,
   z. B. `python -c "from anki.collection import Collection; help(Collection.export_anki_package)"`
   oder im Quellcode unter `.venv/lib/python3.12/site-packages/anki/`. Die Tabelle in SPEC §6
   ist ein Startpunkt, keine Garantie.
4. **Kein Nachbau** von Scheduler, FSRS, Datenbankschema oder Exportformat von Anki.
5. Alle Nutzereingaben in Kartenfelder laufen durch `sanitize.py`. Kartenausgabe nur im
   sandboxed iframe (ohne `allow-scripts`).
6. Jede POST-Route: Login-Pflicht + CSRF-Prüfung (`verify_csrf`). Admin-Routen über
   `require_admin`; Seiten, die auch Gruppenleiter sehen, über `require_staff` und dann mit
   `visible_group_ids`/`visible_user_ids` filtern.
7. Login-Codes niemals loggen oder im Klartext speichern.
8. Nach jeder Review `stats.update_after_review()` aufrufen (Dashboard-Cache).
9. **Die `image:`-Zeile in `docker-compose.yml` nie von Hand ändern** – sie gehört dem
   CI-Bump-Job. Andere Änderungen am Compose-File sind erlaubt, müssen aber in Portainer
   ohne lokale Dateien funktionieren (keine relativen Bind-Mounts, kein `build:`).
10. Konfiguration nur über Umgebungsvariablen; neue Variablen in SPEC §8, Compose-File und
    `docs/deploy.md` gleichzeitig nachtragen.
11. Alembic-Migrationen nur additiv (Rollback per Revert muss mit neuerer DB laufen).
12. Alles unter `/data` ist persistent, alles andere im Container ist wegwerfbar.
13. **Notiztyp-IDs sind in allen Collections gleich** (Seed `app/seed/notetypes.apkg`,
    `anki_service.SEED_IDS`), sonst erzeugt das Verteilen von Gruppendecks Duplikate
    („Basic+“). Notiztyp-Änderungen nur über `_upgrade_notetypes` (idempotent, in place) +
    `python -m app.cli build-seed`. **Nie `--fresh`**, sobald Collections existieren.

## Konventionen
- UI-Texte im Code/Templates **Englisch** als Quellsprache, immer über `_()`; Übersetzungen in
  `app/locales/<lang>/LC_MESSAGES/messages.po` (Deutsch muss vollständig sein, `tests/test_i18n.py`).
  Code/Kommentare/Commits **Englisch**.
- Mobile-first für alle Lerner-Seiten: bei 360 px Breite prüfen; Touch-Ziele ≥ 48 px.
  Admin-Seiten (`/admin`) sind Desktop-first (dürfen am Handy nur nicht kaputtgehen).
- htmx-Partials liegen unter `templates/partials/`, Vollseiten erweitern `base.html`.
- Kein JS-Framework, kein Build-Schritt, keine CDNs.
- Neue Tabellen nur per Alembic-Migration.
- Zu jeder Funktion in `anki_service` gehört ein Test mit echter Temp-Collection.

## Stand
Alle Phasen aus SPEC §12 (0–3) sind umgesetzt und deployt. Offen sind die manuellen
Abnahmen (Portainer, Import in Anki Desktop/AnkiDroid, eine Woche Lernen am Handy,
30-Nutzer-Test). Neue Features: SPEC zuerst ergänzen.

## Projektstruktur (Ergänzungen zu SPEC §11)
- `app/i18n.py` – gettext/Babel, Sprache pro Request (ContextVar, Middleware in `main.py`)
- `app/templating.py` – Jinja-Setup, `render()`, `card_document()` (srcdoc + Medien-URLs)
- `app/bible_memorizer/` – vendored aus zefanja/bible-memorizer (`gen_notes.py` unverändert
  lassen), `model.py` (Notiztyp 1:1), `formatter.py` (Port des Web-Formatters)
- `app/routes/statistics.py`, `admin.py` (Hub, Workspace, Import, Jobs)
- `app/distribution.py` – Hintergrundjobs (Verteilen, Werte aktualisieren) mit Fortschritt
- `app/workspace.py` – gemeinsames Bearbeiten der Gruppendecks: `DeckAccess` (sichtbar/bearbeitbar pro Deck für Gruppenleiter), Deck-Anlage, Änderungsprotokoll; Routen nutzen `Depends(deck_access)` bzw. `no_leader_workspace`
- `app/routes/admin_group_decks.py` – Seite „Gruppendecks“ (Verteilen, letzte Änderungen) für Admins und Gruppenleiter

## Anki-Fallstricke (verifiziert mit anki 26.9.3)
- Neue Collections mit `RustBackend(langs=["en"])` anlegen (sonst heißt Basic „Einfach“),
  danach mit Backend in der UI-Sprache öffnen; der Cache öffnet bei Sprachwechsel neu.
- v3-Queue gilt nur fürs aktuelle Deck → vor `get_queued_cards` immer `col.decks.select()`.
- `describe_next_states` liefert Unicode-Isolatoren (U+2068/2069) → entfernen.
- `export_collection_package` schließt die Collection → danach `col.reopen()`.
- Notiztyp mit vorgegebener ID per `add_dict` → **Rust-Panic**. IDs gehen nur per Import.
- Import aktualisiert per GUID, überspringt aber Notizen mit gleicher mtime (Sekunden) →
  in Tests vor dem erneuten Verteilen `time.sleep(1.1)`.
- FSRS-Parameter über `update_deck_configs` speichern (berechnet Memory States neu), nicht
  über `update_config`.
- Deck-Optionen sind Presets, die Decks teilen → `_own_config()` spaltet vorher ab.

## Web-Fallstricke
- Karten-iframe ist sandboxed (opaque origin, erbt die CSP): kein `<base>` (CSP
  `base-uri 'none'`), keine Cookies → Bilder über `/media/<collection>/<token>/<datei>`,
  `src` wird serverseitig umgeschrieben. Bilder/iframes im echten Browser prüfen.
- `request.form()` liefert **Starlette**-`UploadFile` (nicht FastAPI) → `isinstance` darauf.
- `_` nie als Wegwerfvariable oder Parametername (überschattet die Übersetzung).
- Statische Routen (`/admin/users/print`) vor `/{user_id}` definieren.
- `distribution._start` braucht den Event-Loop → aufrufende Routen müssen `async def` sein.
- Jinja-„newstyle“-gettext formatiert immer mit `%` → kein literales `%` in Quelltexten;
  Übersetzungen sind `Markup` (dürfen einfaches HTML enthalten).
- In srcdoc werden Anführungszeichen als `&#34;` escaped → Tests mit `html.unescape`.

## Arbeitsweise
- Tests laufen auf Deutsch (`DEFAULT_LANGUAGE=de` in `conftest.py`) – prüft den Katalog mit.
  Neue UI-Texte: `pybabel extract` + `update`, dann `de/messages.po` vollständig übersetzen.
- Vor jedem Push `git pull --rebase` – CI legt nach jedem Deploy einen Bot-Commit an.
- CI-Fehler stehen als Annotation in der öffentlichen API (Logs brauchen Login):
  `curl -s https://api.github.com/repos/zefanja/crosspoint/check-runs/<job-id>/annotations`.
  Sporadisch rote CI kam (mind. teilweise) von Tests, die sich auf eine undefinierte Reihenfolge
  verließen: Nutzer-IDs sind ULIDs, im selben Millisekunden-Takt angelegt → zufällige Reihenfolge.
  In Tests Nutzer nie per `[0]` aus einer Query ohne `order_by` wählen, sondern per Name.
- Handy-Breite prüfen: Playwright-Screenshots bei 360 px (Chromium liegt unter
  `~/.cache/ms-playwright/`), Overflow über `document.documentElement.scrollWidth` messen.
- `pkill -f <muster>` trifft die eigene Shell, wenn das Muster im Befehl steht → lieber
  `kill $(lsof -t -i:<port>)`.

## Definition of Done (pro Feature)
- Tests grün (`pytest -q`)
- Auf Handy-Breite geprüft (Lerner-Seiten; Admin auf Desktop)
- Bei Collection-relevanten Änderungen: Export-Roundtrip-Test grün
- SPEC.md aktualisiert, falls Verhalten abweicht
- Neue UI-Texte übersetzt (`tests/test_i18n.py` grün)
- `docker build` läuft lokal durch; GitHub-Workflow grün

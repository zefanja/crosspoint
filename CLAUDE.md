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

# Docker lokal
docker compose -f docker-compose.dev.yml up --build
docker build -t versfest:local . && docker run --rm versfest:local python spike/check_anki.py
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
6. Jede POST-Route: Login-Pflicht + CSRF-Prüfung. Admin-Routen über die Dependency
   `require_admin`.
7. Login-Codes niemals loggen oder im Klartext speichern.
8. Nach jeder Review `stats.update_after_review()` aufrufen (Dashboard-Cache).
9. **Die `image:`-Zeile in `docker-compose.yml` nie von Hand ändern** – sie gehört dem
   CI-Bump-Job. Andere Änderungen am Compose-File sind erlaubt, müssen aber in Portainer
   ohne lokale Dateien funktionieren (keine relativen Bind-Mounts, kein `build:`).
10. Konfiguration nur über Umgebungsvariablen; neue Variablen in SPEC §8, Compose-File und
    `docs/deploy.md` gleichzeitig nachtragen.
11. Alembic-Migrationen nur additiv (Rollback per Revert muss mit neuerer DB laufen).
12. Alles unter `/data` ist persistent, alles andere im Container ist wegwerfbar.

## Konventionen
- UI-Texte **Deutsch**, Code/Kommentare/Commits **Englisch**.
- Mobile-first: jede neue Seite bei 360 px Breite prüfen; Touch-Ziele ≥ 48 px.
- htmx-Partials liegen unter `templates/partials/`, Vollseiten erweitern `base.html`.
- Kein JS-Framework, kein Build-Schritt, keine CDNs.
- Neue Tabellen nur per Alembic-Migration.
- Zu jeder Funktion in `anki_service` gehört ein Test mit echter Temp-Collection.

## Reihenfolge
Phasen aus SPEC §12 strikt einhalten. **Phase 0a (Deploy-Pipeline) zuerst**, dann 0b
(anki-Spike im Container). Erst wenn beides abgenommen und der manuelle Import in
Anki Desktop/AnkiDroid bestätigt ist, mit Phase 1 beginnen.

## Definition of Done (pro Feature)
- Tests grün (`pytest -q`)
- Auf Handy-Breite manuell geprüft
- Bei Collection-relevanten Änderungen: Export-Roundtrip-Test grün
- SPEC.md aktualisiert, falls Verhalten abweicht
- `docker build` läuft lokal durch; GitHub-Workflow grün

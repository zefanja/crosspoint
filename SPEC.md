# SPEC – CrossPoint: Web-Lernplattform „kompatibel mit Anki“

> Name: **CrossPoint** – bewusst **nicht** „Anki…“ oder „AnkiWeb“ im Namen (Markenrecht).
> Formulierung erlaubt: „kompatibel mit Anki“.

## 1. Ziel

Eine rein webbasierte, mobil optimierte Lernplattform mit Spaced Repetition (FSRS), die den
Einstieg in die Anki-Welt erleichtert. Nutzer lernen ohne Installation im Browser und können
**jederzeit** ihre Karten **inklusive Lernstand** als Anki-Datei exportieren und in Anki
(Desktop/AnkiDroid/AnkiMobile) weiterlernen.

Hauptanwendung: Bibelverse auswendig lernen, einfache Vorder-/Rückseite-Karten.
Zielgruppe: Gruppen (Gemeinde, Schulklasse) mit 10–200 Nutzern, verwaltet von einem Admin.

### Nicht-Ziele (bewusst ausgeschlossen)

- Kein Sync mit Anki-Clients (kein Sync-Server). Umstieg nur per Export.
- Kein Offline-Lernen (Online-first; PWA nur für Installierbarkeit/Homescreen).
- Keine Medien-Uploads im MVP (Bilder/Audio → Phase 3).
- Kein eigener Template-Editor, keine Cloze-/Tipp-Karten im MVP.
- Kein Nachbau von Ankis Datenformat oder Scheduler – dafür gibt es die `anki`-Bibliothek.

## 2. Grundentscheidung: echte Anki-Collections

Jeder Nutzer besitzt eine echte `collection.anki2`, die ausschließlich über die offizielle
Python-Bibliothek `anki` (PyPI, enthält den Rust-Kern „rslib“) gelesen und geschrieben wird.

Konsequenzen:
- FSRS rechnet exakt wie in Anki; Speicherzustand (Stabilität/Schwierigkeit) und Revlog sind
  automatisch Anki-konform.
- Export ist ein Bibliotheksaufruf, kein Nachbau.
- Schema-Änderungen von Anki werden durch ein Versions-Update der Bibliothek abgefangen.
- **Lizenz:** `anki` steht unter AGPL-3.0 → dieses Projekt wird ebenfalls AGPL-3.0.
  Im Footer jeder Seite: Link „Quellcode“ auf das Repository.

## 3. Rollen

| Rolle | Rechte |
|---|---|
| **Lerner** | Eigene Decks lernen, Karten anlegen/bearbeiten, Deck-Optionen, Export |
| **Admin** | Alles von Lerner + Dashboard: Gruppen, Nutzer, Codes, Gruppendecks verteilen, Fortschritt sehen |
| *Gruppenleiter* (Phase 3) | Wie Lerner + Fortschritt und neue Codes/Druckblatt nur für Mitglieder der eigenen Gruppen (zugewiesen auf der Gruppenseite, Tabelle `group_leaders`); keine Nutzer-/Gruppenverwaltung, kein Verteilen |

## 4. Anmeldung per Code

- Jeder Nutzer hat einen **Login-Code**: 8 Zeichen aus `ABCDEFGHJKMNPQRSTUVWXYZ23456789`
  (ohne 0/O/1/I/L), angezeigt als `K7MX-P3QR`. Eingabe ohne Bindestrich und
  case-insensitiv akzeptieren.
- Admin-Codes: 16 Zeichen.
- Speicherung nur als `HMAC-SHA256(code, SECRET_KEY)` – Klartext wird nur bei Erzeugung angezeigt
  (Druckblatt). Verlorener Code → „Code neu erzeugen“ im Dashboard (alter Code wird ungültig).
- Login-URL mit Code als QR: `https://<domain>/login?c=K7MXP3QR` → sofort eingeloggt
  (Code danach aus der URL entfernen per Redirect).
- Session: zufällige Session-ID in `app.db`, Cookie `HttpOnly; Secure; SameSite=Lax`,
  Laufzeit 180 Tage, gleitend verlängert. „Abmelden“ löscht die Session.
- Rate-Limit: max. 10 Fehlversuche pro IP in 15 Minuten, danach 15 Minuten Sperre.
  Während der Sperre werden Versuche abgewiesen, aber nicht gezählt.
- CSRF: Token im Formular bzw. htmx-Header `X-CSRF-Token` für alle POST-Anfragen
  (HMAC der Session-ID). Das Login-Formular selbst nutzt ein Double-Submit-Cookie.
- Bis zum Dashboard (Phase 2) legt `python -m app.cli create-user …` Nutzer an.

## 5. Datenmodell

### 5.1 Dateisystem

```
DATA_DIR/
  app.db                              # App-Datenbank (SQLite)
  collections/
    _admin/collection.anki2           # Admin-Collection: Quelle der Gruppendecks
    <user_id>/collection.anki2        # eine Collection pro Nutzer
  exports/                            # temporäre Exportdateien (nach 1 h löschen)
```

`<user_id>` ist eine interne ID (z. B. ULID), niemals der Anzeigename.

### 5.2 `app.db` (SQLAlchemy 2 + Alembic)

```
users(id, display_name, role[learner|leader|admin], code_hash, created_at,
      last_login_at, disabled)
groups(id, name, created_at)
group_members(group_id, user_id)
group_decks(id, group_id, admin_deck_id, deck_name, last_distributed_at)
sessions(id, user_id, created_at, expires_at, user_agent)
login_attempts(ip, ts, success)
user_stats(user_id, total_cards, due_today, new_today, reviews_today,
           retention_30d, streak_days, last_review_at, updated_at)
daily_reviews(user_id, day, reviews, again_count, new_count)   # für Diagramme
```

`user_stats`/`daily_reviews` sind ein **Cache**: nach jeder Review und jedem Login aktualisiert,
damit das Dashboard nicht 100+ Collections öffnen muss. Wahrheit bleibt die Collection.

### 5.3 Notiztypen

1. **Basic** (Ankis Standard „Basic“, unverändert übernehmen) – Felder `Front`, `Back`.
   In der Oberfläche „Frage & Antwort“ (Felder „Frage“/„Antwort“): für allgemeine Fragen
   zu Themen der Bibel, nicht nur für Verse.
2. **Bible Verse** – Name exakt `Bible Verse` (Notiztyp- und Feldnamen englisch; nur die
   Beschriftungen in der Oberfläche werden übersetzt)
   - Felder: `Reference` (z. B. „Joh 3,16“), `Text`, `Translation` (optional, z. B. „LUT17“)
   - Karte 1 „Reference → Text“: Vorderseite `{{Reference}}` (+ kleine Übersetzung),
     Rückseite Vorderseite + `<hr id=answer>` + `{{Text}}`
   - Schlichtes, gut lesbares CSS (große Schrift, Zeilenhöhe 1.5, zentriert, Dark-Mode-fähig).
   - Feld `Reverse` (Checkbox „Auch umgekehrt lernen“) + Karte 2 „Text → Reference“, die nur
     erzeugt wird, wenn `Reverse` gefüllt ist (wie Ankis „Basic (optional reversed card)“).

3. **Bible Memorizer** – 1:1 aus dem Anki-Add-on
   [zefanja/bible-memorizer](https://github.com/zefanja/bible-memorizer) (`src/model.py`,
   Version 1.5): 10 Felder (`answer, verse_number, verse_part, table, front, back,
   first_letters, last_letters, title, id`), 4 Karten (letzte Buchstaben, Anfangsbuchstaben,
   Übersichtstabelle, Zusammenhang), Sortierfeld `id`, Original-CSS.
   Notizen werden aus einem Bibeltext erzeugt (siehe 7.1 „Bibeltext importieren“).

Alle drei Notiztypen haben in **jeder** Collection dieselbe ID, sonst legt der Import eines
Gruppendecks Duplikate an („Basic+“, „Bible Verse+“). IDs lassen sich per API nicht setzen, ein Import
übernimmt sie aber: Die kanonischen Notiztypen liegen im Seed-Paket
`app/seed/notetypes.apkg` (erzeugt mit `python -m app.cli build-seed`, IDs in
`anki_service.SEED_IDS`) und werden bei Anlage einer Collection importiert. Ungenutzte
Notiztypen mit fremder ID werden ersetzt.
Änderungen an Notiztypen: in `_upgrade_notetypes` ergänzen (läuft idempotent bei jedem Öffnen,
IDs bleiben) und `python -m app.cli build-seed` ausführen – das baut den Seed **aus dem
bestehenden** und behält die IDs. `--fresh` (neue IDs) nur, solange keine Collection existiert.

## 6. Collection-Zugriff (`anki_service.py`)

- **Einziges Modul**, das `anki` importiert. Alle Routen sprechen nur mit diesem Service.
- Ein uvicorn-Worker, Lock pro Nutzer (`asyncio.Lock` in einem Dict). Collection-Zugriffe laufen
  in `run_in_threadpool`, innerhalb des Locks.
- Collection-Cache: max. 20 offene Collections (LRU), nach 10 Minuten Inaktivität schließen.
  Beim Shutdown alle sauber schließen.
- Neue Collection anlegen: FSRS global einschalten, Tagesbeginn 4 Uhr, Notiztypen anlegen,
  Standard-Deck „Meine Karten“.
- Zeitzone: Prozess läuft mit `TZ=Europe/Berlin`. **Im Spike prüfen**, dass der Tageswechsel
  (fällige Karten) für Nutzer in Deutschland korrekt ist.

Benötigte Operationen (Signaturen grob, gegen installierte `anki`-Version verifizieren):

| Operation | anki-API (zu verifizieren) |
|---|---|
| Deckbaum mit Zählern | `col.sched.deck_due_tree()` |
| Nächste Karte | `col.sched.get_queued_cards(fetch_limit=1)` |
| Intervall-Vorschau je Button | `col.sched.describe_next_states(states)` |
| Antworten | `col.sched.build_answer(card=…, states=…, rating=…)` + `col.sched.answer_card(…)` |
| Rückgängig | `col.undo()` / `col.undo_status()` |
| Rendering | `card.question()`, `card.answer()` |
| Notiz anlegen/ändern | `col.new_note(notetype)`, `col.add_note(note, deck_id)`, `col.update_note(note)` |
| Suche | `col.find_cards(query)` / `col.find_notes(query)` |
| Deck-Optionen | `col.decks.config_dict_for_deck_id(did)`, `col.decks.update_config(conf)` |
| Export Sammlung | `col.export_collection_package(path, include_media=…, legacy=False)` |
| Export Deck | `col.export_anki_package(out_path=…, options=ExportAnkiPackageOptions(with_scheduling=True, …), limit=DeckIdLimit(did))` |
| Import Deck | `col.import_anki_package(ImportAnkiPackageRequest(…))` |

**Ergebnisse Spike 0b (verifiziert mit `anki==26.9.3`, Python 3.12, `python:3.12-slim`):**
- Wheels: `manylinux_2_35` für x86_64 und aarch64 → Debian-Basis nötig (kein Alpine).
- FSRS global: `col.set_config("fsrs", True)`. Tagesbeginn: `prefs = col.get_preferences();
  prefs.scheduling.rollover = 4; col.set_preferences(prefs)` (4 ist bereits Standard).
- Neue Collections enthalten bereits „Basic“ und das Deck „Default“ (ID 1);
  „Meine Karten“ per `col.decks.id("Meine Karten")` anlegen.
- Die v3-Queue gilt nur für das **aktuelle Deck** → vor `get_queued_cards` immer
  `col.decks.select(did)`.
- Sprache: Ein Backend mit `langs=["de"]` legt Standard-Notiztypen lokalisiert an
  („Einfach“ statt „Basic“). Deshalb neue Collections mit `RustBackend(langs=["en"])`
  anlegen und danach mit `RustBackend(langs=["de"])` öffnen → deutsche Intervall-Labels
  („<10 Min.“, „4 Tg.“) und Fehlermeldungen, Namen bleiben „Basic“/„Default“.
- `describe_next_states` liefert Labels mit Unicode-Isolatoren (U+2068/U+2069) → für die
  Anzeige entfernen.
- Deck-Optionen: `conf["new"]["perDay"]`, `conf["rev"]["perDay"]`, `conf["desiredRetention"]`.
- Export Deck: `limit=DeckIdLimit(deck_id=did)`; Import-Optionen
  `ImportAnkiPackageOptions(with_scheduling, with_deck_configs, update_notes, merge_notetypes, …)`.
- `.colpkg` + `.apkg` wieder geöffnet: Revlog und FSRS-Speicherzustand vollständig erhalten.
- Import mit `update_notes=…ALWAYS` aktualisiert per GUID, überspringt aber Notizen mit
  **gleicher mtime (Sekunden)** als „duplicate“ – relevant nur für Tests.
- `export_collection_package` schließt die Collection → danach `col.reopen()` (Undo-Stack weg).
- Nach `col.undo()` einer Bewertung liefert die Queue die zurückgenommene Karte wieder zuerst.
- Tageswechsel mit `TZ=Europe/Berlin`: `col.sched.day_cutoff` = 04:00 Ortszeit. ✔

## 7. Funktionen

### 7.1 Lerner (MVP)

**Startseite / Deckliste**
- Liste der Decks mit drei Zahlen: neu (blau), Lernen (rot), fällig (grün) – wie Anki.
- Antippen → Lernansicht. Unten: „Karte hinzufügen“, „Karten durchsuchen“, „Export“.
- Hinweis-Banner, wenn heute alles erledigt ist („Fertig für heute 🎉 – nächste Karten morgen“).

**Lernansicht** (wichtigste Seite, mobile-first)
- Kartenvorderseite füllt den Bildschirm; großer Button „Antwort zeigen“ unten (Daumenzone).
- Danach vier Buttons **Nochmal / Schwer / Gut / Einfach** mit Intervall-Vorschau darunter
  („<10 min“, „2 T“, …). Farben wie Anki.
- Desktop: Leertaste = Antwort zeigen / Gut; Tasten 1–4 = Bewertung; `Z` = Rückgängig.
- „Rückgängig“-Button für die letzte Bewertung (Fehltipp auf dem Handy).
- *Tippmodus* (Phase 3, Schalter ⌨ in der Lernansicht, Cookie): Antwort eintippen → „Prüfen“
  zeigt Ankis Diff (`col.compare_answer`) und die Rückseite, danach bewerten. Für Bible Verse
  (Text bzw. Referenz), Basic (Rückseite) und Bible Memorizer Karte 1–3 (`answer`).
- Fortschrittszeile oben: verbleibende Karten (neu/Lernen/fällig).
- Kein Seiten-Reload: htmx tauscht nur den Kartenbereich aus. Nächste Karte wird nach
  der Bewertung direkt mitgeliefert (eine Anfrage pro Bewertung).
- Umsetzung: Vorder- und Rückseite kommen in derselben Antwort; „Antwort zeigen“ schaltet
  rein per CSS um (funktioniert ohne JS). Die Antwortzeit misst der Server
  (`shown_at` im Formular).

**Karten anlegen / bearbeiten**
- Formular je Notiztyp; Deck-Auswahl; „Hinzufügen“ hält Formular offen (Serienanlage).
- Felder: einfaches Textfeld, Zeilenumbrüche → `<br>`. Erlaubtes HTML nach Sanitizing:
  `b, i, u, br, div, span, sup, sub, ul, ol, li` (Bibliothek `nh3`).
- Karten in Gruppendecks sind für Lerner **schreibgeschützt** (Tag `gruppendeck` erkennen).

**Bibeltext importieren** (Bible Memorizer)
- Felder: Deck, Titel, Bibeltext, Zeilen Zusammenhang (1–10, Standard 2),
  „Übersichtstabelle erstellen“ (nur bei mehr als 4 Versen).
- Der Text wird von Markup befreit und automatisch formatiert (Port von `formatter.js` aus
  dem Web-Tool des Add-ons: Zeilenumbruch vor jeder Versnummer, auch Hochzahlen,
  n+1-Heuristik). Live-Vorschau der erkannten Abschnitte per htmx.
- Erzeugung mit den Funktionen aus `gen_notes.py` (unverändert vendored unter
  `app/bible_memorizer/`). Einzige Abweichung: „[Beginning]“ → „[Anfang]“.
- Bibeltext-Notizen sind nicht einzeln bearbeitbar (löschen und neu importieren);
  aussetzen und löschen geht.

**Kartenliste / Suche**
- Suchfeld (Anki-Suchsyntax wird durchgereicht), Liste: Vorderseite (gekürzt), Deck, fällig am.
- Antippen → bearbeiten, löschen (mit Bestätigung), aussetzen.

**Deck-Optionen** (pro Deck, reduziert)
- Neue Karten pro Tag (Standard 10), max. Wiederholungen pro Tag (Standard 200),
  gewünschte Retention (Slider 0,80–0,97, Standard 0,90).
- FSRS ist immer an und nicht abschaltbar.
- Phase 2: Button „FSRS-Parameter optimieren“ (ab ca. 400 Reviews sinnvoll).

**Export**
- „Alles exportieren (für neue Anki-Nutzer)“ → `.colpkg`
  Hinweistext: *ersetzt beim Import die Sammlung in Anki – nur für leere/neue Anki-Installationen.*
- „Deck exportieren (in bestehendes Anki einfügen)“ → `.apkg` mit Lernstand und Deck-Optionen.
- Kurze bebilderte Anleitung: Anki installieren → Datei öffnen/importieren → AnkiWeb-Konto →
  synchronisieren.

### 7.2 Admin-Dashboard (MVP)

*Umsetzung:* Desktop-first (Lerner-Seiten bleiben mobile-first). Erreichbar über „Verwaltung“
in der Kopfzeile (`/admin`). Hintergrundaufgaben (Verteilen, Werte aktualisieren) laufen
im Prozess und zeigen eine Fortschrittsseite (htmx-Polling).

**Nutzer**
- Anlegen einzeln oder als Liste (ein Name pro Zeile) → Codes werden erzeugt.
- **Druckblatt** (PDF oder druckoptimiertes HTML): pro Nutzer Name, Code, QR-Code mit
  Login-Link, Kurzanleitung. 8 Kärtchen pro A4-Seite zum Ausschneiden.
  *Umsetzung:* druckoptimiertes HTML (2 × 4 Kärtchen à 105 × 74 mm), QR als Inline-SVG
  (`segno`), `Cache-Control: no-store`. Da Codes nur als HMAC gespeichert sind, erscheint das
  Druckblatt direkt nach dem Anlegen bzw. nach „Neue Codes + Druckblatt“ (auch für eine
  Auswahl/Gruppe) – alte Codes werden dabei ungültig.
- Code neu erzeugen, deaktivieren, löschen (löscht Collection-Ordner – Bestätigung mit Namen tippen).
- Gruppen zuordnen.

**Gruppen**
- Anlegen, umbenennen, löschen; Mitglieder verwalten.
- Gruppendecks zuweisen (siehe 7.3).

**Fortschritt**
- Tabelle pro Gruppe: Name, letzte Aktivität, Reviews heute/7 Tage, fällige Karten,
  Retention 30 Tage, Streak. Sortierbar. Werte aus `user_stats`.
- Ampel: rot = 7+ Tage inaktiv oder > 100 fällig.

### 7.3 Gruppendecks

- Der Admin pflegt Gruppendecks in seiner **Admin-Collection** mit derselben Oberfläche wie
  Lerner (Karten anlegen, bearbeiten) – plus „`.apkg` importieren“ (z. B. aus dem eigenen
  Browser-Generator).
  *Umsetzung:* Umschalter „Admin-Sammlung bearbeiten“ (Cookie `vf_ws`); ein Banner zeigt den
  Modus an. Notizen in der Admin-Collection bekommen den Tag `gruppendeck` (beim Anlegen und
  vor jedem Verteilen für das ganze Deck).
- „An Gruppe verteilen“: Deck aus der Admin-Collection als `.apkg` **ohne** Lernstand exportieren
  und in jede Mitglieds-Collection importieren.
  - Notizen tragen den Tag `gruppendeck`.
  - Erneutes Verteilen aktualisiert bestehende Notizen anhand der GUID (Inhalt ändert sich,
    Lernstand bleibt), neue Notizen kommen hinzu. Gelöschte Notizen werden **nicht** gelöscht
    (Phase 2: optional aussetzen).
  - Neue Gruppenmitglieder erhalten alle Gruppendecks beim Hinzufügen automatisch.
- Verteilung läuft als Hintergrundaufgabe mit Fortschrittsanzeige (100 Nutzer × Import).

## 8. Oberfläche & Technik

- **Backend:** Python 3.12, FastAPI, uvicorn (1 Worker), SQLAlchemy 2, Alembic, Jinja2.
- **Frontend:** serverseitig gerenderte Seiten + htmx; minimales eigenes JS nur für
  Tastenkürzel in der Lernansicht. Kein Build-Schritt. htmx lokal ausliefern (kein CDN).
- **CSS:** eigenes, schlankes Stylesheet mit CSS-Variablen, Hell/Dunkel nach System,
  Touch-Ziele ≥ 48 px, funktioniert ab 360 px Breite.
- **Karten-Rendering:** Karten-HTML in einem `<iframe sandbox srcdoc=…>` ohne `allow-scripts`
  darstellen – schützt vor Skripten aus importierten Decks und isoliert das Template-CSS.
- **PWA:** `manifest.webmanifest`, Icons, minimaler Service Worker (nur App-Shell-Cache,
  Offline-Seite „Keine Verbindung“). Kein Offline-Lernen.
- **Sprache:** UI mehrsprachig. Quellsprache Englisch (`_("…")` in Code und Templates),
  Übersetzungen per gettext/Babel in `app/locales/<lang>/LC_MESSAGES/messages.po`
  (zur Laufzeit im Speicher kompiliert, kein Build-Schritt). Erste Übersetzung: Deutsch.
  Auswahl: Sprach-Cookie (Umschalter im Footer) → `Accept-Language` → `DEFAULT_LANGUAGE`.
  Die Anki-Collection wird mit einem Backend in der UI-Sprache geöffnet (Intervall-Labels);
  das Standard-Deck heißt bei Anlage in der Sprache des Nutzers („My cards“/„Meine Karten“).
  Notiztyp- und Feldnamen sind Daten und bleiben unübersetzt (nur die Labels).
  Code, Kommentare, Commits Englisch.
- **Konfiguration** ausschließlich per Umgebungsvariablen (12-Factor):

| Variable | Pflicht | Standard | Zweck |
|---|---|---|---|
| `SECRET_KEY` | ja | – | Session-/HMAC-Schlüssel (min. 32 Zeichen) |
| `BASE_URL` | ja | – | Öffentliche URL, z. B. `https://lernen.example.de` (für QR-Codes) |
| `DATA_DIR` | nein | `/data` | Datenverzeichnis |
| `TZ` | nein | `Europe/Berlin` | Zeitzone |
| `FORWARDED_ALLOW_IPS` | nein | `127.0.0.1` | IPs des Reverse Proxys, deren `X-Forwarded-For` vertraut wird (wichtig für Rate-Limit) |
| `INITIAL_ADMIN_NAME` | nein | `Admin` | Name des beim Erststart angelegten Admins |
| `BACKUP_KEEP_DAYS` | nein | `7` | Aufbewahrung der nächtlichen Backups |
| `DEFAULT_LANGUAGE` | nein | `en` | Fallback-Sprache der Oberfläche (`en`, `de`, …) |

- Start bricht mit klarer Fehlermeldung ab, wenn Pflichtvariablen fehlen.
- Footer zeigt die laufende Version (`APP_VERSION` = Git-Commit, beim Image-Build gesetzt) –
  so ist sofort sichtbar, ob ein Auto-Update angekommen ist.
- **Erststart:** Existiert noch kein Admin, wird einer angelegt und sein Code **einmalig**
  deutlich sichtbar ins Container-Log geschrieben (in Portainer unter „Logs“ abrufbar).
  Das ist die einzige erlaubte Ausnahme von „Codes nie loggen“.

## 9. Datenschutz

- Gespeichert wird nur ein Anzeigename (Vorname/Kürzel genügt), keine E-Mail.
- Datenschutzhinweis-Seite `/privacy` (Platzhaltertext) im Footer.
- Nutzer kann jederzeit exportieren; Admin kann Nutzer samt Daten löschen.
- Logs ohne Login-Codes (Query-Parameter `c` aus Access-Logs filtern).

## 10. Hosting: Docker auf eigenem Server (Portainer)

Ziel: Auf dem Server wird **nur das Git-Repository in Portainer eingetragen**. GitHub baut das
Image, Portainer holt sich Änderungen selbstständig. Kein manuelles Bauen, kein SSH.
(Uberspace bleibt eine spätere Option; die App selbst ist hosting-neutral.)

### 10.1 Ablauf (Überblick)

```
git push main
  └─ GitHub Actions
       1. Tests (pytest)
       2. Image bauen → ghcr.io/<owner>/<repo>:sha-<kurz> und :latest
       3. Bot-Commit: docker-compose.yml auf neues Tag sha-<kurz> setzen  [skip ci]
  └─ Portainer (GitOps-Polling, alle 5 Min.)
       erkennt geänderte docker-compose.yml → zieht neues Image → startet Stack neu
  └─ Container-Start
       Alembic-Migrationen → uvicorn
```

Warum der Bot-Commit statt nur `:latest`: Portainer erkennt Änderungen zuverlässig am
Compose-File im Repo. Jede Version steht damit nachvollziehbar in der Git-Historie, und ein
**Rollback** ist ein `git revert` des Bot-Commits.

### 10.2 Zu erstellende Dateien

**`Dockerfile`**
- Basis `python:3.12-slim` (Debian; aktuelle glibc → `anki`-Wheels installierbar).
- `tzdata` installieren, `requirements.txt` zuerst kopieren (Layer-Cache), dann App.
- Läuft als Nicht-Root-Nutzer (UID 1000), `WORKDIR /app`, `VOLUME /data`, `EXPOSE 8000`.
- Build-Argument `APP_VERSION` → Umgebungsvariable.
- `HEALTHCHECK` gegen `GET /healthz` (prüft DB-Verbindung und Schreibrecht auf `/data`).
- Ziel-Architektur `linux/amd64`; `linux/arm64` nur bauen, wenn die `anki`-Wheels es hergeben.

**`docker/entrypoint.sh`**
- Pflichtvariablen prüfen → `alembic upgrade head` → Erststart-Admin anlegen →
  `exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers
  --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"`.
- Sauberes Herunterfahren bei `SIGTERM` (Collections schließen, siehe Lifespan).

**`.dockerignore`** – `.git`, `.venv`, `data/`, `tests/`, `__pycache__`, `*.md` außer nötigen.

**`docker-compose.yml`** (wird von Portainer direkt aus dem Repo verwendet)

```yaml
services:
  app:
    image: ghcr.io/OWNER/REPO:sha-0000000   # wird von CI automatisch aktualisiert
    container_name: crosspoint
    restart: unless-stopped
    environment:
      SECRET_KEY: ${SECRET_KEY:?SECRET_KEY muss gesetzt sein}
      BASE_URL: ${BASE_URL:?BASE_URL muss gesetzt sein}
      TZ: Europe/Berlin
      DATA_DIR: /data
      FORWARDED_ALLOW_IPS: ${FORWARDED_ALLOW_IPS:-127.0.0.1}
      INITIAL_ADMIN_NAME: ${INITIAL_ADMIN_NAME:-Admin}
      DEFAULT_LANGUAGE: ${DEFAULT_LANGUAGE:-en}
    volumes:
      # Absoluter Host-Pfad (z. B. /srv/crosspoint) oder Standard: benanntes Volume
      - ${DATA_PATH:-crosspoint-data}:/data
    ports:
      - "${PORT:-8000}:8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  crosspoint-data:
```

Hinweis: In Portainer-Git-Stacks **keine relativen Bind-Mounts** (`./data`) verwenden – die
zeigen in Portainers internen Klon des Repos und gehen bei Updates verloren.

**`.github/workflows/build-deploy.yml`**
- Trigger: `push` auf `main` (Bot-Commits enthalten `[skip ci]` → keine Endlosschleife),
  zusätzlich `workflow_dispatch` für manuelles Auslösen.
- `permissions: contents: write, packages: write`.
- Job `test`: Python 3.12, `pip install -r requirements.txt -r requirements-dev.txt`, `pytest -q`.
- Job `build` (needs: test): `docker/setup-buildx-action`, `docker/login-action` (GHCR mit
  `GITHUB_TOKEN`), `docker/metadata-action` (Tags `sha-<kurz>` und `latest`),
  `docker/build-push-action` mit `build-args: APP_VERSION=${{ github.sha }}` und GHA-Cache.
- Job `bump` (needs: build): Image-Zeile in `docker-compose.yml` per `sed` auf `sha-<kurz>`
  setzen, als `github-actions[bot]` committen mit Nachricht
  `deploy: <kurz> [skip ci]`, pushen. Nur ausführen, wenn sich die Zeile tatsächlich ändert.
- Workflow ist mit Platzhaltern `OWNER/REPO` generisch über `${{ github.repository }}`
  formuliert (Kleinbuchstaben erzwingen, GHCR verlangt das).

**`.github/workflows/test.yml`** – Tests für Pull Requests (ohne Build/Deploy).

**`docs/deploy.md`** – Schritt-für-Schritt-Anleitung (Inhalt siehe 10.3).

### 10.3 Einrichtung in Portainer (Inhalt von `docs/deploy.md`)

1. **Image öffentlich machen:** Nach dem ersten erfolgreichen Workflow-Lauf auf GitHub unter
   *Packages → Paket → Package settings → Change visibility → Public*.
   (Repo ist wegen AGPL ohnehin öffentlich. Bei privatem Paket stattdessen in Portainer unter
   *Registries* GHCR mit einem Personal Access Token `read:packages` hinterlegen.)
2. **Stack anlegen:** *Stacks → Add stack → Repository*
   - Repository URL: `https://github.com/<owner>/<repo>`
   - Reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
3. **Umgebungsvariablen** im Stack setzen: `SECRET_KEY` (z. B. `openssl rand -hex 32`),
   `BASE_URL`, `DATA_PATH` (absoluter Pfad auf dem Server, empfohlen), `FORWARDED_ALLOW_IPS`
   (IP des Reverse Proxys), optional `PORT`.
4. **GitOps updates** aktivieren: Mechanismus *Polling*, Intervall `5m`.
   Falls angeboten zusätzlich *Re-pull image* aktivieren.
5. **Deploy** → unter *Containers → crosspoint → Logs* den einmalig ausgegebenen Admin-Code notieren.
6. **Reverse Proxy** (bestehender Caddy/nginx) auf `http://<server>:<PORT>` zeigen lassen,
   TLS dort terminieren. Die App selbst spricht nur HTTP.
7. **Prüfen:** Footer zeigt Commit-Kurz-ID. Nach dem nächsten Push sollte sie sich innerhalb
   weniger Minuten ändern.

**Rollback:** Auf GitHub den letzten `deploy:`-Commit reverten (Commit-Nachricht mit
`[skip ci]`, sonst baut CI sofort wieder den aktuellen Stand) → Portainer zieht beim nächsten
Polling das vorherige Image. Daten bleiben unberührt; Migrationen müssen deshalb
**abwärtskompatibel** sein (nur additive Schemaänderungen pro Release).

### 10.4 Backups

- Die App erstellt selbst nächtlich um 3:00 Uhr (In-Process-Scheduler) ein Backup nach
  `/data/backups/<datum>/`: `app.db` per SQLite-Backup-API, Collections per
  `col.export_collection_package` bzw. Dateikopie unter Nutzer-Lock, gepackt als `.tar.gz`.
- Rotation nach `BACKUP_KEEP_DAYS`.
- Weil `/data` auf einem Host-Pfad liegt, wird es zusätzlich von der bestehenden
  Server-Sicherung erfasst. Restore-Anleitung in `docs/deploy.md`.

### 10.5 Lokale Entwicklung mit Docker

`docker-compose.dev.yml` baut das Image lokal (`build: .`), mountet `./app` für Live-Reload
und setzt Entwicklungs-Defaults (`SECRET_KEY=dev`, `BASE_URL=http://localhost:8000`).
Start: `docker compose -f docker-compose.dev.yml up --build`.

## 11. Projektstruktur

```
app/
  main.py              # FastAPI-App, Router-Registrierung, Lifespan (Collections schließen)
  config.py
  db.py                # SQLAlchemy-Engine, Session
  models.py            # app.db-Tabellen
  auth.py              # Codes, Sessions, Rate-Limit, CSRF, Rollen-Dependencies
  anki_service.py      # EINZIGER anki-Import; Lock, Cache, alle Collection-Operationen
  stats.py             # user_stats / daily_reviews aktualisieren
  backup.py            # nächtliches Backup + Rotation
  bootstrap.py         # Erststart: Admin anlegen, Code ins Log
  distribution.py      # Gruppendecks verteilen (Hintergrundaufgabe)
  sanitize.py
  routes/
    auth.py  decks.py  review.py  notes.py  browse.py  export.py
    admin_users.py  admin_groups.py  admin_progress.py
  templates/  (base.html, partials/…)
  static/     (htmx.min.js, app.css, review.js, manifest.webmanifest, sw.js, icons/)
migrations/            # Alembic
tests/
docker/
  entrypoint.sh
docs/
  deploy.md            # Portainer-Einrichtung, Rollback, Restore
spike/                 # Phase 0
.github/workflows/
  build-deploy.yml     # Test → Image → GHCR → Compose-Tag-Bump
  test.yml             # Tests für Pull Requests
Dockerfile
.dockerignore
docker-compose.yml     # Produktiv, von Portainer aus dem Repo genutzt
docker-compose.dev.yml # Lokale Entwicklung
```

## 12. Phasen & Abnahmekriterien

### Phase 0 – Spike + Deploy-Pipeline (zuerst)
**0a – Pipeline:** Minimal-App (nur `/healthz` und eine Seite mit Versionsanzeige) mit
Dockerfile, Compose, Workflows und `docs/deploy.md`. Abnahme: Stack in Portainer aus dem Repo
angelegt; ein Push ändert die angezeigte Version innerhalb von ~10 Minuten ohne Eingriff
am Server; Rollback per Revert funktioniert.

**0b – anki-Spike:** `spike/check_anki.py` muss **im gebauten Container** durchlaufen
(`docker run --rm <image> python spike/check_anki.py`):
1. `pip install anki` im Image funktioniert (Version notieren und in `requirements.txt` pinnen).
2. Collection anlegen, FSRS an, Notiztyp „Bible Verse“ anlegen, 20 Notizen.
3. 50 simulierte Reviews über die v3-Scheduler-API inkl. Intervall-Vorschau und Undo.
4. Export `.colpkg` und `.apkg` (mit Lernstand).
5. Export erneut mit `anki` öffnen: Revlog-Einträge vorhanden, FSRS-Speicherzustand gesetzt.
6. **Manuell:** `.colpkg` in Anki Desktop und AnkiDroid importieren – Fälligkeiten und
   Intervalle stimmen mit der Web-Vorschau überein.
7. Tageswechsel/Zeitzone prüfen.

Die Pipeline aus 0a steht vor 0b, damit jede weitere Entwicklung sofort automatisch auf dem
Server landet.

### Phase 1 – MVP Lerner
Login per Code, Deckliste, Lernansicht inkl. Undo und Tastatur, Karten anlegen/bearbeiten,
Suche, Deck-Optionen, beide Exporte, PWA-Manifest.
*Abnahme:* Ein Nutzer lernt eine Woche lang am Handy, exportiert, importiert in AnkiDroid –
alle Karten mit korrekten Fälligkeiten vorhanden.

### Phase 2 – Dashboard
Nutzer/Gruppen/Codes, Druckblatt mit QR, Admin-Collection, Gruppendecks verteilen,
Fortschrittstabelle.
*Abnahme:* 30 Testnutzer in 2 Gruppen anlegen, Deck verteilen, Deck korrigieren und
neu verteilen – Lernstände bleiben erhalten.

### Phase 3 – Komfort
Gruppenleiter-Rolle, FSRS-Optimierung, Statistikseite für Lerner (Heatmap, Prognose),
Medien (Bilder), zweite Kartenrichtung „Text → Reference“ für Bibelverse,
Lückentext-/Tipp-Modus für Verse.

## 13. Tests

- `pytest` mit temporärem `DATA_DIR`.
- Pflicht: Login/Rate-Limit, Rollenrechte (Lerner kommt nicht ins Dashboard),
  Review-Ablauf, Undo, Export-Roundtrip (Export → mit `anki` öffnen → prüfen),
  Gruppendeck-Update erhält Lernstand, Sanitizing (kein `<script>` überlebt).
- Lernansicht zusätzlich manuell auf iOS Safari und Android Chrome prüfen.

# Deployment mit Portainer

Auf dem Server wird nur dieses Git-Repository als Stack in Portainer eingetragen.
GitHub Actions testet, baut das Image nach GHCR und setzt per Bot-Commit
(`deploy: <kurz> [skip ci]`) das neue Image-Tag in `docker-compose.yml`. Portainer
erkennt die Änderung per Polling und startet den Stack mit dem neuen Image neu.

```
git push main → Tests → Image ghcr.io/<owner>/<repo>:sha-<kurz> → Bot-Commit
             → Portainer-Polling (5 min) → neues Image → Alembic-Migrationen → uvicorn
```

## Einrichtung

1. **Ersten Workflow-Lauf abwarten.** Nach dem ersten Push auf `main` unter
   *Actions → Build & Deploy* prüfen, dass alle drei Jobs (test, build, bump) grün sind.
   Danach steht in `docker-compose.yml` ein echtes Tag statt `sha-0000000`.

2. **Image öffentlich machen.** Auf GitHub unter *Packages → crosspoint → Package settings
   → Change visibility → Public*. (Das Repo ist wegen AGPL ohnehin öffentlich.)
   Alternativ bei privatem Paket: in Portainer unter *Registries* GHCR
   (`ghcr.io`) mit einem Personal Access Token mit Scope `read:packages` hinterlegen.

3. **Stack anlegen:** *Stacks → Add stack → Repository*
   - Repository URL: `https://github.com/<owner>/<repo>`
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`

4. **Umgebungsvariablen** im Stack setzen:

   | Variable | Pflicht | Beispiel / Standard | Zweck |
   |---|---|---|---|
   | `SECRET_KEY` | ja | `openssl rand -hex 32` | Session-/HMAC-Schlüssel (min. 32 Zeichen). **Nie ändern** – sonst werden alle Login-Codes ungültig. |
   | `BASE_URL` | ja | `https://lernen.example.de` | Öffentliche URL (für QR-Codes) |
   | `DATA_PATH` | empfohlen | `/srv/crosspoint` | Absoluter Host-Pfad für `/data`. Ohne: benanntes Volume `crosspoint-data`. |
   | `FORWARDED_ALLOW_IPS` | empfohlen | `127.0.0.1` | IP des Reverse Proxys (wichtig für das Rate-Limit) |
   | `PORT` | nein | `8000` | Host-Port |
   | `INITIAL_ADMIN_NAME` | nein | `Admin` | Name des beim Erststart angelegten Admins |
   | `BACKUP_KEEP_DAYS` | nein | `7` | Aufbewahrung der nächtlichen Backups |
   | `DEFAULT_LANGUAGE` | nein | `en` | Sprache, wenn weder Sprach-Cookie noch Browser-Sprache passen (`en`, `de`) |

   Bei `DATA_PATH` muss das Verzeichnis für UID 1000 beschreibbar sein:
   `sudo mkdir -p /srv/crosspoint && sudo chown 1000:1000 /srv/crosspoint`.

   **Keine relativen Pfade** (`./data`) verwenden – die zeigen in Portainers internen
   Klon des Repos und gehen bei Updates verloren.

5. **GitOps updates** aktivieren: Mechanismus *Polling*, Intervall `5m`.
   Falls angeboten, zusätzlich *Re-pull image* aktivieren.

6. **Deploy the stack.** Danach unter *Containers → crosspoint → Logs* den einmalig
   ausgegebenen Admin-Code notieren (Zeile „Admin code“).

   Bis das Admin-Dashboard (Phase 2) fertig ist, werden Nutzer über die Konsole angelegt:
   *Containers → crosspoint → Console → Connect* (`/bin/sh`), dann

   ```bash
   python -m app.cli create-user "Anna" "Ben"   # gibt Name, Code und ID aus
   python -m app.cli list-users
   python -m app.cli reset-code <user-id>       # neuer Code, alter wird ungültig
   ```

   Login-Link für einen QR-Code: `<BASE_URL>/login?c=<CODE>`.

7. **Reverse Proxy** (bestehender Caddy/nginx) auf `http://<server>:<PORT>` zeigen lassen
   und TLS dort terminieren. Die App spricht nur HTTP. Beispiel Caddy:

   ```
   lernen.example.de {
       reverse_proxy 127.0.0.1:8000
   }
   ```

8. **Prüfen:** Der Footer zeigt die Commit-Kurz-ID. Nach dem nächsten Push auf `main`
   sollte sie sich innerhalb von ~10 Minuten ändern. `GET /healthz` liefert
   `{"status": "ok", ...}`.

## Rollback

Den letzten `deploy:`-Commit reverten – **mit `[skip ci]` in der Commit-Nachricht**.
Ohne `[skip ci]` würde der Revert einen neuen Workflow-Lauf auslösen, der den aktuellen
Code neu baut und das Tag sofort wieder nach vorne setzt.

```bash
git pull
git revert --no-edit <deploy-sha>
git commit --amend -m "rollback: revert deploy <kurz> [skip ci]"
git push
```

Portainer zieht beim nächsten Polling das vorherige Image. Daten bleiben unberührt;
Migrationen sind deshalb nur additiv – eine ältere Version läuft mit einer neueren DB.

Zum Wiederfreigeben einfach einen neuen Commit auf `main` pushen (oder den Workflow unter
*Actions → Build & Deploy → Run workflow* manuell starten).

## Tägliche Erinnerung (Web Push)

Lerner können auf „Ich“ eine tägliche Erinnerung einschalten. Dafür ist nichts einzurichten:
Das VAPID-Schlüsselpaar wird beim ersten Gebrauch als `/data/vapid_private.pem` erzeugt.
Voraussetzungen: HTTPS (`BASE_URL`), `TZ` passend zu den Nutzern (Uhrzeiten gelten in der
Server-Zeitzone) und ausgehende HTTPS-Verbindungen zu den Push-Diensten
(`*.googleapis.com`, `*.mozilla.com`, `*.push.apple.com`, `*.notify.windows.com`).
Auf iPhone/iPad funktioniert es nur, wenn CrossPoint zum Home-Bildschirm hinzugefügt wurde.

## Backups und Restore

Die App erstellt nächtlich ab 3:00 Uhr ein Backup unter `/data/backups/<datum>/`
(`app.db` per SQLite-Backup-API, Collections als `.tar.gz`, Web-Push-Schlüssel
`vapid_private.pem`), Rotation nach
`BACKUP_KEEP_DAYS`. Da `/data` auf einem Host-Pfad liegt, erfasst es zusätzlich die
bestehende Server-Sicherung.

Restore:
1. Stack in Portainer stoppen.
2. Inhalt von `DATA_PATH` sichern (`tar czf /root/crosspoint-vorher.tgz /srv/crosspoint`).
3. Gewünschtes Backup entpacken und `app.db`, `collections/` sowie `vapid_private.pem`
   nach `DATA_PATH` zurückkopieren; Besitzer `1000:1000`.
4. Stack starten und `GET /healthz` prüfen.

## Lokal testen

```bash
docker build -t crosspoint:local .
docker run --rm crosspoint:local python spike/check_anki.py
docker compose -f docker-compose.dev.yml up --build   # http://localhost:8000
```

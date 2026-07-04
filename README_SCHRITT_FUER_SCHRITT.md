# Opa Peters Bestellsystem – GitHub + Vercel

## Wichtig
Lade bei GitHub den Inhalt dieses Ordners hoch. Die Dateien müssen direkt im GitHub-Repository liegen.

Richtig:

- api/index.py
- app.py
- requirements.txt
- pyproject.toml
- vercel.json
- static/style.css

Falsch:

- opa-peters-github-vercel-final/api/index.py

Wenn GitHub oben nur einen Ordner `opa-peters-...` zeigt, ist es falsch hochgeladen.

## Schritt 1: ZIP entpacken
ZIP entpacken und den Ordner öffnen.

## Schritt 2: GitHub Repository leeren
Im alten Repository alle alten Dateien löschen oder ein komplett neues Repository erstellen.

Empfohlen: Neues Repository erstellen, zum Beispiel:

`opa-peters-bestellsystem-neu`

## Schritt 3: Dateien hochladen
In GitHub auf `Add file` → `Upload files` klicken.

Dann NICHT die ZIP-Datei hochladen, sondern alle Dateien und Ordner aus dem entpackten Ordner markieren und hochladen:

- api
- static
- app.py
- requirements.txt
- pyproject.toml
- vercel.json
- settings.json
- bestellsystem.db
- .gitignore
- README_SCHRITT_FUER_SCHRITT.md

Danach `Commit changes` klicken.

## Schritt 4: Vercel Projekt neu verbinden
In Vercel am besten ein neues Projekt erstellen:

`Add New` → `Project` → GitHub Repository auswählen → `Deploy`

## Schritt 5: Environment Variables setzen
In Vercel unter Settings → Environment Variables setzen:

- ADMIN_PIN = deine Admin-PIN
- APP_SECRET = langer geheimer Text

Danach nochmal `Redeploy` starten.

## Fehler: No python entrypoint found
Dieser Fehler kommt meistens, wenn die Dateien in einem Unterordner liegen. Prüfe in GitHub:

Richtig: `api/index.py` direkt im Repository.

Falsch: `opa-peters-github-vercel-final/api/index.py`.

Diese ZIP enthält zusätzlich `pyproject.toml` mit:

`[tool.vercel] entrypoint = "api.index:handler"`

Damit weiß Vercel zusätzlich genau, welche Python-Datei gestartet werden soll.

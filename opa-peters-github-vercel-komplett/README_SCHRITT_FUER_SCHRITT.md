# Opa Peters Bestellsystem – GitHub + Vercel

Diese ZIP ist komplett vorbereitet für einen kostenlosen Test über GitHub und Vercel.

Wichtig: Vercel ist hier nur für Testbetrieb gedacht. Die App nutzt SQLite, PDF-Dateien und Upload-Ordner. Auf Vercel werden diese Daten serverless unter `/tmp` gespeichert und können nach Redeploy/Neustart verloren gehen.

## 1. ZIP entpacken

Die ZIP-Datei auf dem Mac entpacken.

Du solltest diese Struktur sehen:

```text
api/
static/
app.py
requirements.txt
settings.json
bestellsystem.db
vercel.json
.gitignore
README_SCHRITT_FUER_SCHRITT.md
```

## 2. Neues GitHub Repository erstellen

1. GitHub öffnen.
2. Oben rechts auf `+` klicken.
3. `New repository` auswählen.
4. Name eintragen: `opa-peters-bestellsystem`.
5. Am besten `Private` auswählen.
6. Kein README, kein .gitignore und keine License hinzufügen.
7. `Create repository` klicken.

## 3. Dateien zu GitHub hochladen

1. Im neuen Repository auf `Add file` klicken.
2. `Upload files` auswählen.
3. Den Inhalt aus dem entpackten Ordner hineinziehen.

Nicht die ZIP-Datei hochladen, sondern den Inhalt:

```text
api/
static/
app.py
requirements.txt
settings.json
bestellsystem.db
vercel.json
.gitignore
README_SCHRITT_FUER_SCHRITT.md
```

4. Unten auf `Commit changes` klicken.

## 4. Vercel mit GitHub verbinden

1. Vercel öffnen.
2. Mit GitHub anmelden.
3. `Add New` → `Project` klicken.
4. Das Repository `opa-peters-bestellsystem` auswählen.
5. Bei Framework Preset am besten `Other` lassen.

## 5. Environment Variables eintragen

Vor dem Deploy in Vercel den Bereich `Environment Variables` öffnen.

Diese Werte eintragen:

```text
ADMIN_PIN=1234
APP_SECRET=opa-peters-geheimer-schluessel-bitte-spaeter-aendern
```

Du kannst `ADMIN_PIN` direkt auf eine neue PIN setzen.

## 6. Deploy starten

1. Auf `Deploy` klicken.
2. Warten, bis Vercel fertig ist.
3. Danach bekommst du eine Adresse wie:

```text
https://opa-peters-bestellsystem.vercel.app
```

## 7. Testen

Startseite:

```text
https://dein-projekt.vercel.app
```

Adminbereich:

```text
https://dein-projekt.vercel.app/admin/login
```

Admin-PIN ist der Wert aus `ADMIN_PIN`.

## 8. Wenn der alte Runtime-Fehler kommt

Dann ist wahrscheinlich noch eine alte `vercel.json` in GitHub.

Die richtige `vercel.json` muss exakt so aussehen:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "rewrites": [
    {
      "source": "/(.*)",
      "destination": "/api/index"
    }
  ]
}
```

Es darf kein Block mit `runtime` enthalten sein.

## 9. Wichtiger Hinweis

Für echte dauerhafte Nutzung später besser Render mit persistentem Speicher, Cloud Run mit Datenbank oder ein kleiner eigener Server. Vercel ist hier nur zum Online-Testen geeignet.

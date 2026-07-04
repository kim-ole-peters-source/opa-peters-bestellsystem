# Opa Peters Bestellsystem — Vercel-Testpaket

Dieses Paket ist für deinen gewünschten Weg vorbereitet:

1. Code bei GitHub speichern
2. GitHub mit Vercel verbinden
3. kostenlos testweise online hosten

## Inhalt

- `app.py` — deine Python-Web-App, für Vercel leicht angepasst
- `api/index.py` — Einstiegspunkt für Vercel Python Functions
- `vercel.json` — Vercel-Konfiguration mit Rewrite auf die Python-App
- `requirements.txt` — Python-Abhängigkeiten
- `settings.json` und `bestellsystem.db` — Startdaten
- `static/` — CSS, JavaScript, Service Worker und Icons

## Wichtig zu Vercel

Vercel ist hier nur für den Test gedacht. Lokale Schreibdaten wie Datenbank, PDFs, Uploads und Zeiterfassungs-Exports werden in `/tmp` gespeichert. Das ist nicht dauerhaft. Nach einem Neustart, Cold Start oder Redeploy können diese Testdaten verloren gehen.

Für echten Betrieb später besser: Render/Railway/Fly.io mit persistentem Speicher oder Umbau auf externe Datenbank und externen Dateispeicher.

## GitHub Upload

1. ZIP entpacken.
2. Bei GitHub ein neues Repository erstellen, z. B. `opa-peters-bestellsystem`.
3. Alle Dateien aus diesem Ordner hochladen.
4. Änderungen committen.

## Vercel Deploy

1. Bei Vercel anmelden.
2. `Add New` → `Project`.
3. GitHub-Repository auswählen.
4. Framework Preset: `Other` oder automatisch erkennen lassen.
5. Deploy starten.

## Environment Variables in Vercel

In Vercel unter Project → Settings → Environment Variables setzen:

```text
ADMIN_PIN=deine-neue-admin-pin
APP_SECRET=ein-sehr-langer-geheimer-zufallswert
```

Optional für Standort-PINs:

```text
BUYER_PIN_FILIALISTEN_EIS=1111
BUYER_PIN_FILIALISTEN_FEINE_KOST=2222
BUYER_PIN_PRODUZENTEN=3333
BUYER_PIN_BOSS=4444
```

## Aufrufen

Nach dem Deploy bekommst du eine URL wie:

```text
https://dein-projekt.vercel.app
```

Adminbereich:

```text
https://dein-projekt.vercel.app/admin/login
```

## Lokaler kurzer Test

Die normale lokale Version startest du weiterhin mit:

```bash
python -m pip install -r requirements.txt
python app.py
```

Dann öffnen:

```text
http://localhost:8000
```

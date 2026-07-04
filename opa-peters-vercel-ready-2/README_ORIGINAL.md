# Internes Bestellsystem

Ein einfaches lokales Webprogramm für interne Warenbestellungen zwischen Filialen und Zentrale.

## Start lokal

```bash
cd internes_bestellsystem
python -m pip install -r requirements.txt
python app.py
```

Danach im Browser öffnen:

```text
http://localhost:8000
```

Adminbereich:

```text
http://localhost:8000/admin/login
```

Standard-Admin-PIN:

```text
1234
```

## Besteller-Logins

| Bereich | PIN |
|---|---:|
| Filialisten Eis | 1111 |
| Filialisten Feine Kost | 2222 |
| Produzenten | 3333 |
| Boss | 4444 |

Die PINs können beim Start über Umgebungsvariablen geändert werden.

## Funktionen

- Bestellbereich mit Login
- Produkte mit Bild, Name, Gebindegröße und Kategorie
- Produktsuche im Bestellbereich
- Sortierung im Bestellbereich
- Kategorie-Filter im Bestellbereich
- Pflichtfelder Standort und Bestellername
- Freitextfeld für Bemerkungen
- PDF-Erstellung nach Bestellung
- PDF-Sortierung nach Bezugsquelle
- Adminbereich mit Produktsuche
- Kategorie-Verwaltung
- Produktanlage mit Kategorie-Auswahl
- Sichtbarkeit pro Produkt nach Bestellergruppe
- CSV-Import für Produkte
- Excel-Import für Produkte über `.xlsx`
- Versand-Einstellungen für E-Mail und WhatsApp-Link
- Standortverwaltung

## Kategorien verwalten

Im Adminbereich gibt es den Menüpunkt **Kategorien**.

Dort können Kategorien erstellt und deaktiviert werden. Neu erstellte Kategorien erscheinen anschließend beim Anlegen eines Produktes als Auswahlfeld.

## Produkte importieren

Im Adminbereich gibt es den Menüpunkt **Import**.

Unterstützt werden:

- CSV-Dateien
- Excel-Dateien im Format `.xlsx`

Die Datei braucht eine Kopfzeile. Erkannt werden diese Spalten:

| Spalte | Pflicht | Beispiel |
|---|---:|---|
| Produktname oder name | Ja | Kaffeebecher |
| Gebindegröße oder package_size | Ja | Karton à 100 Stück |
| Bezugsquelle oder source | Ja | Großhandel A |
| Kategorie | Nein | Verbrauchsmaterial |
| Sichtbar für oder visible_to | Nein | Filialisten Eis, Boss |

Wenn die Kategorie in der Importdatei noch nicht existiert, wird sie automatisch angelegt.

Wenn die Sichtbarkeit leer ist, wird die im Importformular ausgewählte Standard-Sichtbarkeit verwendet.

Eine CSV-Vorlage kann im Adminbereich unter **Import** heruntergeladen werden.

## E-Mail und WhatsApp

Unter **Admin > Einstellungen** können eingetragen werden:

- E-Mail-Adresse für Bestellungen
- WhatsApp-Kontakt / Nummer

WhatsApp wird als klickbarer Link mit vorbereiteter Nachricht geöffnet. Ein komplett automatischer WhatsApp-Versand ist nur über die offizielle WhatsApp Business API möglich.

Damit E-Mail automatisch verschickt wird, müssen zusätzlich SMTP-Daten gesetzt werden:

```bash
SMTP_HOST=smtp.example.com \
SMTP_PORT=587 \
SMTP_USER=benutzer@example.com \
SMTP_PASSWORD=passwort \
SMTP_FROM=benutzer@example.com \
python app.py
```

## Wichtige Sicherheitshinweise vor Onlinebetrieb

Vor einem echten Einsatz sollten geändert werden:

- Admin-PIN
- Besteller-PINs
- APP_SECRET
- SMTP-Zugangsdaten
- Hosting mit HTTPS
- regelmäßige Datensicherung

Beispiel zum Ändern der Admin-PIN:

```bash
ADMIN_PIN=9876 python app.py
```

## Programm beenden

Im Terminal:

```text
STRG + C
```

## Nutzung als App / PWA

Diese Version ist als Progressive Web App vorbereitet. Das bedeutet: Die Anwendung läuft weiterhin über den Browser, kann aber auf Smartphone, Tablet oder Desktop wie eine App installiert werden.

### iPhone / iPad

1. Safari öffnen.
2. Adresse des Systems öffnen, lokal zum Beispiel `http://localhost:8000` oder später die Server-Adresse.
3. Teilen-Symbol antippen.
4. **Zum Home-Bildschirm** auswählen.
5. Namen bestätigen.

### Android

1. Chrome öffnen.
2. Adresse des Systems öffnen.
3. Menü mit den drei Punkten öffnen.
4. **App installieren** oder **Zum Startbildschirm hinzufügen** wählen.

### Desktop Chrome / Edge

1. Adresse des Systems öffnen.
2. In der Adressleiste oder im Menü auf **Installieren** klicken.

Hinweis: Für die Installation auf echten Geräten sollte die App später über HTTPS laufen. Lokal funktioniert das Testen über `localhost`.

## Design

Das Design wurde an den verspielten, warmen Markenauftritt von Opa Peters angelehnt: cremefarbener Hintergrund, kräftige Akzentfarben, abgerundete Karten, mobile Bedienung und App-Icon im Eis-Stil.

## Version: Mobile App Design

Diese Version wurde für die Nutzung auf iPad und Handy optisch überarbeitet:

- moderne App-Optik mit großen Touch-Flächen
- untere Navigation auf kleinen Bildschirmen
- kompakte Produktkarten für Handy
- größere Eingabefelder für Mengen und Formulare
- ruhigeres, moderneres Design
- weiterhin als PWA installierbar

Start wie bisher:

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Dann im Browser öffnen:

```text
http://localhost:8000
```

## Optimierungen in dieser Version

- Admin-Sitzung wird signiert gespeichert, die Admin-PIN steht nicht mehr im Browser-Cookie.
- Login-Cookies nutzen `HttpOnly` und `SameSite=Lax`.
- Uploads sind begrenzt: Produktbilder maximal 6 MB, Importdateien maximal 5 MB.
- Der Import akzeptiert nur noch `.csv` und `.xlsx`.
- Adminbereich zeigt Kennzahlen für aktive/deaktivierte Produkte und letzte Bestellungen.
- Nach Speichern, Importieren und Deaktivieren erscheinen klare Erfolg- oder Fehlermeldungen.
- Deaktivieren von Produkten und Kategorien fragt vorher nach.
- Die Bestellseite zeigt beim Ausfüllen die Anzahl gewählter Positionen.

## Neue Funktionen

- Standorte können im Adminbereich einzeln bearbeitet werden.
- Jeder Standort hat einen Standortnamen und optional ein eigenes Passwort.
- Die Standorte sind gleichzeitig die Zugangsdaten für die Bestellseite.
- Das Standortpasswort wird direkt beim Login zur Bestellseite geprüft.
- Die Produktsichtbarkeit ist mit den Standorten synchronisiert.
- Produkte können für einzelne Standorte oder für alle Standorte sichtbar sein.
- Im Importbereich gibt es keine Standort-Sichtbarkeit mehr; importierte Produkte sind zunächst für alle Standorte sichtbar.
- Die Kopfzeile zeigt nur noch **Bestellen** und **Admin**.
- Bestellungen können optional ein Bild enthalten; Galerie oder Kamera wird über ein Dropdown ausgewählt.
- Bestellbilder erscheinen im Adminbereich und werden bei SMTP-Versand zusätzlich als Bildanhang verschickt.
- Auf Apple-Geräten zeigt der Button **App installieren** eine Anleitung für **Teilen > Zum Home-Bildschirm**.

## Version: Zeiterfassung und Touch-Login

Diese gespeicherte Version wurde zusätzlich erweitert:

- Produkte können bearbeitet und gelöscht werden.
- Im Adminbereich gibt es eigene Reiter für **Bestellungen** und **Zeiterfassung**.
- Standorte können eine bestellende Person, Zeiterfassung und eine maximale Endzeit erhalten.
- Standort-Logins mit aktivierter Zeiterfassung zeigen zuerst eine Auswahlseite.
- Zeiterfassung speichert nur den aktuellen Tag und verhindert Zukunftsendzeiten.
- Anfangs- und Endzeiten werden in 15-Minuten-Schritten ausgewählt.
- Admins können Zeiteinträge nachträglich bearbeiten; Stunden werden neu berechnet.
- Monatsübersichten zeigen Summen je Mitarbeiter, Einsatzort und Mitarbeiter/Einsatzort.
- Monats-Export ist als Excel-Datei möglich.
- Automatischer Monats-Export per E-Mail wird am Monatsende ab 23:55 Uhr versucht, wenn SMTP-Daten gesetzt sind.
- Admin-PIN kann dauerhaft unter **Admin > Einstellungen** geändert werden.
- Login-PINs erhalten ein großes Zahlenfeld für iPad und Handy.
- Passwortfelder können per Button angezeigt oder verborgen werden.
- Das Logo wurde aus der bereitgestellten Bilddatei neu eingebunden.

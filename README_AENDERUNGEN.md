# Änderungen in dieser Version

Diese Version enthält die gewünschten Anpassungen:

1. **PDF öffnen / drucken repariert**
   - PDFs aus neuen Bestellungen können nach dem Absenden direkt über **PDF öffnen / drucken** geöffnet werden.
   - Die PDF-Ausgabe funktioniert auch auf Vercel-Testbetrieb mit dem `/tmp`-Speicher.

2. **Oberer Reiter verschwindet nach Anmeldung**
   - Nach Standort-Login oder Admin-Login wird die obere Umschaltung zwischen **Bestellen** und **Admin** ausgeblendet.

3. **Suchleiste im Bestellmenü einfacher**
   - Die Bestellansicht hat jetzt eine einfache Produktsuche mit Kategorie-Auswahl und Reset.
   - Die zusätzliche Sortierauswahl wurde aus der Bestellansicht entfernt.

4. **Bestelldaten in den Warenkorb verschoben**
   - Name, Bemerkung und optionales Bild stehen jetzt im Warenkorb-Fenster.
   - In der Produktansicht sind nur noch Produkte und Mengen sichtbar.

5. **Moderneres Design**
   - Farben stärker am Logo orientiert: warmes Orange/Gelb, Rot-Akzent und Blau/Grau.
   - Karten, Buttons, Warenkorb und Suchbereich wurden optisch modernisiert.

## Wichtig für Vercel

Vercel ist weiterhin nur für Testbetrieb empfohlen. SQLite-Datenbank, Uploads und PDFs werden dort nicht dauerhaft sicher gespeichert.
Für echten Betrieb später besser Render/Railway/Server mit persistentem Speicher nutzen.

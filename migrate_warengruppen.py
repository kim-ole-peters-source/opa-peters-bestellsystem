#!/usr/bin/env python3
"""Sortiert die Produkte in feinere Warengruppen ein.

Hintergrund: 199 von 340 Produkten lagen in der Gruppe "Produktion".
Dieses Programm teilt sie anhand der Produktnamen auf und legt
dabei gleichnamige Gruppen zusammen.

    python3 migrate_warengruppen.py              nur anzeigen, nichts ändern
    python3 migrate_warengruppen.py --anwenden   wirklich ändern

Vor dem Ändern wird immer eine Kopie der Datenbank angelegt.
Die Gruppen selbst legt das Bestellsystem beim nächsten Start von
allein an - dafür ist hier nichts zu tun.
"""

import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime

def datenordner():
    """Findet den Ordner mit der Datenbank, egal wie das Programm gestartet wird."""
    if os.environ.get("DATA_DIR"):
        return os.environ["DATA_DIR"]
    for kandidat in ("/var/lib/opa-peters", os.path.dirname(os.path.abspath(__file__))):
        if os.path.exists(os.path.join(kandidat, "bestellsystem.db")):
            return kandidat
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = datenordner()
DB_PATH = os.path.join(DATA_DIR, "bestellsystem.db")

# Gruppen, die dasselbe meinen, werden zusammengelegt
ZUSAMMEN = {
    # Komma im Namen wird vom Bestellsystem als Trennung gelesen -> umbenannt
    "Milch, Sahne & Eier": "Milchprodukte & Eier",
    "Verpackung": "Becher & Verpackung",
    "Soße": "Variegato & Saucen",
    "Milch & Sahne": "Milchprodukte & Eier",
    "Allgemein": "Divers",
    "Kaffe & Co": "Kaffee & Co",
    "Kaffeetasse": "Kaffee & Co",
}

# Einzelentscheidungen, die sich nicht aus einer Regel ergeben
EINZELN = [
    (r"FAB Superpanna|Pulisano Bourbon-Vanille", "Aromen & Pasten"),
    (r"FAB Deli|FAB Vari|IC Joycream|^M'3 |PREG Salty", "Variegato & Saucen"),
    (r"FAB Topping", "Toppings"),
    (r"Gastro Joghurt", "Milchprodukte & Eier"),
    (r"IC Codetta", "Schokolade & Kuvertüre"),
    (r"IC Joyfruit", "Früchte & Füllungen"),
    (r"Haselnüsse", "Nüsse & Marzipan"),
    (r"Trinkhalme", "Becher & Verpackung"),
    (r"^ProHaag|^WEPA", "Reinigung"),
    (r"Senf", "Divers"),
]

# Tiefgekühltes wird nach Warenart einsortiert, nicht pauschal unter TK.
# Geprüft wird der Name ohne das vorangestellte "TK-".
TIEFGEKUEHLT = [
    (r"brötchen|brownie|cookie|berliner|plunder|teig|kuchen|croissant|puck|backshop|muffin|donut",
     "Backwaren"),
    (r"himbeer|erdbeer|kirsch|pflaume|heidelbeer|beere|mango|ananas|melone|pfirsich|frucht|rhabarber",
     "Früchte & Füllungen"),
    (r"saft|zitrone", "Backzutaten"),
]

# Regeln für die Gruppe "Produktion", der Reihe nach geprüft
REGELN = [
    ("Tüten & Waffeln",        r"tüte|hörnchen|waffel|krossi|cornet"),
    ("Becher & Verpackung",    r"eisbecher|clear cup|becher|deckel|löffel|spatel|tray|karton|beutel|folie|serviette|papier|verpackung|etikett|aufkleber|druck"),
    ("Aromen & Pasten",        r"aroma|pasta |paste|3d |^ba |supra|europa|basis|fondant|gelat|base |mella|sonnengold|germa|rühr|krem"),
    ("Variegato & Saucen",     r"variegat|varieg|marmoria|soße|sauce|ketchup|sirup|snackoloso|chiarafrutta|pulpe"),
    ("Früchte & Füllungen",    r"amarena|kirsch|füllung|konfit|frucht|heidelbeer|himbeer|mandarine|passions|walnuss|grütze|mango|zitronenschale|belegkirschen"),
    ("Schokolade & Kuvertüre", r"schoko|glasur|callebaut|dobla|hohlkugel|kuvert|chips weiss|reno|gianduja|nougaty|mabu"),
    ("Nüsse & Marzipan",       r"marzipan|mandel|nuss|nuß|krokant|haselnuss|nonpareille|pistaz|moccabohne"),
    ("Kakao",                  r"kakao|cacao"),
    ("Backmischungen",         r"mischung|meister back|delikat|mürb|backmischung"),
    ("Backzutaten",            r"zucker|mehl|backpulver|honig|essig|salz|stärke|glukose|glucodry|zitronensäure|zimt|gewürz|raps-öl|sahnegrieß"),
    ("Milchprodukte & Eier",    r"sahne|milch|eier|frischkäse|mascarpone|quark|butter|oatly|haferdrink"),
    ("Getränke",               r"schorle|saft|wasser|gerolsteiner|\bvio\b|rauch|cola|limo"),
    ("Süßwaren",               r"haribo|bueno|plätzchen|amaretti|keks|smarties|oreo|stange hoche"),
    ("Geräte & Zubehör",       r"portionierer|zange|schaufel|behälter|schild|aufsteller"),
]


# Gruppen, die durch die Umsortierung leer geworden sind und wegkommen
LEER_GEWORDEN = ["Produktion", "Verpackung", "Soße", "Milch & Sahne", "Milch", "Sahne & Eier",
                 "Milch, Sahne & Eier", "Allgemein, Verpackung", "Allgemein, TK", "Allgemein, Reinigung",
                 "Kaffe & Co", "Kaffeetasse"]

TRENNZEICHEN = re.compile(r"[,;|/\n]")


def neue_gruppe(name, alt):
    alt = (alt or "").replace("Allgemein, ", "").strip()
    if alt != "Produktion" and not re.match(r"\s*TK-", name, re.I):
        return ZUSAMMEN.get(alt, alt)
    if re.match(r"\s*TK-", name, re.I):
        rest = re.sub(r"^\s*TK-", "", name, flags=re.I).lower()
        for muster, gruppe in TIEFGEKUEHLT:
            if re.search(muster, rest):
                return gruppe
        return "TK"
    for muster, gruppe in EINZELN:
        if re.search(muster, name, re.I):
            return gruppe
    n = name.lower()
    for gruppe, muster in REGELN:
        if re.search(muster, n):
            return gruppe
    return None   # konnte nicht zugeordnet werden


def gruppen_aufraeumen(con):
    """Leer gewordene alte Gruppen ausblenden (nur wenn kein Produkt sie mehr nutzt)."""
    benutzt = set()
    for (kat,) in con.execute("SELECT category FROM products WHERE deleted_at IS NULL"):
        for teil in TRENNZEICHEN.split(kat or ""):
            if teil.strip():
                benutzt.add(teil.strip().lower())
    jetzt = datetime.now().isoformat(timespec="seconds")
    weg = []
    with con:
        for name in LEER_GEWORDEN:
            if name.lower() in benutzt:
                continue
            n = con.execute(
                "UPDATE categories SET active=0, deleted_at=? WHERE lower(name)=lower(?) AND deleted_at IS NULL",
                (jetzt, name),
            ).rowcount
            if n:
                weg.append(name)
    if weg:
        print("Leere alte Gruppen ausgeblendet:", ", ".join(weg))


def main():
    anwenden = "--anwenden" in sys.argv
    if not os.path.exists(DB_PATH):
        sys.exit(f"Datenbank nicht gefunden: {DB_PATH}")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    produkte = con.execute(
        "SELECT id, name, category FROM products WHERE deleted_at IS NULL"
    ).fetchall()

    alle_ziele = [g for g, _ in REGELN] + [g for _, g in EINZELN] + [g for _, g in TIEFGEKUEHLT]
    for gruppe in alle_ziele + list(ZUSAMMEN.values()):
        assert not TRENNZEICHEN.search(gruppe), f"Gruppenname mit Trennzeichen: {gruppe}"

    aenderungen, offen = [], []
    for p in produkte:
        neu = neue_gruppe(p["name"], p["category"])
        if neu is None:
            offen.append(p["name"])
        elif neu != p["category"]:
            aenderungen.append((p["id"], p["name"], p["category"], neu))

    print(f"Datenbank : {DB_PATH}")
    print(f"Produkte  : {len(produkte)}")
    print(f"Änderungen: {len(aenderungen)}")
    print(f"Unklar    : {len(offen)}  (bleiben wie sie sind)")
    print()

    zaehler = Counter(x[3] for x in aenderungen)
    print("Neue Verteilung der geänderten Produkte:")
    for gruppe, n in zaehler.most_common():
        print(f"  {n:4}  {gruppe}")
    if offen:
        print("\nNicht zugeordnet:")
        for n in offen[:20]:
            print("  ", n)
        if len(offen) > 20:
            print(f"   … und {len(offen)-20} weitere")

    if not anwenden:
        print("\nEs wurde nichts geändert.")
        print("Zum Übernehmen:  python3 migrate_warengruppen.py --anwenden")
        return

    if not aenderungen:
        print("\nKeine Produkte zu ändern.")
        gruppen_aufraeumen(con)
        return

    kopie = os.path.join(
        DATA_DIR, f"bestellsystem_vor_warengruppen_{datetime.now():%Y-%m-%d_%H-%M}.db"
    )
    shutil.copy2(DB_PATH, kopie)
    print(f"\nKopie angelegt: {kopie}")

    with con:
        con.executemany(
            "UPDATE products SET category=? WHERE id=?",
            [(neu, pid) for pid, _, _, neu in aenderungen],
        )
    print(f"{len(aenderungen)} Produkte umsortiert.")
    gruppen_aufraeumen(con)
    print("Bitte den Dienst neu starten, damit die Gruppen angelegt werden:")
    print("  systemctl restart opa-peters")


if __name__ == "__main__":
    main()

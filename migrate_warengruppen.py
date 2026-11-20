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

DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DATA_DIR, "bestellsystem.db")

# Gruppen, die dasselbe meinen, werden zusammengelegt
ZUSAMMEN = {
    "Verpackung": "Becher & Verpackung",
    "Soße": "Variegato & Saucen",
    "Milch & Sahne": "Milch, Sahne & Eier",
    "Allgemein": "Divers",
}

# Einzelentscheidungen, die sich nicht aus einer Regel ergeben
EINZELN = [
    (r"FAB Superpanna|Pulisano Bourbon-Vanille", "Aromen & Pasten"),
    (r"FAB Deli|FAB Vari|IC Joycream|^M'3 |PREG Salty", "Variegato & Saucen"),
    (r"FAB Topping", "Toppings"),
    (r"Gastro Joghurt", "Milch, Sahne & Eier"),
    (r"IC Codetta", "Schokolade & Kuvertüre"),
    (r"IC Joyfruit", "Früchte & Füllungen"),
    (r"Haselnüsse", "Nüsse & Marzipan"),
    (r"Trinkhalme", "Becher & Verpackung"),
    (r"^ProHaag|^WEPA", "Reinigung"),
    (r"Senf", "Divers"),
    (r"^TK-", "TK"),
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
    ("Milch, Sahne & Eier",    r"sahne|milch|eier|frischkäse|mascarpone|quark|butter|oatly|haferdrink"),
    ("Getränke",               r"schorle|saft|wasser|gerolsteiner|\bvio\b|rauch|cola|limo"),
    ("Süßwaren",               r"haribo|bueno|plätzchen|amaretti|keks|smarties|oreo|stange hoche"),
    ("Geräte & Zubehör",       r"portionierer|zange|schaufel|behälter|schild|aufsteller"),
]


def neue_gruppe(name, alt):
    alt = (alt or "").replace("Allgemein, ", "").strip()
    if alt != "Produktion":
        return ZUSAMMEN.get(alt, alt)
    for muster, gruppe in EINZELN:
        if re.search(muster, name, re.I):
            return gruppe
    n = name.lower()
    for gruppe, muster in REGELN:
        if re.search(muster, n):
            return gruppe
    return None   # konnte nicht zugeordnet werden


def main():
    anwenden = "--anwenden" in sys.argv
    if not os.path.exists(DB_PATH):
        sys.exit(f"Datenbank nicht gefunden: {DB_PATH}")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    produkte = con.execute(
        "SELECT id, name, category FROM products WHERE deleted_at IS NULL"
    ).fetchall()

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
        print("\nNichts zu tun.")
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
    print("Bitte den Dienst neu starten, damit die Gruppen angelegt werden:")
    print("  systemctl restart opa-peters")


if __name__ == "__main__":
    main()

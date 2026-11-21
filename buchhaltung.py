"""
Buchhaltung für das Bestellsystem Opa Peters.

Sammelt Rechnungen aus Postfächern (und später weiteren Quellen), liest sie aus,
benennt sie einheitlich, gleicht sie mit Kontoauszügen ab und erstellt den
Monatsexport (Rechnungseingang / Rechnungsausgang / Fehlliste / Abgleichliste).

Grundsätze:
- Originale werden nie verändert oder gelöscht (GoBD). Geändert werden nur die
  Begleitdaten; jede Änderung wird protokolliert.
- Postfächer werden nur gelesen (EXAMINE / BODY.PEEK) – nichts wird verschoben,
  gelöscht oder als gelesen markiert.
- Zugangsdaten der Quellen werden verschlüsselt gespeichert.
"""

import csv
import email
import hashlib
import imaplib
import io
import json
import os
import re
import sqlite3
import threading
import traceback
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    BERLIN = None

BASIS = ""
ARCHIV = ""
DB_PFAD = ""
SCHLUESSEL_PFAD = ""
_lauf_lock = threading.Lock()
LAUF = {"laeuft": False, "start": "", "ende": "", "meldung": "", "neu": 0}

ARTEN = {"ER": "Eingangsrechnung", "AR": "Ausgangsrechnung", "GS": "Gutschrift"}
STATUS_TEXT = {"neu": "zu prüfen", "geprueft": "geprüft", "portal": "im Portal holen", "ignoriert": "kein Beleg", "doppelt": "doppelt"}

# Bekannte Anbieter für Postfächer (Host, Port) – "Andere" = selbst eintragen
ANBIETER = {
    "strato": ("Strato", "imap.strato.de", 993),
    "ionos": ("IONOS / 1&1", "imap.ionos.de", 993),
    "gmail": ("Gmail / Google Workspace", "imap.gmail.com", 993),
    "outlook": ("Outlook / Microsoft 365", "outlook.office365.com", 993),
    "gmx": ("GMX", "imap.gmx.net", 993),
    "webde": ("WEB.DE", "imap.web.de", 993),
    "telekom": ("T-Online", "secureimap.t-online.de", 993),
    "andere": ("Anderer Anbieter", "", 993),
}

# Quellenarten. Neue Arten (z. B. PayPal, Kasse) werden hier ergänzt.
QUELLEN_ARTEN = {
    "imap": "E-Mail-Postfach",
}

RECHNUNGS_WORTE = re.compile(r"rechn|invoice|beleg|gutschrift|faktur|receipt|quittung|abrechnung|bill\b|zahlungsbest", re.I)


# ---------------------------------------------------------------- Grundlagen

def jetzt():
    if BERLIN:
        return datetime.now(BERLIN).replace(tzinfo=None)
    return datetime.now()


def jetzt_text():
    return jetzt().strftime("%Y-%m-%d %H:%M:%S")


def init(data_dir):
    global BASIS, ARCHIV, DB_PFAD, SCHLUESSEL_PFAD
    BASIS = os.path.join(data_dir, "buchhaltung")
    ARCHIV = os.path.join(BASIS, "archiv")
    DB_PFAD = os.path.join(BASIS, "buchhaltung.db")
    SCHLUESSEL_PFAD = os.path.join(BASIS, "zugang.key")
    os.makedirs(ARCHIV, exist_ok=True)
    os.makedirs(os.path.join(BASIS, "export"), exist_ok=True)
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS quellen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            art TEXT NOT NULL DEFAULT 'imap',
            anbieter TEXT DEFAULT '',
            host TEXT DEFAULT '',
            port INTEGER DEFAULT 993,
            benutzer TEXT DEFAULT '',
            geheim TEXT DEFAULT '',
            ordner TEXT DEFAULT 'INBOX',
            alles_ist_beleg INTEGER DEFAULT 0,
            richtung TEXT DEFAULT 'auto',
            start_datum TEXT DEFAULT '',
            aktiv INTEGER DEFAULT 1,
            letzter_lauf TEXT DEFAULT '',
            letzter_status TEXT DEFAULT '',
            letzter_fehler INTEGER DEFAULT 0,
            gefunden INTEGER DEFAULT 0,
            erstellt TEXT
        );
        CREATE TABLE IF NOT EXISTS quellen_gesehen (
            quelle_id INTEGER, ordner TEXT, uidvalidity TEXT, uid TEXT,
            PRIMARY KEY (quelle_id, ordner, uidvalidity, uid)
        );
        CREATE TABLE IF NOT EXISTS belege (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT UNIQUE,
            art TEXT DEFAULT 'ER',
            lieferant TEXT DEFAULT '',
            rechnungsnr TEXT DEFAULT '',
            datum TEXT DEFAULT '',
            betrag INTEGER,
            waehrung TEXT DEFAULT 'EUR',
            datei TEXT DEFAULT '',
            original_name TEXT DEFAULT '',
            xml_datei TEXT DEFAULT '',
            mime TEXT DEFAULT '',
            quelle TEXT DEFAULT '',
            quelle_id INTEGER,
            mail_id TEXT DEFAULT '',
            mail_betreff TEXT DEFAULT '',
            mail_von TEXT DEFAULT '',
            mail_datum TEXT DEFAULT '',
            portal_link TEXT DEFAULT '',
            status TEXT DEFAULT 'neu',
            sicher INTEGER DEFAULT 0,
            bar INTEGER DEFAULT 0,
            dauer INTEGER DEFAULT 0,
            dauer_muster TEXT DEFAULT '',
            notiz TEXT DEFAULT '',
            text_auszug TEXT DEFAULT '',
            erstellt TEXT,
            geaendert TEXT
        );
        CREATE INDEX IF NOT EXISTS belege_datum ON belege(datum);
        CREATE TABLE IF NOT EXISTS konten (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            iban TEXT DEFAULT '',
            erstellt TEXT
        );
        CREATE TABLE IF NOT EXISTS buchungen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            konto_id INTEGER,
            datum TEXT,
            betrag INTEGER,
            gegenpartei TEXT DEFAULT '',
            iban_gegen TEXT DEFAULT '',
            zweck TEXT DEFAULT '',
            schluessel TEXT UNIQUE,
            status TEXT DEFAULT 'offen',
            vorschlag_id INTEGER,
            vorschlag_punkte INTEGER DEFAULT 0,
            regel_id INTEGER,
            import_id INTEGER,
            notiz TEXT DEFAULT '',
            erstellt TEXT
        );
        CREATE INDEX IF NOT EXISTS buchungen_datum ON buchungen(datum);
        CREATE TABLE IF NOT EXISTS zuordnung (
            buchung_id INTEGER, beleg_id INTEGER, art TEXT, punkte INTEGER DEFAULT 0, erstellt TEXT,
            PRIMARY KEY (buchung_id, beleg_id)
        );
        CREATE TABLE IF NOT EXISTS regeln (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, muster TEXT, erstellt TEXT
        );
        CREATE TABLE IF NOT EXISTS importe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            konto_id INTEGER, dateiname TEXT, neu INTEGER, doppelt INTEGER, erstellt TEXT
        );
        CREATE TABLE IF NOT EXISTS lieferanten (
            muster TEXT PRIMARY KEY, name TEXT
        );
        CREATE TABLE IF NOT EXISTS monate (
            monat TEXT PRIMARY KEY, abgeschlossen_am TEXT, belege TEXT
        );
        CREATE TABLE IF NOT EXISTS protokoll (
            id INTEGER PRIMARY KEY AUTOINCREMENT, zeit TEXT, art TEXT, text TEXT
        );
        CREATE TABLE IF NOT EXISTS positionen (
            beleg_id INTEGER, nr INTEGER, name TEXT, menge REAL, einheit TEXT DEFAULT '',
            einzelpreis INTEGER, summe INTEGER, herkunft TEXT DEFAULT '',
            PRIMARY KEY (beleg_id, nr)
        );
        CREATE TABLE IF NOT EXISTS positionen_gelesen (beleg_id INTEGER PRIMARY KEY, anzahl INTEGER, herkunft TEXT);
        CREATE TABLE IF NOT EXISTS anforderungen (
            id INTEGER PRIMARY KEY AUTOINCREMENT, buchung_id INTEGER, an TEXT, zeit TEXT, weg TEXT
        );
        CREATE TABLE IF NOT EXISTS einstellungen (
            schluessel TEXT PRIMARY KEY, wert TEXT
        );
        """
    )
    if not einstellung(con, "eigene_namen"):
        setze_einstellung(con, "eigene_namen", "Opa Peters")
    if not einstellung(con, "eigene_domains"):
        setze_einstellung(con, "eigene_domains", "opapeters.de")
    con.commit()
    con.close()


def db():
    con = sqlite3.connect(DB_PFAD, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def einstellung(con, key, standard=""):
    row = con.execute("SELECT wert FROM einstellungen WHERE schluessel=?", (key,)).fetchone()
    return row["wert"] if row else standard


def setze_einstellung(con, key, wert):
    con.execute("INSERT INTO einstellungen (schluessel, wert) VALUES (?, ?) ON CONFLICT(schluessel) DO UPDATE SET wert=excluded.wert", (key, str(wert)))


def protokolliere(con, art, text):
    con.execute("INSERT INTO protokoll (zeit, art, text) VALUES (?, ?, ?)", (jetzt_text(), art, text[:2000]))


def schluessel_text(text):
    text = (text or "").lower()
    for alt, neu in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(alt, neu)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# ---------------------------------------------------------------- Verschlüsselung

def _fernet():
    from cryptography.fernet import Fernet
    if not os.path.exists(SCHLUESSEL_PFAD):
        alte = os.umask(0o077)
        try:
            with open(SCHLUESSEL_PFAD, "wb") as f:
                f.write(Fernet.generate_key())
        finally:
            os.umask(alte)
    with open(SCHLUESSEL_PFAD, "rb") as f:
        return Fernet(f.read().strip())


def verschluesseln(text):
    return _fernet().encrypt((text or "").encode("utf-8")).decode("ascii") if text else ""


def entschluesseln(text):
    return _fernet().decrypt(text.encode("ascii")).decode("utf-8") if text else ""


# ---------------------------------------------------------------- Beträge & Daten

def cent(wert):
    """'1.234,56' / '-12,50' / '12.50' / 12.5 -> Cent (int) oder None"""
    if wert is None:
        return None
    if isinstance(wert, (int, float)):
        return int(round(float(wert) * 100))
    text = str(wert).strip().replace("€", "").replace("EUR", "").replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    minus = text.startswith("-") or text.endswith("-")
    text = text.strip("+-")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        betrag = int(round(float(text) * 100))
    except ValueError:
        return None
    return -betrag if minus else betrag


def betrag_text(cents, waehrung="EUR"):
    if cents is None:
        return "–"
    vorzeichen = "-" if cents < 0 else ""
    cents = abs(int(cents))
    euro = f"{cents // 100:,}".replace(",", ".")
    zeichen = "€" if waehrung == "EUR" else waehrung
    return f"{vorzeichen}{euro},{cents % 100:02d} {zeichen}"


def datum_iso(text):
    text = (text or "").strip().replace(" ", "")
    for kandidat in dict.fromkeys((text, text[:10], text[:8])):
        for muster in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%Y%m%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(kandidat, muster).date().isoformat()
            except ValueError:
                continue
    return ""


def datum_de(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return iso or "–"


def dateiname_teil(text, laenge=40):
    text = (text or "").strip()
    for alt, neu in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss"), ("&", "und")):
        text = text.replace(alt, neu)
    text = re.sub(r"[/\\]+", "-", text)
    text = re.sub(r"[^A-Za-z0-9.\-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    return text[:laenge].strip("-.")


def beleg_dateiname(beleg, endung=None):
    """2026-11-03_ER_Hefe-van-Haag_RE-48213_412-35EUR.pdf"""
    datum = beleg["datum"] or "ohne-Datum"
    lieferant = dateiname_teil(beleg["lieferant"]) or "Unbekannt"
    nummer = dateiname_teil(beleg["rechnungsnr"], 30) or "ohne-Nr"
    if beleg["betrag"] is None:
        betrag = "ohne-Betrag"
    else:
        c = abs(int(beleg["betrag"]))
        betrag = f"{'-' if beleg['betrag'] < 0 else ''}{c // 100}-{c % 100:02d}{beleg['waehrung'] or 'EUR'}"
    if endung is None:
        endung = os.path.splitext(beleg["datei"] or beleg["original_name"] or "x.pdf")[1].lower() or ".pdf"
    return f"{datum}_{beleg['art'] or 'ER'}_{lieferant}_{nummer}_{betrag}{endung}"


# ---------------------------------------------------------------- Belege auslesen

def _lokal(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _finde(element, *pfad):
    """Sucht ein Unterelement über lokale Namen (ohne Namensräume)."""
    aktuelle = [element]
    for name in pfad:
        naechste = []
        for e in aktuelle:
            for kind in e.iter():
                if kind is not e and _lokal(kind.tag) == name:
                    naechste.append(kind)
        aktuelle = naechste
        if not aktuelle:
            return None
    return aktuelle[0]


def _text(element, *pfad):
    e = _finde(element, *pfad)
    return (e.text or "").strip() if e is not None and e.text else ""


def e_rechnung_lesen(xml_bytes):
    """ZUGFeRD/Factur-X (CII) oder XRechnung (UBL/CII) -> dict oder None"""
    try:
        wurzel = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    name = _lokal(wurzel.tag)
    daten = {}
    if name == "CrossIndustryInvoice":
        daten["rechnungsnr"] = _text(wurzel, "ExchangedDocument", "ID")
        datum = _text(wurzel, "ExchangedDocument", "IssueDateTime", "DateTimeString")
        daten["datum"] = datum_iso(datum)
        typ = _text(wurzel, "ExchangedDocument", "TypeCode")
        daten["gutschrift"] = typ in ("381", "261")
        daten["verkaeufer"] = _text(wurzel, "SellerTradeParty", "Name")
        daten["kaeufer"] = _text(wurzel, "BuyerTradeParty", "Name")
        betrag = _text(wurzel, "SpecifiedTradeSettlementHeaderMonetarySummation", "GrandTotalAmount") or \
            _text(wurzel, "SpecifiedTradeSettlementHeaderMonetarySummation", "DuePayableAmount")
        daten["betrag"] = cent(betrag.replace(",", ".")) if betrag else None
        daten["waehrung"] = _text(wurzel, "InvoiceCurrencyCode") or "EUR"
    elif name in ("Invoice", "CreditNote"):
        daten["rechnungsnr"] = next((((e.text or "").strip()) for e in wurzel if _lokal(e.tag) == "ID"), "")
        daten["datum"] = datum_iso(next((((e.text or "").strip()) for e in wurzel if _lokal(e.tag) == "IssueDate"), ""))
        daten["gutschrift"] = name == "CreditNote"
        lieferant = _finde(wurzel, "AccountingSupplierParty")
        kunde = _finde(wurzel, "AccountingCustomerParty")
        daten["verkaeufer"] = (_text(lieferant, "PartyName", "Name") or _text(lieferant, "RegistrationName")) if lieferant is not None else ""
        daten["kaeufer"] = (_text(kunde, "PartyName", "Name") or _text(kunde, "RegistrationName")) if kunde is not None else ""
        betrag = _text(wurzel, "LegalMonetaryTotal", "PayableAmount") or _text(wurzel, "LegalMonetaryTotal", "TaxInclusiveAmount")
        daten["betrag"] = cent(betrag.replace(",", ".")) if betrag else None
        daten["waehrung"] = next((((e.text or "").strip()) for e in wurzel if _lokal(e.tag) == "DocumentCurrencyCode"), "EUR") or "EUR"
    else:
        return None
    daten["sicher"] = bool(daten.get("rechnungsnr") and daten.get("betrag") is not None and daten.get("datum"))
    return daten


BETRAG_MUSTER = r"(-?\d{1,3}(?:[.\s]\d{3})*,\d{2}|-?\d+,\d{2}|-?\d{1,3}(?:,\d{3})*\.\d{2})"
NR_MUSTER = re.compile(
    r"(?:Rechnungs?\s*-?\s*(?:nummer|nr\.?|no\.?)|Invoice\s*(?:No\.?|Number|#)|Beleg\s*-?\s*(?:nummer|nr\.?)|Gutschrift\s*-?\s*(?:nummer|nr\.?)|Re\.?\s*-?\s*Nr\.?)"
    r"\s*[:#.]?\s*([A-Z0-9][A-Za-z0-9\-/_.]{1,30}[A-Za-z0-9])", re.I)
DATUM_MUSTER = re.compile(r"(?:Rechnungsdatum|Belegdatum|Invoice\s*date|Datum|Date)\s*[:.]?\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}|\d{4}-\d{2}-\d{2})", re.I)
SUMME_MUSTER = re.compile(
    r"(?:Gesamtbetrag|Rechnungsbetrag|Endbetrag|Gesamtsumme|Zahlbetrag|zu\s+zahlen(?:der\s+Betrag)?|zahlbarer\s+Betrag|Bruttobetrag|Summe\s+brutto|Brutto\s*gesamt|Gesamt\s*brutto|Total\s*(?:amount|due)?|Amount\s+due|Gesamt)"
    r"[^\d\n-]{0,40}" + BETRAG_MUSTER, re.I)


def text_auslesen(text):
    daten = {"rechnungsnr": "", "datum": "", "betrag": None, "gutschrift": False, "sicher": False}
    if not text:
        return daten
    kopf = text[:3000]
    m = NR_MUSTER.search(text)
    if m:
        daten["rechnungsnr"] = m.group(1).strip(".")
    m = DATUM_MUSTER.search(text)
    if m:
        daten["datum"] = datum_iso(m.group(1).replace(" ", ""))
    else:
        m = re.search(r"\b(\d{1,2}\.\d{1,2}\.20\d{2})\b", text)
        if m:
            daten["datum"] = datum_iso(m.group(1))
    summen = [cent(x.replace(" ", "")) for x in SUMME_MUSTER.findall(text)]
    summen = [s for s in summen if s is not None]
    if summen:
        daten["betrag"] = max(summen, key=abs)
    else:
        euro = [cent(x.replace(" ", "")) for x in re.findall(BETRAG_MUSTER + r"\s*(?:€|EUR)", text)]
        euro = [e for e in euro if e is not None]
        if euro:
            daten["betrag"] = max(euro, key=abs)
    daten["gutschrift"] = bool(re.search(r"\bGutschrift\b|\bCredit\s+note\b|Stornorechnung", kopf, re.I))
    daten["sicher"] = bool(daten["rechnungsnr"] and daten["betrag"] is not None and daten["datum"] and summen)
    return daten


def pdf_lesen(inhalt):
    """-> (text, eingebettetes_xml_bytes_oder_None)"""
    try:
        from pypdf import PdfReader
        leser = PdfReader(io.BytesIO(inhalt))
    except Exception:
        return "", None
    xml = None
    try:
        for name, inhalte in (leser.attachments or {}).items():
            if name.lower().endswith(".xml") and inhalte:
                xml = inhalte[0]
                break
    except Exception:
        xml = None
    teile = []
    try:
        for seite in leser.pages[:4]:
            teile.append(seite.extract_text() or "")
    except Exception:
        pass
    return "\n".join(teile), xml


def ist_eigene_firma(con, name):
    name_k = schluessel_text(name)
    if not name_k:
        return False
    for eigen in (einstellung(con, "eigene_namen") or "").split(","):
        eigen_k = schluessel_text(eigen)
        if eigen_k and eigen_k in name_k:
            return True
    return False


def eigene_domain(con, adresse):
    adresse = (adresse or "").lower()
    return any(d.strip().lower() and adresse.endswith("@" + d.strip().lower()) for d in (einstellung(con, "eigene_domains") or "").split(","))


def lieferant_aus_absender(con, absender):
    """Gelernte Zuordnung Absender/Domain -> Lieferantenname, sonst Anzeigename/Domain."""
    name, adresse = parseaddr(absender or "")
    adresse = adresse.lower()
    domain = adresse.split("@")[-1] if "@" in adresse else ""
    for muster in (adresse, domain):
        if muster:
            row = con.execute("SELECT name FROM lieferanten WHERE muster=?", (muster,)).fetchone()
            if row:
                return row["name"]
    if name:
        name = str(make_header(decode_header(name)))
        name = re.sub(r"\s+(?:GmbH|AG|KG|e\.\s?K\.|Rechnung(?:en)?|Buchhaltung|Invoice|Billing|noreply|no-reply)\b.*$", "", name, flags=re.I).strip(" -|,")
        if name and "@" not in name:
            return name[:60]
    if domain:
        teile = domain.split(".")
        return (teile[-2] if len(teile) >= 2 else domain).capitalize()
    return ""


def beleg_anlegen(inhalt, dateiname, mime="", quelle="Upload", quelle_id=None, mail=None, richtung="auto", alles_ist_beleg=True, xml_dazu=None, vorgaben=None):
    """Speichert einen Beleg im Archiv und liest ihn aus. -> (beleg_id, neu: bool, grund)"""
    mail = mail or {}
    vorgaben = vorgaben or {}
    sha = hashlib.sha256(inhalt).hexdigest()
    con = db()
    try:
        alt = con.execute("SELECT id FROM belege WHERE sha256=?", (sha,)).fetchone()
        if alt:
            return alt["id"], False, "schon vorhanden"
        endung = os.path.splitext(dateiname or "")[1].lower()
        if not endung:
            endung = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "application/xml": ".xml", "text/xml": ".xml"}.get(mime, ".bin")
        text, xml = "", None
        if endung == ".pdf":
            text, xml = pdf_lesen(inhalt)
        elif endung == ".xml":
            xml = inhalt
        xml = xml_dazu or xml
        daten = e_rechnung_lesen(xml) if xml else None
        if not daten:
            daten = text_auslesen(text)
            daten["verkaeufer"] = ""
            daten["kaeufer"] = ""
        if not alles_ist_beleg and not daten.get("sicher") and not xml:
            # Inhalt und Dateiname entscheiden; der Betreff zählt nur bei Scans ohne Text
            pruef_text = " ".join([text[:2500], dateiname or ""] + ([] if text.strip() else [mail.get("betreff", "")]))
            if not RECHNUNGS_WORTE.search(pruef_text):
                return None, False, "keine Rechnung"
        # Richtung: Eingang oder Ausgang?
        art = "ER"
        if richtung == "ausgang":
            art = "AR"
        elif richtung == "auto":
            if daten.get("verkaeufer") and ist_eigene_firma(con, daten["verkaeufer"]):
                art = "AR"
            elif mail.get("von_adresse") and eigene_domain(con, mail["von_adresse"]) and not re.match(r"^\s*(wg|fwd?|aw|re)\s*:", mail.get("betreff", ""), re.I):
                art = "AR"
        if daten.get("gutschrift") and art == "ER":
            art = "GS"
        if art == "AR":
            lieferant = daten.get("kaeufer") or mail.get("an_name") or lieferant_aus_absender(con, mail.get("an", "")) or ""
        else:
            lieferant = daten.get("verkaeufer") or lieferant_aus_absender(con, mail.get("von", "")) or ""
        if not lieferant and text and art != "AR":
            for zeile in text.splitlines()[:6]:
                zeile = zeile.strip()
                if 3 <= len(zeile) <= 60 and re.search(r"[A-Za-zÄÖÜäöü]{3}", zeile) and not RECHNUNGS_WORTE.search(zeile) and not ist_eigene_firma(con, zeile):
                    lieferant = re.sub(r"\s+(?:GmbH|AG|KG|e\.\s?K\.)\b.*$", "", zeile).strip(" ,-")
                    break
        if not lieferant and dateiname:
            lieferant = re.split(r"[_\-\s.]", os.path.splitext(dateiname)[0])[0][:30]
        jetzt_t = jetzt_text()
        cur = con.execute(
            """INSERT INTO belege (sha256, art, lieferant, rechnungsnr, datum, betrag, waehrung, original_name, mime, quelle, quelle_id,
               mail_id, mail_betreff, mail_von, mail_datum, status, sicher, bar, notiz, text_auszug, erstellt, geaendert)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'neu', ?, ?, ?, ?, ?, ?)""",
            (sha, vorgaben.get("art") or art, vorgaben.get("lieferant") or lieferant[:80], daten.get("rechnungsnr", "")[:60],
             vorgaben.get("datum") or daten.get("datum") or (mail.get("datum") or "")[:10] or (jetzt_t[:10] if vorgaben.get("bar") else ""), vorgaben.get("betrag") if vorgaben.get("betrag") is not None else daten.get("betrag"),
             daten.get("waehrung") or "EUR", (dateiname or "")[:200], mime, quelle[:80], quelle_id, mail.get("id", "")[:300],
             mail.get("betreff", "")[:300], mail.get("von", "")[:300], mail.get("datum", ""), 1 if daten.get("sicher") else 0,
             1 if vorgaben.get("bar") else 0, vorgaben.get("notiz", ""), (text or "")[:4000], jetzt_t, jetzt_t),
        )
        beleg_id = cur.lastrowid
        monat = (datetime.strptime(jetzt_t[:10], "%Y-%m-%d")).strftime("%Y/%m")
        ordner = os.path.join(ARCHIV, monat)
        os.makedirs(ordner, exist_ok=True)
        rel = os.path.join(monat, f"{beleg_id:06d}_{sha[:10]}{endung}")
        with open(os.path.join(ARCHIV, rel), "wb") as f:
            f.write(inhalt)
        os.chmod(os.path.join(ARCHIV, rel), 0o440)
        xml_rel = ""
        if xml and endung != ".xml":
            xml_rel = os.path.join(monat, f"{beleg_id:06d}_{sha[:10]}.xml")
            with open(os.path.join(ARCHIV, xml_rel), "wb") as f:
                f.write(xml)
            os.chmod(os.path.join(ARCHIV, xml_rel), 0o440)
        con.execute("UPDATE belege SET datei=?, xml_datei=? WHERE id=?", (rel, xml_rel, beleg_id))
        # Doppelt? Gleicher Lieferant + Nummer + Betrag schon vorhanden
        b = con.execute("SELECT * FROM belege WHERE id=?", (beleg_id,)).fetchone()
        if b["rechnungsnr"] and b["betrag"] is not None:
            zwilling = con.execute(
                "SELECT id FROM belege WHERE id != ? AND rechnungsnr = ? AND betrag = ? AND status NOT IN ('doppelt', 'ignoriert')",
                (beleg_id, b["rechnungsnr"], b["betrag"]),
            ).fetchone()
            if zwilling:
                con.execute("UPDATE belege SET status='doppelt', notiz=? WHERE id=?", (f"Gleiche Rechnung wie Beleg {zwilling['id']}", beleg_id))
        protokolliere(con, "beleg", f"Beleg {beleg_id} angelegt aus {quelle}: {dateiname}")
        con.commit()
        return beleg_id, True, ""
    finally:
        con.close()


def portal_hinweis_anlegen(con, quelle, quelle_id, mail, link):
    schluessel = "portal:" + (mail.get("id") or hashlib.sha1((mail.get("betreff", "") + mail.get("datum", "")).encode()).hexdigest())
    if con.execute("SELECT id FROM belege WHERE sha256=?", (schluessel,)).fetchone():
        return False
    jetzt_t = jetzt_text()
    con.execute(
        """INSERT INTO belege (sha256, art, lieferant, datum, status, quelle, quelle_id, mail_id, mail_betreff, mail_von, mail_datum, portal_link, erstellt, geaendert)
           VALUES (?, 'ER', ?, ?, 'portal', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (schluessel, lieferant_aus_absender(con, mail.get("von", "")), (mail.get("datum") or "")[:10], quelle, quelle_id,
         mail.get("id", ""), mail.get("betreff", "")[:300], mail.get("von", "")[:300], mail.get("datum", ""), link[:800], jetzt_t, jetzt_t),
    )
    return True


def beleg_aendern(beleg_id, felder):
    erlaubt = ("art", "lieferant", "rechnungsnr", "datum", "betrag", "waehrung", "status", "bar", "dauer", "dauer_muster", "notiz")
    con = db()
    alt = con.execute("SELECT * FROM belege WHERE id=?", (beleg_id,)).fetchone()
    if not alt:
        con.close()
        return False
    aenderungen = {k: v for k, v in felder.items() if k in erlaubt and str(alt[k] if alt[k] is not None else "") != str(v if v is not None else "")}
    if aenderungen:
        setze = ", ".join(f"{k}=?" for k in aenderungen)
        con.execute(f"UPDATE belege SET {setze}, geaendert=? WHERE id=?", (*aenderungen.values(), jetzt_text(), beleg_id))
        protokolliere(con, "aenderung", f"Beleg {beleg_id}: " + "; ".join(f"{k}: {alt[k]!r} -> {v!r}" for k, v in aenderungen.items()))
        # Lieferant für den Absender merken
        if "lieferant" in aenderungen and alt["mail_von"] and aenderungen["lieferant"]:
            adresse = parseaddr(alt["mail_von"])[1].lower()
            if adresse and not eigene_domain(con, adresse):
                con.execute("INSERT INTO lieferanten (muster, name) VALUES (?, ?) ON CONFLICT(muster) DO UPDATE SET name=excluded.name", (adresse, aenderungen["lieferant"]))
    con.commit()
    con.close()
    return True


def beleg_pfad(beleg, xml=False):
    rel = beleg["xml_datei"] if xml else beleg["datei"]
    if not rel:
        return ""
    pfad = os.path.realpath(os.path.join(ARCHIV, rel))
    if os.path.commonpath([pfad, os.path.realpath(ARCHIV)]) != os.path.realpath(ARCHIV):
        return ""
    return pfad


# ---------------------------------------------------------------- Quellen: Postfächer

def _dekodiere(wert):
    try:
        return str(make_header(decode_header(wert or "")))
    except Exception:
        return wert or ""


def imap_verbinden(quelle):
    host = quelle["host"] or ANBIETER.get(quelle["anbieter"] or "", ("", "", 993))[1]
    port = int(quelle["port"] or 993)
    verbindung = imaplib.IMAP4_SSL(host, port, timeout=40) if port == 993 else imaplib.IMAP4(host, port, timeout=40)
    if port != 993:
        verbindung.starttls()
    verbindung.login(quelle["benutzer"], entschluesseln(quelle["geheim"]))
    return verbindung


def imap_ordner_liste(verbindung):
    status, daten = verbindung.list()
    namen = []
    for zeile in daten or []:
        if not zeile:
            continue
        text = zeile.decode("utf-8", "replace") if isinstance(zeile, bytes) else str(zeile)
        m = re.search(r'\) "?[^"]*"? (.+)$', text)
        if m:
            namen.append(m.group(1).strip().strip('"'))
    return namen


def quelle_testen(quelle_id):
    con = db()
    quelle = con.execute("SELECT * FROM quellen WHERE id=?", (quelle_id,)).fetchone()
    con.close()
    if not quelle:
        return False, "Quelle nicht gefunden."
    try:
        v = imap_verbinden(quelle)
        ordner = imap_ordner_liste(v)
        v.logout()
    except imaplib.IMAP4.error as exc:
        return False, f"Anmeldung abgelehnt – Benutzer oder Passwort prüfen. ({exc})"
    except Exception as exc:
        return False, f"Keine Verbindung: {exc}"
    return True, "Verbindung klappt. Ordner: " + ", ".join(ordner[:40])


def _mail_teile(nachricht):
    anhaenge, html, text = [], "", ""
    for teil in nachricht.walk():
        if teil.is_multipart():
            continue
        name = _dekodiere(teil.get_filename() or "")
        typ = (teil.get_content_type() or "").lower()
        if name or typ in ("application/pdf", "application/xml", "text/xml") or "attachment" in (teil.get("Content-Disposition") or ""):
            inhalt = teil.get_payload(decode=True) or b""
            if inhalt:
                anhaenge.append((name or f"anhang{len(anhaenge) + 1}", typ, inhalt))
        elif typ == "text/html" and not html:
            html = (teil.get_payload(decode=True) or b"").decode(teil.get_content_charset() or "utf-8", "replace")
        elif typ == "text/plain" and not text:
            text = (teil.get_payload(decode=True) or b"").decode(teil.get_content_charset() or "utf-8", "replace")
    return anhaenge, html, text


def _portal_link(html, text):
    for url, beschriftung in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html or "", re.I | re.S):
        if RECHNUNGS_WORTE.search(re.sub("<[^>]+>", "", beschriftung)) or re.search(r"rechnung|invoice|download|billing|konto|account", url, re.I):
            return url.replace("&amp;", "&")
    m = re.search(r"https?://\S*(?:rechnung|invoice|billing|download)\S*", text or "", re.I)
    return m.group(0) if m else ""


def mail_verarbeiten(con, quelle, nachricht):
    """Eine Mail -> 0..n Belege. Gibt Anzahl neuer Belege zurück."""
    betreff = _dekodiere(nachricht.get("Subject", ""))
    von = _dekodiere(nachricht.get("From", ""))
    an = getaddresses([nachricht.get("To", "")])
    try:
        datum = parsedate_to_datetime(nachricht.get("Date")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        datum = ""
    mail = {
        "id": (nachricht.get("Message-ID") or "").strip(),
        "betreff": betreff, "von": von, "von_adresse": parseaddr(von)[1].lower(), "datum": datum,
        "an_name": _dekodiere(an[0][0]) if an else "",
        "an": _dekodiere(nachricht.get("To", "")),
    }
    anhaenge, html, text = _mail_teile(nachricht)
    pdfs = [a for a in anhaenge if a[0].lower().endswith(".pdf") or a[1] == "application/pdf"]
    xmls = [a for a in anhaenge if a[0].lower().endswith(".xml") or a[1] in ("application/xml", "text/xml")]
    bilder = [a for a in anhaenge if a[1].startswith("image/") and len(a[2]) > 30000] if quelle["alles_ist_beleg"] else []
    neu = 0
    con.commit()
    if pdfs or xmls or bilder:
        xml_fuer_pdf = xmls[0][2] if (len(pdfs) == 1 and len(xmls) == 1) else None
        for name, typ, inhalt in pdfs:
            beleg_id, ist_neu, _ = beleg_anlegen(inhalt, name, typ, quelle["name"], quelle["id"], mail, quelle["richtung"], bool(quelle["alles_ist_beleg"]), xml_dazu=xml_fuer_pdf)
            neu += 1 if ist_neu else 0
        if not xml_fuer_pdf:
            for name, typ, inhalt in xmls:
                if e_rechnung_lesen(inhalt):
                    beleg_id, ist_neu, _ = beleg_anlegen(inhalt, name, typ, quelle["name"], quelle["id"], mail, quelle["richtung"], True)
                    neu += 1 if ist_neu else 0
        for name, typ, inhalt in bilder:
            beleg_id, ist_neu, _ = beleg_anlegen(inhalt, name, typ, quelle["name"], quelle["id"], mail, quelle["richtung"], True)
            neu += 1 if ist_neu else 0
    elif RECHNUNGS_WORTE.search(betreff):
        link = _portal_link(html, text)
        if link and portal_hinweis_anlegen(con, quelle["name"], quelle["id"], mail, link):
            con.commit()
            neu += 1
    return neu


def quelle_abholen(quelle_id, max_mails=400):
    con = db()
    quelle = con.execute("SELECT * FROM quellen WHERE id=?", (quelle_id,)).fetchone()
    if not quelle or not quelle["aktiv"]:
        con.close()
        return 0, "pausiert"
    neu, gelesen = 0, 0
    try:
        v = imap_verbinden(quelle)
        start = quelle["start_datum"] or (jetzt() - timedelta(days=60)).date().isoformat()
        if quelle["letzter_lauf"] and not quelle["letzter_fehler"]:
            start = max(start, (datetime.strptime(quelle["letzter_lauf"][:10], "%Y-%m-%d") - timedelta(days=3)).date().isoformat())
        d = datetime.strptime(start, "%Y-%m-%d")
        seit = f"{d.day:02d}-{['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.month - 1]}-{d.year}"
        for ordner in [o.strip() for o in (quelle["ordner"] or "INBOX").split(",") if o.strip()]:
            status, _ = v.select(f'"{ordner}"', readonly=True)
            if status != "OK":
                continue
            uidvalidity = ""
            try:
                antwort = v.response("UIDVALIDITY")[1]
                uidvalidity = (antwort[0].decode() if antwort and isinstance(antwort[0], bytes) else str(antwort[0] if antwort else ""))
            except Exception:
                pass
            status, daten = v.uid("search", None, f"SINCE {seit}")
            uids = (daten[0] or b"").split() if status == "OK" and daten else []
            for uid in uids:
                uid = uid.decode()
                if con.execute("SELECT 1 FROM quellen_gesehen WHERE quelle_id=? AND ordner=? AND uidvalidity=? AND uid=?", (quelle_id, ordner, uidvalidity, uid)).fetchone():
                    continue
                if gelesen >= max_mails:
                    break
                status, teile = v.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK" or not teile or not isinstance(teile[0], tuple):
                    continue
                nachricht = email.message_from_bytes(teile[0][1])
                try:
                    neu += mail_verarbeiten(con, quelle, nachricht)
                except Exception as exc:
                    protokolliere(con, "fehler", f"Quelle {quelle['name']}, Mail {uid}: {exc}")
                con.execute("INSERT OR IGNORE INTO quellen_gesehen VALUES (?, ?, ?, ?)", (quelle_id, ordner, uidvalidity, uid))
                con.commit()
                gelesen += 1
        v.logout()
        meldung = f"{gelesen} Mails gelesen, {neu} neue Belege" + (" (weitere beim nächsten Lauf)" if gelesen >= max_mails else "")
        con.execute("UPDATE quellen SET letzter_lauf=?, letzter_status=?, letzter_fehler=0, gefunden=gefunden+? WHERE id=?", (jetzt_text(), meldung, neu, quelle_id))
    except imaplib.IMAP4.error as exc:
        meldung = f"Anmeldung abgelehnt – Passwort prüfen ({exc})"
        con.execute("UPDATE quellen SET letzter_lauf=?, letzter_status=?, letzter_fehler=1 WHERE id=?", (jetzt_text(), meldung, quelle_id))
    except Exception as exc:
        meldung = f"Fehler: {exc}"
        protokolliere(con, "fehler", traceback.format_exc())
        con.execute("UPDATE quellen SET letzter_lauf=?, letzter_status=?, letzter_fehler=1 WHERE id=?", (jetzt_text(), meldung, quelle_id))
    con.commit()
    con.close()
    return neu, meldung


def alle_abholen(nur_quelle=None):
    """Läuft im Hintergrund. Gibt False zurück, wenn schon ein Lauf aktiv ist."""
    if not _lauf_lock.acquire(blocking=False):
        return False
    def lauf():
        try:
            LAUF.update({"laeuft": True, "start": jetzt_text(), "ende": "", "meldung": "läuft …", "neu": 0})
            con = db()
            ids = [r["id"] for r in con.execute("SELECT id FROM quellen WHERE aktiv=1" + (" AND id=?" if nur_quelle else ""), ((nur_quelle,) if nur_quelle else ())).fetchall()]
            con.close()
            neu = 0
            for qid in ids:
                n, _ = quelle_abholen(qid)
                neu += n
            abgleichen()
            con = db()
            setze_einstellung(con, "letzter_lauf", jetzt_text())
            con.commit()
            con.close()
            LAUF.update({"meldung": f"fertig: {neu} neue Belege", "neu": neu})
        except Exception as exc:
            LAUF.update({"meldung": f"Fehler: {exc}"})
        finally:
            LAUF.update({"laeuft": False, "ende": jetzt_text()})
            _lauf_lock.release()
    threading.Thread(target=lauf, daemon=True).start()
    return True


def nachtlauf_starten(taeglich=None):
    """Hintergrund-Uhr: holt jede Nacht zwischen 2 und 5 Uhr einmal alle Quellen ab.
    taeglich(): wird einmal am Tag ab 8 Uhr aufgerufen (z. B. für die Monatserinnerung)."""
    def schleife():
        import time
        while True:
            try:
                t = jetzt()
                con = db()
                letzter = einstellung(con, "letzter_nachtlauf")
                con.close()
                if 2 <= t.hour < 5 and letzter[:10] != t.date().isoformat():
                    con = db()
                    setze_einstellung(con, "letzter_nachtlauf", jetzt_text())
                    con.commit()
                    con.close()
                    alle_abholen()
                if taeglich and t.hour >= 8:
                    con = db()
                    zuletzt = einstellung(con, "letzter_tageslauf")
                    if zuletzt[:10] != t.date().isoformat():
                        setze_einstellung(con, "letzter_tageslauf", jetzt_text())
                        con.commit()
                        con.close()
                        taeglich()
                    else:
                        con.close()
            except Exception:
                traceback.print_exc()
            time.sleep(600)
    threading.Thread(target=schleife, daemon=True).start()


# ---------------------------------------------------------------- Kontoauszüge

def konto_finden_oder_anlegen(con, name="", iban=""):
    iban = re.sub(r"\s+", "", iban or "").upper()
    if iban:
        row = con.execute("SELECT id FROM konten WHERE iban=?", (iban,)).fetchone()
        if row:
            return row["id"]
    if name:
        row = con.execute("SELECT id FROM konten WHERE name=?", (name,)).fetchone()
        if row:
            if iban:
                con.execute("UPDATE konten SET iban=? WHERE id=? AND (iban IS NULL OR iban='')", (iban, row["id"]))
            return row["id"]
    name = name or (f"Konto …{iban[-4:]}" if iban else "Bankkonto")
    return con.execute("INSERT INTO konten (name, iban, erstellt) VALUES (?, ?, ?)", (name, iban, jetzt_text())).lastrowid


def camt_lesen(inhalt):
    """CAMT.053 (auch mehrere Dateien in einer ZIP) -> [(iban, [buchung, …])]"""
    dateien = []
    if inhalt[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(inhalt)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xml"):
                    dateien.append(z.read(name))
    else:
        dateien.append(inhalt)
    ergebnis = []
    for daten in dateien:
        wurzel = ET.fromstring(daten)
        for stmt in [e for e in wurzel.iter() if _lokal(e.tag) in ("Stmt", "Rpt")]:
            konto = _finde(stmt, "Acct")
            iban = _text(konto, "IBAN") if konto is not None else ""
            buchungen = []
            for ntry in [e for e in stmt if _lokal(e.tag) == "Ntry"]:
                betrag_e = _finde(ntry, "Amt")
                betrag = cent((betrag_e.text or "0").strip()) if betrag_e is not None else 0
                if _text(ntry, "CdtDbtInd") == "DBIT":
                    betrag = -abs(betrag)
                datum = _text(ntry, "BookgDt", "Dt") or _text(ntry, "BookgDt", "DtTm")[:10] or _text(ntry, "ValDt", "Dt")
                partei_rolle = "Cdtr" if betrag < 0 else "Dbtr"
                parteien = _finde(ntry, "RltdPties")
                name = iban_g = ""
                if parteien is not None:
                    rolle = _finde(parteien, partei_rolle)
                    name = _text(rolle, "Nm") if rolle is not None else ""
                    konto_g = _finde(parteien, partei_rolle + "Acct")
                    iban_g = _text(konto_g, "IBAN") if konto_g is not None else ""
                zweck = " ".join((e.text or "").strip() for e in ntry.iter() if _lokal(e.tag) == "Ustrd" and e.text)
                if not zweck:
                    zweck = _text(ntry, "AddtlNtryInf")
                buchungen.append({"datum": datum_iso(datum), "betrag": betrag, "gegenpartei": name, "iban_gegen": iban_g,
                                  "zweck": zweck, "ref": _text(ntry, "AcctSvcrRef") or _text(ntry, "NtryRef")})
            ergebnis.append((iban, buchungen))
    return ergebnis


CSV_SPALTEN = {
    "datum": ["buchungstag", "buchungsdatum", "datum", "date", "buchung", "wertstellung", "valuta"],
    "betrag": ["betrag", "betrag eur", "umsatz", "betrag in eur", "brutto", "amount", "betrag euro"],
    "gegenpartei": ["beguenstigter zahlungspflichtiger", "name zahlungsbeteiligter", "zahlungsempfaenger", "empfaenger", "auftraggeber empfaenger",
                    "beguenstigter auftraggeber", "auftraggeber", "name", "beguenstigter", "gegenkonto name", "payee"],
    "iban": ["kontonummer iban", "iban zahlungsbeteiligter", "iban", "iban auftraggeber", "gegenkonto iban"],
    "zweck": ["verwendungszweck", "beschreibung", "hinweis", "betreff", "buchungstext", "description", "vorgang verwendungszweck", "referenz"],
    "soll": ["soll", "belastung", "ausgang"],
    "haben": ["haben", "gutschrift", "eingang"],
}


def csv_lesen(inhalt):
    """Kontoauszug als CSV (Sparkasse, Volksbank, Commerzbank, DKB, PayPal …) -> [buchung]"""
    text = None
    for kodierung in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = inhalt.decode(kodierung)
            break
        except UnicodeDecodeError:
            continue
    zeilen = text.splitlines()
    trenner = max([";", ",", "\t"], key=lambda t: sum(z.count(t) for z in zeilen[:30]))
    tabelle = list(csv.reader(io.StringIO(text), delimiter=trenner))
    kopf_index, zuordnung = None, {}
    for i, zeile in enumerate(tabelle[:40]):
        namen = [schluessel_text(z) for z in zeile]
        gefunden = {}
        for feld, synonyme in CSV_SPALTEN.items():
            for s in synonyme:
                if s in namen and feld not in gefunden:
                    gefunden[feld] = namen.index(s)
                    break
        if "datum" in gefunden and ("betrag" in gefunden or "soll" in gefunden):
            kopf_index, zuordnung = i, gefunden
            break
    if kopf_index is None:
        raise ValueError("In der Datei wurden keine Spalten für Datum und Betrag gefunden.")
    buchungen = []
    for zeile in tabelle[kopf_index + 1:]:
        if len(zeile) <= max(zuordnung.values()):
            continue
        datum = datum_iso(zeile[zuordnung["datum"]])
        if not datum:
            continue
        if "betrag" in zuordnung:
            betrag = cent(zeile[zuordnung["betrag"]])
        else:
            betrag = (cent(zeile[zuordnung["haben"]]) or 0) - abs(cent(zeile[zuordnung["soll"]]) or 0) if "haben" in zuordnung else -abs(cent(zeile[zuordnung["soll"]]) or 0)
        if betrag is None:
            continue
        hole = lambda f: zeile[zuordnung[f]].strip() if f in zuordnung else ""
        buchungen.append({"datum": datum, "betrag": betrag, "gegenpartei": hole("gegenpartei"), "iban_gegen": hole("iban").replace(" ", ""),
                          "zweck": hole("zweck"), "ref": ""})
    return buchungen


def kontoauszug_importieren(inhalt, dateiname, konto_name=""):
    """-> dict(neu, doppelt, konten)"""
    con = db()
    ergebnis = {"neu": 0, "doppelt": 0, "konten": []}
    try:
        if dateiname.lower().endswith((".xml", ".zip")) or inhalt.lstrip()[:5] == b"<?xml":
            pakete = camt_lesen(inhalt)
        else:
            pakete = [("", csv_lesen(inhalt))]
        for iban, buchungen in pakete:
            konto_id = konto_finden_oder_anlegen(con, konto_name, iban)
            ergebnis["konten"].append(konto_id)
            import_id = con.execute("INSERT INTO importe (konto_id, dateiname, neu, doppelt, erstellt) VALUES (?, ?, 0, 0, ?)", (konto_id, dateiname, jetzt_text())).lastrowid
            gesehen = {}
            neu = doppelt = 0
            for b in buchungen:
                basis = f"{konto_id}|{b['datum']}|{b['betrag']}|{schluessel_text(b['gegenpartei'])}|{schluessel_text(b['zweck'])[:120]}|{b['ref']}"
                gesehen[basis] = gesehen.get(basis, 0) + 1
                schluessel = hashlib.sha1(f"{basis}|{gesehen[basis]}".encode()).hexdigest()
                try:
                    con.execute(
                        "INSERT INTO buchungen (konto_id, datum, betrag, gegenpartei, iban_gegen, zweck, schluessel, import_id, erstellt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (konto_id, b["datum"], b["betrag"], b["gegenpartei"][:200], b["iban_gegen"][:40], b["zweck"][:600], schluessel, import_id, jetzt_text()),
                    )
                    neu += 1
                except sqlite3.IntegrityError:
                    doppelt += 1
            con.execute("UPDATE importe SET neu=?, doppelt=? WHERE id=?", (neu, doppelt, import_id))
            ergebnis["neu"] += neu
            ergebnis["doppelt"] += doppelt
        protokolliere(con, "import", f"Kontoauszug {dateiname}: {ergebnis['neu']} neu, {ergebnis['doppelt']} schon vorhanden")
        con.commit()
    finally:
        con.close()
    abgleichen()
    return ergebnis


# ---------------------------------------------------------------- Abgleich

def _woerter(text):
    return {w for w in schluessel_text(text).split() if len(w) >= 4 and w not in ("gmbh", "rechnung", "kundennummer", "vielen", "dank", "sepa", "lastschrift", "ueberweisung")}


def punkte(buchung, beleg):
    if beleg["betrag"] is None:
        return 0
    zahl = abs(buchung["betrag"])
    soll = abs(beleg["betrag"])
    p = 0
    if zahl == soll:
        p += 50
    elif soll and 0.95 * soll <= zahl < soll:
        p += 30  # Skonto
    else:
        return 0
    text = schluessel_text(buchung["zweck"] + " " + buchung["gegenpartei"])
    nr = schluessel_text(beleg["rechnungsnr"])
    if nr and len(nr.replace(" ", "")) >= 3 and nr.replace(" ", "") in text.replace(" ", ""):
        p += 40
    if _woerter(beleg["lieferant"]) & _woerter(buchung["gegenpartei"] + " " + buchung["zweck"]):
        p += 25
    try:
        tage = (datetime.strptime(buchung["datum"], "%Y-%m-%d") - datetime.strptime(beleg["datum"], "%Y-%m-%d")).days
        if -7 <= tage <= 60:
            p += 15 if tage <= 30 else 8
        else:
            p -= 20
    except (ValueError, TypeError):
        pass
    return p


def _passende_art(buchung, beleg):
    if buchung["betrag"] < 0:
        return beleg["art"] in ("ER",)
    return beleg["art"] in ("AR", "GS")


def kandidaten(con, buchung, limit=8, suche=""):
    belegt = {r["beleg_id"] for r in con.execute("SELECT beleg_id FROM zuordnung")}
    liste = []
    zeilen = con.execute("SELECT * FROM belege WHERE status NOT IN ('ignoriert', 'doppelt', 'portal') AND dauer=0").fetchall()
    for b in zeilen:
        if suche:
            if schluessel_text(suche) not in schluessel_text(" ".join([b["lieferant"], b["rechnungsnr"], betrag_text(b["betrag"]), b["original_name"]])):
                continue
            p = punkte(buchung, b)
        else:
            if b["id"] in belegt or not _passende_art(buchung, b):
                continue
            p = punkte(buchung, b)
            if p < 40:
                continue
        liste.append((p, b))
    liste.sort(key=lambda x: (-x[0], x[1]["datum"] or ""))
    return liste[:limit]


def regel_passt(con, buchung):
    text = schluessel_text(buchung["gegenpartei"] + " " + buchung["zweck"])
    for r in con.execute("SELECT * FROM regeln").fetchall():
        muster = schluessel_text(r["muster"])
        if muster and muster in text:
            return r["id"]
    return None


def abgleichen():
    """Ordnet offene Buchungen zu: Regeln, Dauerbelege, eindeutige Treffer automatisch, sonst Vorschlag."""
    con = db()
    belegt = {r["beleg_id"] for r in con.execute("SELECT beleg_id FROM zuordnung")}
    belege = con.execute("SELECT * FROM belege WHERE status NOT IN ('ignoriert', 'doppelt', 'portal')").fetchall()
    dauer = [b for b in belege if b["dauer"] and b["dauer_muster"]]
    normal = [b for b in belege if not b["dauer"]]
    zugeordnet = 0
    for buchung in con.execute("SELECT * FROM buchungen WHERE status IN ('offen', 'vorschlag')").fetchall():
        regel = regel_passt(con, buchung)
        if regel:
            con.execute("UPDATE buchungen SET status='kein_beleg', regel_id=?, vorschlag_id=NULL WHERE id=?", (regel, buchung["id"]))
            continue
        text = schluessel_text(buchung["gegenpartei"] + " " + buchung["zweck"])
        dauer_treffer = next((b for b in dauer if schluessel_text(b["dauer_muster"]) and schluessel_text(b["dauer_muster"]) in text), None)
        if dauer_treffer:
            con.execute("INSERT OR IGNORE INTO zuordnung VALUES (?, ?, 'dauer', 100, ?)", (buchung["id"], dauer_treffer["id"], jetzt_text()))
            con.execute("UPDATE buchungen SET status='zugeordnet', vorschlag_id=NULL WHERE id=?", (buchung["id"],))
            zugeordnet += 1
            continue
        bewertet = sorted(
            ((punkte(buchung, b), b) for b in normal if b["id"] not in belegt and _passende_art(buchung, b)),
            key=lambda x: -x[0],
        )
        bewertet = [x for x in bewertet if x[0] >= 40]
        if bewertet and bewertet[0][0] >= 75 and (len(bewertet) == 1 or bewertet[1][0] <= bewertet[0][0] - 20):
            p, b = bewertet[0]
            con.execute("INSERT OR IGNORE INTO zuordnung VALUES (?, ?, 'auto', ?, ?)", (buchung["id"], b["id"], p, jetzt_text()))
            con.execute("UPDATE buchungen SET status='zugeordnet', vorschlag_id=NULL WHERE id=?", (buchung["id"],))
            belegt.add(b["id"])
            zugeordnet += 1
        elif bewertet:
            con.execute("UPDATE buchungen SET status='vorschlag', vorschlag_id=?, vorschlag_punkte=? WHERE id=?", (bewertet[0][1]["id"], bewertet[0][0], buchung["id"]))
        else:
            con.execute("UPDATE buchungen SET status='offen', vorschlag_id=NULL WHERE id=?", (buchung["id"],))
    con.commit()
    con.close()
    return zugeordnet


def zuordnen(buchung_id, beleg_ids, art="hand"):
    con = db()
    for beleg_id in beleg_ids:
        con.execute("INSERT OR IGNORE INTO zuordnung VALUES (?, ?, ?, 100, ?)", (buchung_id, beleg_id, art, jetzt_text()))
        con.execute("UPDATE belege SET status='geprueft' WHERE id=? AND status='neu'", (beleg_id,))
    con.execute("UPDATE buchungen SET status='zugeordnet', vorschlag_id=NULL WHERE id=?", (buchung_id,))
    protokolliere(con, "zuordnung", f"Buchung {buchung_id} <- Beleg(e) {beleg_ids}")
    con.commit()
    con.close()


def zuordnung_loesen(buchung_id, beleg_id=None):
    con = db()
    if beleg_id:
        con.execute("DELETE FROM zuordnung WHERE buchung_id=? AND beleg_id=?", (buchung_id, beleg_id))
    else:
        con.execute("DELETE FROM zuordnung WHERE buchung_id=?", (buchung_id,))
    rest = con.execute("SELECT COUNT(*) FROM zuordnung WHERE buchung_id=?", (buchung_id,)).fetchone()[0]
    if not rest:
        con.execute("UPDATE buchungen SET status='offen', regel_id=NULL WHERE id=?", (buchung_id,))
    protokolliere(con, "zuordnung", f"Buchung {buchung_id}: Zuordnung gelöst ({beleg_id or 'alle'})")
    con.commit()
    con.close()
    abgleichen()


def kein_beleg(buchung_id, regel_muster="", notiz=""):
    con = db()
    regel_id = None
    if regel_muster:
        regel_id = con.execute("INSERT INTO regeln (name, muster, erstellt) VALUES (?, ?, ?)", (regel_muster, regel_muster, jetzt_text())).lastrowid
    con.execute("DELETE FROM zuordnung WHERE buchung_id=?", (buchung_id,))
    con.execute("UPDATE buchungen SET status='kein_beleg', regel_id=?, notiz=?, vorschlag_id=NULL WHERE id=?", (regel_id, notiz, buchung_id))
    protokolliere(con, "zuordnung", f"Buchung {buchung_id}: kein Beleg nötig" + (f" (Regel „{regel_muster}“)" if regel_muster else ""))
    con.commit()
    con.close()
    if regel_id:
        abgleichen()


def regel_loeschen(regel_id):
    con = db()
    con.execute("DELETE FROM regeln WHERE id=?", (regel_id,))
    con.execute("UPDATE buchungen SET status='offen', regel_id=NULL WHERE regel_id=?", (regel_id,))
    con.commit()
    con.close()
    abgleichen()


# ---------------------------------------------------------------- Auswertung je Monat

def monats_zahlen(monat):
    con = db()
    zeilen = con.execute("SELECT status, COUNT(*) n FROM buchungen WHERE substr(datum,1,7)=? GROUP BY status", (monat,)).fetchall()
    con.close()
    z = {r["status"]: r["n"] for r in zeilen}
    gesamt = sum(z.values())
    return {"gesamt": gesamt, "zugeordnet": z.get("zugeordnet", 0), "kein_beleg": z.get("kein_beleg", 0),
            "vorschlag": z.get("vorschlag", 0), "offen": z.get("offen", 0)}


def monate_mit_daten():
    con = db()
    monate = {r[0] for r in con.execute("SELECT DISTINCT substr(datum,1,7) FROM buchungen WHERE datum != ''")}
    monate |= {r[0] for r in con.execute("SELECT DISTINCT substr(datum,1,7) FROM belege WHERE datum != ''")}
    con.close()
    monate.add(jetzt().strftime("%Y-%m"))
    return sorted((m for m in monate if re.match(r"^\d{4}-\d{2}$", m or "")), reverse=True)


# ---------------------------------------------------------------- Monatsexport

def _pdf_liste(titel, unterzeile, kopf, zeilen, breiten):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    puffer = io.BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
    stile = getSampleStyleSheet()
    klein = ParagraphStyle("k", parent=stile["BodyText"], fontSize=8.5, leading=10.5)
    fett = ParagraphStyle("f", parent=klein, fontName="Helvetica-Bold")
    from xml.sax.saxutils import escape
    daten = [[Paragraph(escape(str(k)), fett) for k in kopf]] + [[Paragraph(escape(str(z)), klein) for z in zeile] for zeile in zeilen]
    if not zeilen:
        daten.append([Paragraph("Keine Einträge – alles vollständig.", klein)] + [""] * (len(kopf) - 1))
    tabelle = Table(daten, colWidths=[b * mm for b in breiten], repeatRows=1)
    tabelle.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E2D9")), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                                 ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    doc.build([Paragraph(escape(titel), stile["Title"]), Paragraph(escape(unterzeile), stile["Normal"]), Spacer(1, 5 * mm), tabelle])
    return puffer.getvalue()


def monatsexport(monat):
    """-> (zip_bytes, dateiname, zusammenfassung)"""
    con = db()
    konten = {r["id"]: r["name"] for r in con.execute("SELECT * FROM konten")}
    buchungen = con.execute("SELECT * FROM buchungen WHERE substr(datum,1,7)=? ORDER BY datum, id", (monat,)).fetchall()
    zuord = {}
    for r in con.execute("SELECT z.buchung_id, b.* FROM zuordnung z JOIN belege b ON b.id=z.beleg_id"):
        zuord.setdefault(r["buchung_id"], []).append(r)
    bar = con.execute("SELECT * FROM belege WHERE bar=1 AND substr(datum,1,7)=? AND status NOT IN ('ignoriert','doppelt','portal')", (monat,)).fetchall()
    abschluss = con.execute("SELECT * FROM monate WHERE monat=?", (monat,)).fetchone()
    schon = set(json.loads(abschluss["belege"])) if abschluss and abschluss["belege"] else set()
    belegt_ids = {r["beleg_id"] for r in con.execute("SELECT beleg_id FROM zuordnung")}
    offene_rechnungen = con.execute(
        "SELECT * FROM belege WHERE substr(datum,1,7)=? AND bar=0 AND dauer=0 AND status NOT IN ('ignoriert','doppelt','portal')", (monat,)).fetchall()
    offene_rechnungen = [b for b in offene_rechnungen if b["id"] not in belegt_ids]
    portal = con.execute("SELECT * FROM belege WHERE status='portal'").fetchall()
    con.close()

    puffer = io.BytesIO()
    wurzel = f"Buchhaltung_{monat}/"
    enthalten = set()
    namen_vergeben = set()
    zusammenfassung = {"eingang": 0, "ausgang": 0, "fehlen": 0, "nachtrag": 0}
    datei_je_beleg = {}

    def beleg_rein(z, beleg, praefix=""):
        if beleg["id"] in enthalten and not beleg["dauer"]:
            return datei_je_beleg.get(beleg["id"], "")
        ordner = "Rechnungsausgang" if beleg["art"] == "AR" else "Rechnungseingang"
        if abschluss and beleg["id"] not in schon:
            ordner = f"Nachtrag_{monat}/" + ordner
            zusammenfassung["nachtrag"] += 1
        name = praefix + beleg_dateiname(beleg)
        basis, endung = os.path.splitext(name)
        zaehler = 2
        while f"{ordner}/{name}" in namen_vergeben:
            name = f"{basis}_{zaehler}{endung}"
            zaehler += 1
        namen_vergeben.add(f"{ordner}/{name}")
        pfad = beleg_pfad(beleg)
        if pfad and os.path.exists(pfad):
            z.write(pfad, wurzel + f"{ordner}/{name}")
        xml = beleg_pfad(beleg, xml=True)
        if xml and os.path.exists(xml):
            z.write(xml, wurzel + f"{ordner}/{os.path.splitext(name)[0]}.xml")
        enthalten.add(beleg["id"])
        datei_je_beleg[beleg["id"]] = name
        zusammenfassung["ausgang" if beleg["art"] == "AR" else "eingang"] += 1
        return name

    fehlende, abgleich_zeilen = [], []
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        for bu in buchungen:
            dateien = []
            for beleg in zuord.get(bu["id"], []):
                praefix = f"{bu['datum']}_Dauerbeleg_" if beleg["dauer"] else ""
                dateien.append(beleg_rein(z, beleg, praefix))
            status = {"zugeordnet": "Beleg vorhanden", "kein_beleg": "kein Beleg nötig", "vorschlag": "Vorschlag offen", "offen": "BELEG FEHLT"}.get(bu["status"], bu["status"])
            abgleich_zeilen.append([datum_de(bu["datum"]), konten.get(bu["konto_id"], ""), bu["betrag"] / 100, bu["gegenpartei"], bu["zweck"], status, ", ".join(dateien)])
            if bu["status"] in ("offen", "vorschlag"):
                hinweis = ""
                woerter = _woerter(bu["gegenpartei"])
                for p in portal:
                    if woerter & _woerter(p["lieferant"] + " " + p["mail_von"]):
                        hinweis = f"Im Portal: {p['portal_link'][:90]}"
                        break
                fehlende.append((abs(bu["betrag"]), [datum_de(bu["datum"]), betrag_text(bu["betrag"]), bu["gegenpartei"], bu["zweck"][:140], konten.get(bu["konto_id"], ""), hinweis]))
        for beleg in bar:
            beleg_rein(z, beleg)
        fehlende.sort(key=lambda x: -x[0])
        zusammenfassung["fehlen"] = len(fehlende)
        z.writestr(wurzel + f"Fehlende_Belege_{monat}.pdf", _pdf_liste(
            f"Fehlende Belege {monat}", f"{len(fehlende)} Buchungen ohne Beleg · erstellt {jetzt().strftime('%d.%m.%Y %H:%M')} · größte Beträge zuerst",
            ["Datum", "Betrag", "Empfänger / Zahler", "Verwendungszweck", "Konto", "Hinweis"], [f[1] for f in fehlende], [22, 26, 55, 95, 30, 45]))
        z.writestr(wurzel + f"Offene_Rechnungen_{monat}.pdf", _pdf_liste(
            f"Rechnungen ohne Zahlung {monat}", "Rechnungsdatum in diesem Monat, aber noch keiner Buchung zugeordnet",
            ["Datum", "Art", "Lieferant / Kunde", "Rechnungsnr.", "Betrag", "Datei"],
            [[datum_de(b["datum"]), ARTEN.get(b["art"], b["art"]), b["lieferant"], b["rechnungsnr"], betrag_text(b["betrag"], b["waehrung"]), beleg_dateiname(b)] for b in offene_rechnungen],
            [22, 30, 55, 35, 26, 105]))
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            wb = Workbook()
            ws = wb.active
            ws.title = f"Abgleich {monat}"
            ws.append(["Datum", "Konto", "Betrag (EUR)", "Empfänger / Zahler", "Verwendungszweck", "Status", "Beleg-Datei"])
            for zelle in ws[1]:
                zelle.font = Font(bold=True)
            for zeile in abgleich_zeilen:
                ws.append(zeile)
                if zeile[5] == "BELEG FEHLT":
                    for zelle in ws[ws.max_row]:
                        zelle.fill = PatternFill("solid", fgColor="F8D7D6")
            for spalte, breite in zip("ABCDEFG", (12, 18, 14, 34, 60, 18, 70)):
                ws.column_dimensions[spalte].width = breite
            for zelle in ws["C"][1:]:
                zelle.number_format = "#,##0.00"
            xlsx = io.BytesIO()
            wb.save(xlsx)
            z.writestr(wurzel + f"Abgleich_{monat}.xlsx", xlsx.getvalue())
        except Exception:
            text = io.StringIO()
            w = csv.writer(text, delimiter=";")
            w.writerows([["Datum", "Konto", "Betrag", "Empfänger", "Zweck", "Status", "Datei"]] + abgleich_zeilen)
            z.writestr(wurzel + f"Abgleich_{monat}.csv", text.getvalue().encode("utf-8-sig"))
        if not enthalten:
            z.writestr(wurzel + "Rechnungseingang/", b"")
            z.writestr(wurzel + "Rechnungsausgang/", b"")
    return puffer.getvalue(), f"Buchhaltung_{monat}.zip", zusammenfassung, sorted(enthalten)


def monat_abschliessen(monat):
    _, _, _, enthalten = monatsexport(monat)
    con = db()
    con.execute("INSERT INTO monate (monat, abgeschlossen_am, belege) VALUES (?, ?, ?) ON CONFLICT(monat) DO UPDATE SET abgeschlossen_am=excluded.abgeschlossen_am, belege=excluded.belege",
                (monat, jetzt_text(), json.dumps(enthalten)))
    protokolliere(con, "monat", f"Monat {monat} abgeschlossen ({len(enthalten)} Belege)")
    con.commit()
    con.close()


def monat_wieder_oeffnen(monat):
    con = db()
    con.execute("DELETE FROM monate WHERE monat=?", (monat,))
    protokolliere(con, "monat", f"Monat {monat} wieder geöffnet")
    con.commit()
    con.close()


# ---------------------------------------------------------------- Rechnungspositionen

MENGE = r"(\d{1,5}(?:[.,]\d{1,3})?)"
PREIS = r"(\d{1,3}(?:\.\d{3})*,\d{2,3}|\d+,\d{2,3}|\d+\.\d{2})"
EINHEIT = r"(?:x|Stk\.?|St\.?|Stück|Krt\.?|Kart(?:on)?s?|KT|Pck\.?|Pak(?:et)?|Btl\.?|VE|kg|g|l|Ltr\.?|Fl\.?|Dose|Ds\.?|Eimer|Pal\.?)"
ZEILE_MUSTER = [
    # Name  Menge [Einheit]  Einzelpreis  Gesamt
    re.compile(rf"^(?:\d{{1,4}}\s+)?(?:(?=\S*\d)[A-Z0-9][\w\-./]{{2,14}}\s+)?(?P<name>[A-Za-zÄÖÜäöüß].{{2,70}}?)\s+(?P<menge>{MENGE})\s*(?P<einheit>{EINHEIT})?\.?\s+(?:à\s*|je\s*)?(?P<preis>{PREIS})\s*(?:€|EUR)?\s+(?P<summe>{PREIS})\s*(?:€|EUR)?(?:\s+[A-Z0-9%]{{1,4}})?$", re.I),
    # Menge [Einheit] x Name  Einzelpreis  Gesamt
    re.compile(rf"^(?P<menge>{MENGE})\s*(?P<einheit>{EINHEIT})?\s*x?\s+(?P<name>[A-Za-zÄÖÜäöüß].{{2,70}}?)\s+(?P<preis>{PREIS})\s*(?:€|EUR)?\s+(?P<summe>{PREIS})\s*(?:€|EUR)?$", re.I),
]


def _zahl(text):
    text = (text or "").strip()
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def positionen_aus_xml(xml_bytes):
    try:
        wurzel = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    zeilen = []
    if _lokal(wurzel.tag) == "CrossIndustryInvoice":
        for e in wurzel.iter():
            if _lokal(e.tag) != "IncludedSupplyChainTradeLineItem":
                continue
            menge_e = _finde(e, "BilledQuantity")
            preis = _text(e, "NetPriceProductTradePrice", "ChargeAmount") or _text(e, "GrossPriceProductTradePrice", "ChargeAmount")
            zeilen.append({"name": _text(e, "SpecifiedTradeProduct", "Name"), "menge": _zahl(menge_e.text if menge_e is not None else ""),
                           "einheit": (menge_e.get("unitCode") if menge_e is not None else "") or "",
                           "einzelpreis": cent(preis.replace(",", ".")) if preis else None,
                           "summe": cent(_text(e, "LineTotalAmount").replace(",", ".")) if _text(e, "LineTotalAmount") else None})
    elif _lokal(wurzel.tag) in ("Invoice", "CreditNote"):
        for e in wurzel:
            if _lokal(e.tag) not in ("InvoiceLine", "CreditNoteLine"):
                continue
            menge_e = next((k for k in e if _lokal(k.tag) in ("InvoicedQuantity", "CreditedQuantity")), None)
            summe = next((((k.text or "").strip()) for k in e if _lokal(k.tag) == "LineExtensionAmount"), "")
            zeilen.append({"name": _text(e, "Item", "Name"), "menge": _zahl(menge_e.text if menge_e is not None else ""),
                           "einheit": (menge_e.get("unitCode") if menge_e is not None else "") or "",
                           "einzelpreis": cent(_text(e, "Price", "PriceAmount")) if _text(e, "Price", "PriceAmount") else None,
                           "summe": cent(summe) if summe else None})
    return [z for z in zeilen if z["name"]]


def positionen_aus_text(text):
    zeilen = []
    for roh in (text or "").splitlines():
        zeile = re.sub(r"\s{2,}", "  ", roh.strip())
        if len(zeile) < 8 or re.search(r"summe|gesamt|netto|brutto|mwst|ust\b|steuer|zwischensumme|rabatt|versand|übertrag|seite", zeile, re.I):
            continue
        for muster in ZEILE_MUSTER:
            m = muster.match(zeile)
            if not m:
                continue
            menge, preis, summe = _zahl(m.group("menge")), cent(m.group("preis")), cent(m.group("summe"))
            if menge is None or preis is None or summe is None or menge <= 0:
                continue
            # Plausibel? Menge × Einzelpreis ≈ Gesamt (Rabatte bis 30 % erlaubt)
            if summe and not (0.69 * menge * preis <= summe <= 1.01 * menge * preis + 2):
                continue
            zeilen.append({"name": re.sub(r"^x\s+", "", m.group("name").strip(" .-:")), "menge": menge, "einheit": (m.group("einheit") or "").strip("."),
                           "einzelpreis": preis, "summe": summe})
            break
    return zeilen


def positionen(beleg_id):
    """Positionen eines Belegs (einmal auslesen, dann gespeichert) -> (liste, herkunft)"""
    con = db()
    try:
        gelesen = con.execute("SELECT * FROM positionen_gelesen WHERE beleg_id=?", (beleg_id,)).fetchone()
        if gelesen:
            zeilen = [dict(r) for r in con.execute("SELECT * FROM positionen WHERE beleg_id=? ORDER BY nr", (beleg_id,))]
            return zeilen, gelesen["herkunft"]
        b = con.execute("SELECT * FROM belege WHERE id=?", (beleg_id,)).fetchone()
        if not b:
            return [], ""
        liste, herkunft = [], ""
        xml_pfad = beleg_pfad(b, xml=True) or (beleg_pfad(b) if (b["datei"] or "").endswith(".xml") else "")
        if xml_pfad and os.path.exists(xml_pfad):
            with open(xml_pfad, "rb") as f:
                liste = positionen_aus_xml(f.read())
            herkunft = "E-Rechnung" if liste else ""
        if not liste and (b["datei"] or "").endswith(".pdf"):
            pfad = beleg_pfad(b)
            if pfad and os.path.exists(pfad):
                with open(pfad, "rb") as f:
                    text, _ = pdf_lesen(f.read())
                liste = positionen_aus_text(text)
                herkunft = "PDF-Text" if liste else ""
        for nr, z in enumerate(liste, 1):
            con.execute("INSERT OR REPLACE INTO positionen VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (beleg_id, nr, z["name"][:120], z["menge"], z["einheit"][:12], z["einzelpreis"], z["summe"], herkunft))
        con.execute("INSERT OR REPLACE INTO positionen_gelesen VALUES (?, ?, ?)", (beleg_id, len(liste), herkunft))
        con.commit()
        return [dict(r) for r in con.execute("SELECT * FROM positionen WHERE beleg_id=? ORDER BY nr", (beleg_id,))], herkunft
    finally:
        con.close()


def namens_aehnlichkeit(a, b):
    """0..1 – wie gut passen zwei Produktnamen zusammen (Wortüberlappung, robust gegen Abkürzungen)."""
    def vorbereiten(t):
        t = re.sub(r"(\d)\s+(ml|cl|l|g|kg|cm|mm|st|stk)\b", r"\1\2", schluessel_text(t))
        return [w for w in t.split() if len(w) >= 2]
    wa, wb = vorbereiten(a), vorbereiten(b)
    if not wa or not wb:
        return 0.0
    treffer = 0
    for w in wa:
        if any(w == x or (len(w) >= 4 and len(x) >= 4 and (w.startswith(x[:4]) or x.startswith(w[:4]))) for x in wb):
            treffer += 1
    return treffer / max(1, min(len(wa), len(wb))) * (0.6 + 0.4 * treffer / max(len(wa), len(wb)))


def gleicher_lieferant(a, b):
    return bool(_woerter(a) & _woerter(b)) or (schluessel_text(a) and schluessel_text(a) == schluessel_text(b))


def preisentwicklung(lieferant=None, schwelle=0.03):
    """Preisänderungen je Artikel und Lieferant aus allen gelesenen Positionen."""
    con = db()
    zeilen = con.execute(
        """SELECT p.*, b.lieferant, b.datum, b.id AS bid FROM positionen p JOIN belege b ON b.id=p.beleg_id
           WHERE b.status NOT IN ('ignoriert','doppelt') AND b.art='ER' AND p.einzelpreis IS NOT NULL ORDER BY b.datum, b.id"""
    ).fetchall()
    con.close()
    verlauf = {}
    for z in zeilen:
        if lieferant and not gleicher_lieferant(lieferant, z["lieferant"]):
            continue
        key = (schluessel_text(z["lieferant"]), schluessel_text(z["name"]))
        verlauf.setdefault(key, []).append(z)
    aenderungen = []
    for (_l, _n), liste in verlauf.items():
        for alt, neu in zip(liste, liste[1:]):
            if alt["einzelpreis"] and neu["einzelpreis"] != alt["einzelpreis"]:
                faktor = neu["einzelpreis"] / alt["einzelpreis"] - 1
                if abs(faktor) >= schwelle:
                    aenderungen.append({"lieferant": neu["lieferant"], "artikel": neu["name"], "alt": alt["einzelpreis"], "neu": neu["einzelpreis"],
                                        "prozent": round(faktor * 100, 1), "datum_alt": alt["datum"], "datum_neu": neu["datum"], "beleg_id": neu["bid"]})
    aenderungen.sort(key=lambda a: a["datum_neu"] or "", reverse=True)
    return aenderungen

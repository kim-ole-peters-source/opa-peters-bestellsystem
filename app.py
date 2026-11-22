#!/usr/bin/env python3
# Internes Bestellsystem ohne Preise
# Start: python app.py, dann http://localhost:8000 öffnen

import os
import re
import csv
import io
import html
import uuid
import json
import hmac
import base64
import hashlib
import sqlite3
import smtplib
import calendar
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from http import cookies
from urllib.parse import parse_qs, urlparse, quote, quote_plus, urlencode
import buchhaltung as bh
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import parseaddr
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Vercel ist serverless: Schreibzugriff ist dort nur unter /tmp zuverlässig möglich.
# Lokal bleibt alles im Projektordner, damit die Mac-Version weiter funktioniert.
IS_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))
DATA_DIR = os.environ.get("DATA_DIR") or ("/tmp/opa-peters-bestellsystem" if IS_VERCEL else BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "bestellsystem.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
ORDER_DIR = os.path.join(DATA_DIR, "orders")
ORDER_IMAGE_DIR = os.path.join(DATA_DIR, "order_images")
TIME_EXPORT_DIR = os.path.join(DATA_DIR, "time_exports")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

# Startdaten beim ersten Serverless-Start nach /tmp kopieren.
for _name in ["bestellsystem.db", "settings.json"]:
    _src = os.path.join(BASE_DIR, _name)
    _dst = os.path.join(DATA_DIR, _name)
    if not os.path.exists(_dst) and os.path.exists(_src):
        try:
            import shutil
            shutil.copy2(_src, _dst)
        except Exception:
            pass
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")  # Bitte vor Onlinebetrieb ändern!
APP_SECRET = os.environ.get("APP_SECRET", ADMIN_PIN + "-lokal-test")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ORDER_DIR, exist_ok=True)
os.makedirs(ORDER_IMAGE_DIR, exist_ok=True)
os.makedirs(TIME_EXPORT_DIR, exist_ok=True)

BUYER_ACCOUNTS = [
    {
        "key": "filialisten_eis",
        "label": "Filialisten Eis",
        "pin": os.environ.get("BUYER_PIN_FILIALISTEN_EIS", "1111"),
    },
    {
        "key": "filialisten_feine_kost",
        "label": "Filialisten Feine Kost",
        "pin": os.environ.get("BUYER_PIN_FILIALISTEN_FEINE_KOST", "2222"),
    },
    {
        "key": "produzenten",
        "label": "Produzenten",
        "pin": os.environ.get("BUYER_PIN_PRODUZENTEN", "3333"),
    },
    {
        "key": "boss",
        "label": "Boss",
        "pin": os.environ.get("BUYER_PIN_BOSS", "4444"),
    },
]
BUYER_BY_KEY = {x["key"]: x for x in BUYER_ACCOUNTS}
ALL_LOCATIONS_KEY = "all_locations"
NO_LOCATIONS_KEY = "no_locations"
DEFAULT_VISIBLE_TO = ALL_LOCATIONS_KEY
DEFAULT_CATEGORIES = ["Allgemein", "Eis", "Feine Kost", "Reinigung", "Verbrauchsmaterial"]
ACCESS_ROLES = [
    ("standard", "Standort"),
    ("b2b", "B2B Kunde"),
    ("order_only", "Nur Bestellungen"),
    ("time_only", "Nur Zeiterfassung"),
    ("manager", "Filialleitung"),
]
ACCESS_ROLE_KEYS = {key for key, _label in ACCESS_ROLES}

DEFAULT_SETTINGS = {
    "locations": [
        {"id": "filiale-1", "name": "Filiale 1", "password": ""},
        {"id": "filiale-2", "name": "Filiale 2", "password": ""},
        {"id": "filiale-3", "name": "Filiale 3", "password": ""},
        {"id": "zentrale", "name": "Zentrale", "password": ""},
    ],
    "order_email_to": "",
    "whatsapp_number": "",
    "central_email_hint": "zentrale@example.com",
    "admin_pin_hash": "",
    "time_export_email": "info@opapeters.de",
    "last_auto_time_export_month": "",
    "last_auto_time_export_status": "",
    "time_employees": [],
}

APP_NAME = "Opa Peters Bestellung"
APP_SHORT_NAME = "OP Bestellung"
THEME_COLOR = "#1e3a8a"
ASSET_VERSION = "2026-11-22-buchhaltung-neu"
BACKGROUND_COLOR = "#f6f7fb"
MAX_FORM_BYTES = 12 * 1024 * 1024
MAX_CART_DRAFT_BYTES = 220 * 1024
MAX_PUSH_SUBSCRIPTION_BYTES = 24 * 1024
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_ORDER_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_ORDER_QUANTITY = 9999
BERLIN_TZ = ZoneInfo("Europe/Berlin") if ZoneInfo else None
COCKPIT_API_TOKEN = os.environ.get("COCKPIT_API_TOKEN", "").strip()
PRODUCTION_SECTIONS = {
    "backen": "Backen",
    "eisvitrinen": "Eisvitrinen",
    "eistorten": "Eistorten",
}


class UploadedFile:
    def __init__(self, filename, content):
        self.filename = filename or ""
        self.content = content or b""


class RequestTooLarge(Exception):
    pass


def berlin_now():
    return datetime.now(BERLIN_TZ) if BERLIN_TZ else datetime.now()


def hash_pin(pin):
    raw = str(pin or "").encode("utf-8")
    digest = hashlib.sha256(b"opa-peters-pin:" + raw).hexdigest()
    return f"sha256:{digest}"


def verify_pin(pin, stored_hash):
    stored_hash = stored_hash or hash_pin(ADMIN_PIN)
    return hmac.compare_digest(hash_pin(pin), stored_hash)


def is_valid_hhmm(value):
    if not re.match(r"^\d{2}:\d{2}$", value or ""):
        return False
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
    except ValueError:
        return False
    return 0 <= hour <= 23 and minute in [0, 15, 30, 45]


def is_valid_clock_time(value):
    if not re.match(r"^\d{2}:\d{2}$", value or ""):
        return False
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def minutes_from_hhmm(value):
    if not is_valid_hhmm(value):
        return None
    hour, minute = [int(part) for part in value.split(":", 1)]
    return hour * 60 + minute


def minutes_from_clock_time(value):
    if not is_valid_clock_time(value):
        return None
    hour, minute = [int(part) for part in value.split(":", 1)]
    return hour * 60 + minute


def hhmm_from_minutes(minutes):
    minutes = max(0, min(23 * 60 + 45, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        save_settings(DEFAULT_SETTINGS)
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    changed = False
    for key, value in DEFAULT_SETTINGS.items():
        if key not in data:
            data[key] = value
            changed = True
    if not data.get("admin_pin_hash"):
        data["admin_pin_hash"] = hash_pin(ADMIN_PIN)
        changed = True
    if str(data.get("time_export_email") or "").strip().lower() == "info@opapeters":
        # alter Standardwert ohne Endung – konnte nie zugestellt werden
        data["time_export_email"] = "info@opapeters.de"
        changed = True
    if changed:
        save_settings(data)
    return data


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def parse_decimal(value, default=0.0):
    text = str(value or "").strip().replace("€", "").replace(" ", "").replace(",", ".")
    if not text:
        return default
    try:
        return max(0.0, float(text))
    except ValueError:
        return default


def format_decimal(value, digits=2):
    number = float(value or 0)
    return f"{number:.{digits}f}".replace(".", ",")


def format_money(value):
    return f"{format_decimal(value, 2)} €"


def employee_settings_map():
    return {normalize_text_key(employee["name"]): employee for employee in get_time_employees(active_only=False)}


def employee_payroll_values(employee_name, minutes, employees_by_key=None):
    employees_by_key = employees_by_key if employees_by_key is not None else employee_settings_map()
    employee = employees_by_key.get(normalize_text_key(employee_name), {})
    hours = (minutes or 0) / 60
    max_hours = parse_decimal(employee.get("monthly_hours", 0))
    hourly_wage = parse_decimal(employee.get("hourly_wage", 0))
    overtime = max(0.0, hours - max_hours) if max_hours > 0 else 0.0
    salary = hours * hourly_wage
    return hours, overtime, salary


def normalize_time_employees(raw_employees):
    normalized = []
    seen = set()
    for item in raw_employees or []:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if not name:
            continue
        key = normalize_text_key(name)
        if key in seen:
            continue
        seen.add(key)
        active = bool(item.get("active", True)) if isinstance(item, dict) else True
        hourly_wage = parse_decimal(item.get("hourly_wage", 0) if isinstance(item, dict) else 0)
        monthly_hours = parse_decimal(item.get("monthly_hours", 0) if isinstance(item, dict) else 0)
        normalized.append({
            "name": name,
            "active": active,
            "hourly_wage": hourly_wage,
            "monthly_hours": monthly_hours,
        })
    return normalized


def get_time_employees(active_only=True):
    employees = normalize_time_employees(load_settings().get("time_employees", []))
    return [employee for employee in employees if employee.get("active") or not active_only]


def get_time_employee_names(active_only=True):
    return [employee["name"] for employee in get_time_employees(active_only=active_only)]


def save_time_employees(employees):
    settings = load_settings()
    settings["time_employees"] = normalize_time_employees(employees)
    save_settings(settings)


def make_location_id(name):
    base = normalize_text_key(name) or "standort"
    return base[:40] or "standort"


def normalize_min_stock_items(raw_items):
    normalized = []
    seen = set()
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        if not product_id.isdigit() or product_id in seen:
            continue
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        quantity = max(0, min(quantity, MAX_ORDER_QUANTITY))
        if quantity <= 0:
            continue
        seen.add(product_id)
        normalized.append({"product_id": product_id, "quantity": quantity})
    return normalized


def make_cockpit_item_id(prefix="item"):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def normalize_cockpit_tasks(raw_tasks):
    normalized = []
    seen = set()
    for item in raw_tasks or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        item_id = str(item.get("id") or "").strip() or make_cockpit_item_id("task")
        if item_id in seen:
            item_id = make_cockpit_item_id("task")
        seen.add(item_id)
        normalized.append({
            "id": item_id,
            "title": title[:180],
            "template": bool(item.get("template")),
            "active": bool(item.get("active", True)),
            "image_filename": os.path.basename(str(item.get("image_filename") or "").strip()),
        })
    return normalized


def normalize_cockpit_orders(raw_orders):
    normalized = []
    seen = set()
    for item in raw_orders or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        note = str(item.get("note") or "").strip()
        if not title and not note:
            continue
        item_id = str(item.get("id") or "").strip() or make_cockpit_item_id("order")
        if item_id in seen:
            item_id = make_cockpit_item_id("order")
        seen.add(item_id)
        normalized.append({
            "id": item_id,
            "title": title[:180] or "Bestellung",
            "note": note[:500],
            "pickup_date": str(item.get("pickup_date") or "").strip() if is_valid_iso_date(str(item.get("pickup_date") or "").strip()) else "",
            "active": bool(item.get("active", True)),
            "image_filename": os.path.basename(str(item.get("image_filename") or "").strip()),
        })
    return normalized


def normalize_cockpit_infos(raw_infos):
    normalized = []
    seen = set()
    for item in raw_infos or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or item.get("note") or "").strip()
        if not title and not text:
            continue
        item_id = str(item.get("id") or "").strip() or make_cockpit_item_id("info")
        if item_id in seen:
            item_id = make_cockpit_item_id("info")
        seen.add(item_id)
        normalized.append({
            "id": item_id,
            "title": title[:180] or "Info",
            "text": text[:1200],
            "active": bool(item.get("active", True)),
            "image_filename": os.path.basename(str(item.get("image_filename") or "").strip()),
        })
    return normalized


def message_thread_key(location_id, recipient_name):
    return f"{normalize_text_key(location_id)}:{normalize_text_key(recipient_name)}"


def is_valid_iso_date(value):
    try:
        datetime.strptime(value or "", "%Y-%m-%d")
        return True
    except ValueError:
        return False


def normalize_production_jobs(raw_jobs):
    normalized = []
    seen = set()
    for item in raw_jobs or []:
        if not isinstance(item, dict):
            continue
        customer_name = str(item.get("customer_name") or item.get("name") or item.get("title") or "").strip()
        order_text = str(item.get("order_text") or item.get("what") or item.get("title") or "").strip()
        title = customer_name or order_text
        due_date = str(item.get("due_date") or "").strip()
        section = str(item.get("section") or "").strip().lower()
        note = str(item.get("note") or "").strip()
        if not title or section not in PRODUCTION_SECTIONS or not is_valid_iso_date(due_date):
            continue
        item_id = str(item.get("id") or "").strip() or make_cockpit_item_id("production")
        if item_id in seen:
            item_id = make_cockpit_item_id("production")
        seen.add(item_id)
        normalized.append({
            "id": item_id,
            "section": section,
            "title": title[:180],
            "customer_name": customer_name[:180],
            "order_text": order_text[:500],
            "due_date": due_date,
            "note": note[:500],
            "active": bool(item.get("active", True)),
            "source": str(item.get("source") or "admin").strip()[:40] or "admin",
            "image_filename": os.path.basename(str(item.get("image_filename") or "").strip()),
        })
    return sorted(normalized, key=lambda item: (item["due_date"], item["section"], item["title"].lower()))


def normalize_locations(raw_locations):
    normalized = []
    used_ids = set()
    for index, item in enumerate(raw_locations or []):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            password = str(item.get("password") or "")
            contact_name = str(item.get("contact_name") or "")
            role = str(item.get("role") or item.get("access_role") or "standard").strip()
            time_tracking_enabled = bool(item.get("time_tracking_enabled"))
            time_tracking_max_end = str(item.get("time_tracking_max_end") or "")
            min_stock_enabled = bool(item.get("min_stock_enabled"))
            min_stock_items = normalize_min_stock_items(item.get("min_stock_items", []))
            cockpit_tasks = normalize_cockpit_tasks(item.get("cockpit_tasks", []))
            cockpit_orders = normalize_cockpit_orders(item.get("cockpit_orders", []))
            cockpit_infos = normalize_cockpit_infos(item.get("cockpit_infos", []))
            production_jobs = normalize_production_jobs(item.get("production_jobs", []))
            raw_id = str(item.get("id") or "").strip()
        else:
            name = str(item or "").strip()
            password = ""
            contact_name = ""
            role = "standard"
            time_tracking_enabled = False
            time_tracking_max_end = ""
            min_stock_enabled = False
            min_stock_items = []
            cockpit_tasks = []
            cockpit_orders = []
            cockpit_infos = []
            production_jobs = []
            raw_id = ""
        if not name:
            continue
        base_id = make_location_id(raw_id or name)
        location_id = base_id
        counter = 2
        while location_id in used_ids:
            location_id = f"{base_id}-{counter}"
            counter += 1
        used_ids.add(location_id)
        normalized.append({
            "id": location_id,
            "name": name,
            "password": password,
            "contact_name": contact_name,
            "role": role if role in ACCESS_ROLE_KEYS else "standard",
            "time_tracking_enabled": time_tracking_enabled,
            "time_tracking_max_end": time_tracking_max_end if is_valid_hhmm(time_tracking_max_end) else "",
            "min_stock_enabled": min_stock_enabled,
            "min_stock_items": min_stock_items,
            "cockpit_tasks": cockpit_tasks,
            "cockpit_orders": cockpit_orders,
            "cockpit_infos": cockpit_infos,
            "production_jobs": production_jobs,
        })
    return normalized


def get_locations():
    settings = load_settings()
    locations = normalize_locations(settings.get("locations", []))
    if not locations:
        locations = normalize_locations(DEFAULT_SETTINGS["locations"])
    return locations


def save_locations(locations):
    settings = load_settings()
    settings["locations"] = normalize_locations(locations)
    save_settings(settings)


def find_location(location_id):
    for location in get_locations():
        if location["id"] == location_id:
            return location
    return None


def location_ids():
    return [location["id"] for location in get_locations()]


def location_label(location_id):
    location = find_location(location_id)
    return location["name"] if location else (location_id or "")


def is_production_location(location):
    key = normalize_text_key((location or {}).get("id") or "")
    name = normalize_text_key((location or {}).get("name") or "")
    return key == "produktion" or name == "produktion"


def is_schwarzenbek_location(location):
    key = normalize_text_key((location or {}).get("id") or "")
    name = normalize_text_key((location or {}).get("name") or "")
    variants = {"schwarzenbek", "schwarzenbeck", "schwareznbek", "schwareznbeck"}
    return key in variants or name in variants


def current_workweek(offset=0):
    today = berlin_now().date() + timedelta(days=offset * 7)
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=offset) for offset in range(5)]


def production_jobs_from_all_locations():
    jobs = []
    for location in get_locations():
        jobs.extend(normalize_production_jobs(location.get("production_jobs", [])))
    return jobs


def find_production_job_owner(job_id):
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    for location in get_locations():
        if any(job["id"] == job_id for job in normalize_production_jobs(location.get("production_jobs", []))):
            return location
    return None


def location_labels_from_keys(keys):
    ids = location_ids()
    if not keys or ALL_LOCATIONS_KEY in keys:
        return "Alle Standorte"
    known = [location_label(key) for key in keys if key in ids]
    return ", ".join(known) if known else "Alle Standorte"


def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def table_columns(con, table_name):
    return [row[1] for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()]


def add_column_if_missing(con, table_name, column_name, sql_definition):
    if column_name not in table_columns(con, table_name):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_definition}")


def category_key(name):
    return normalize_text_key(name or "Allgemein") or "allgemein"


def category_exists_in_products(con, category_name):
    rows = con.execute("SELECT category FROM products WHERE deleted_at IS NULL").fetchall()
    return any(product_has_category(row["category"], category_name) for row in rows)


def sync_category_metadata(con):
    now = datetime.now().isoformat(timespec="seconds")
    rows = con.execute("SELECT id, name, category_key, deleted_at FROM categories ORDER BY id").fetchall()
    seen_live = {}
    for row in rows:
        key = category_key(row["name"])
        if row["category_key"] != key:
            con.execute("UPDATE categories SET category_key=? WHERE id=?", (key, row["id"]))
        if row["deleted_at"]:
            continue
        if key in seen_live:
            con.execute("UPDATE categories SET active=0, deleted_at=? WHERE id=?", (now, row["id"]))
            continue
        seen_live[key] = row["name"]


def ensure_category_in_connection(con, name, active=1):
    clean_name = (name or "Allgemein").strip() or "Allgemein"
    key = category_key(clean_name)
    now = datetime.now().isoformat(timespec="seconds")
    row = con.execute("SELECT * FROM categories WHERE category_key=? ORDER BY deleted_at IS NULL DESC, id LIMIT 1", (key,)).fetchone()
    if row:
        con.execute(
            "UPDATE categories SET name=?, active=?, deleted_at=NULL WHERE id=?",
            (row["name"] or clean_name, active, row["id"]),
        )
        return row["name"] or clean_name
    con.execute(
        "INSERT INTO categories (name, category_key, active, created_at, deleted_at) VALUES (?, ?, ?, ?, NULL)",
        (clean_name, key, active, now),
    )
    return clean_name


def category_was_deleted(con, name):
    row = con.execute("SELECT deleted_at FROM categories WHERE category_key=? ORDER BY id LIMIT 1", (category_key(name),)).fetchone()
    return bool(row and row["deleted_at"])


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            package_size TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Allgemein',
            source TEXT NOT NULL,
            visible_to TEXT NOT NULL DEFAULT '',
            image_filename TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category_key TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            deleted_at TEXT,
            created_at TEXT NOT NULL
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL,
            location TEXT NOT NULL,
            ordered_by TEXT NOT NULL,
            buyer_group TEXT,
            note TEXT,
            pdf_filename TEXT NOT NULL,
            pdf_data BLOB,
            order_image_filename TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            package_size TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Allgemein',
            source TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            completed_at TEXT,
            deleted_at TEXT,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cart_drafts (
            location_id TEXT PRIMARY KEY,
            cart_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cockpit_item_states (
            location_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            item_id TEXT NOT NULL,
            state TEXT NOT NULL,
            completed_by TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(location_id, item_type, item_id)
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS time_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id TEXT NOT NULL,
            location_name TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            work_location TEXT NOT NULL,
            work_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            edited INTEGER NOT NULL DEFAULT 0
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS time_export_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            export_month TEXT NOT NULL,
            filename TEXT,
            sent_to TEXT,
            status TEXT NOT NULL,
            message TEXT,
            auto INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            subscription_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            user_agent TEXT,
            owner TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pdf_files (
            filename TEXT PRIMARY KEY,
            pdf_data BLOB NOT NULL,
            created_at TEXT NOT NULL
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cockpit_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id TEXT NOT NULL,
            location_name TEXT NOT NULL,
            recipient_name TEXT NOT NULL,
            sender_role TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            message TEXT NOT NULL,
            read_by_admin_at TEXT,
            read_by_location_at TEXT,
            created_at TEXT NOT NULL
        )
    """
    )

    # Migration für ältere ZIP-Versionen / bestehende lokale Datenbanken
    add_column_if_missing(con, "products", "category", "TEXT NOT NULL DEFAULT 'Allgemein'")
    add_column_if_missing(con, "products", "visible_to", f"TEXT NOT NULL DEFAULT '{DEFAULT_VISIBLE_TO}'")
    add_column_if_missing(con, "products", "deleted_at", "TEXT")
    add_column_if_missing(con, "categories", "category_key", "TEXT")
    add_column_if_missing(con, "categories", "deleted_at", "TEXT")
    add_column_if_missing(con, "orders", "buyer_group", "TEXT")
    add_column_if_missing(con, "orders", "order_image_filename", "TEXT")
    add_column_if_missing(con, "orders", "pdf_data", "BLOB")
    add_column_if_missing(con, "orders", "completed_at", "TEXT")
    add_column_if_missing(con, "order_items", "category", "TEXT NOT NULL DEFAULT 'Allgemein'")
    add_column_if_missing(con, "order_items", "completed_at", "TEXT")
    add_column_if_missing(con, "order_items", "deleted_at", "TEXT")
    add_column_if_missing(con, "cockpit_item_states", "completed_by", "TEXT")
    add_column_if_missing(con, "time_entries", "updated_at", "TEXT")
    add_column_if_missing(con, "time_entries", "edited", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(con, "cockpit_messages", "read_by_admin_at", "TEXT")
    add_column_if_missing(con, "cockpit_messages", "read_by_location_at", "TEXT")

    cur.execute("UPDATE products SET category='Allgemein' WHERE category IS NULL OR TRIM(category)=''")
    cur.execute("UPDATE products SET visible_to=? WHERE visible_to IS NULL OR TRIM(visible_to)=''", (DEFAULT_VISIBLE_TO,))
    cur.execute("UPDATE order_items SET category='Allgemein' WHERE category IS NULL OR TRIM(category)=''")
    sync_category_metadata(con)

    now = datetime.now().isoformat(timespec="seconds")
    for category_name in DEFAULT_CATEGORIES:
        if category_key(category_name) == category_key("Allgemein") or not category_was_deleted(con, category_name):
            ensure_category_in_connection(con, category_name, active=1)
    for row in cur.execute("SELECT DISTINCT category FROM products WHERE deleted_at IS NULL AND category IS NOT NULL AND TRIM(category) != ''").fetchall():
        for category_name in split_categories(row[0]):
            if not category_was_deleted(con, category_name):
                ensure_category_in_connection(con, category_name, active=1)
    sync_category_metadata(con)
    try:
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_live_key ON categories(category_key) WHERE deleted_at IS NULL")
    except sqlite3.IntegrityError:
        sync_category_metadata(con)
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_live_key ON categories(category_key) WHERE deleted_at IS NULL")
    con.commit()
    con.close()


def seed_demo_products():
    con = db()
    count = con.execute("SELECT COUNT(*) FROM products WHERE deleted_at IS NULL").fetchone()[0]
    if count == 0:
        now = datetime.now().isoformat(timespec="seconds")
        demo = [
            ("Kaffeebecher", "Karton à 100 Stück", "Verbrauchsmaterial", "Großhandel A", DEFAULT_VISIBLE_TO, None, now),
            ("Servietten", "Packung à 250 Stück", "Verbrauchsmaterial", "Großhandel A", DEFAULT_VISIBLE_TO, None, now),
            ("Reinigungsmittel", "Kanister à 5 Liter", "Reinigung", "Lieferant B", DEFAULT_VISIBLE_TO, None, now),
        ]
        con.executemany(
            """
            INSERT INTO products (name, package_size, category, source, visible_to, image_filename, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            demo,
        )
        con.commit()
    con.close()


def esc(s):
    return html.escape(str(s or ""))


def pdf_text(value, style):
    return Paragraph(esc(value), style)


def pdf_table_row(values, style):
    return [pdf_text(value, style) for value in values]


def pdf_viewer_href(pdf_filename):
    return "/pdf-viewer?file=" + quote_plus(os.path.basename(pdf_filename or ""))


def pdf_direct_href(pdf_filename):
    return "/orders/" + quote_plus(os.path.basename(pdf_filename or ""))


def pdf_download_href(pdf_filename):
    return "/orders-download/" + quote_plus(os.path.basename(pdf_filename or ""))


def slug_filename(filename):
    filename = os.path.basename(filename or "bild")
    name, ext = os.path.splitext(filename)
    ext = ext.lower() if ext.lower() in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"] else ".jpg"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-") or "produktbild"
    return f"{safe}-{uuid.uuid4().hex[:10]}{ext}"


def buyer_label(key):
    return location_label(key)


def visible_keys_from_text(text):
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def product_visible_location_keys(text):
    keys = visible_keys_from_text(text)
    ids = location_ids()
    if NO_LOCATIONS_KEY in keys:
        return []
    if not keys or ALL_LOCATIONS_KEY in keys:
        return ids
    matching = [key for key in keys if key in ids]
    return matching or ids


def visible_labels(text):
    return location_labels_from_keys(visible_keys_from_text(text))


def role_label(role):
    labels = dict(ACCESS_ROLES)
    return labels.get(role or "standard", labels["standard"])


def role_options(selected="standard"):
    return "".join(
        f'<option value="{esc(key)}" {"selected" if key == selected else ""}>{esc(label)}</option>'
        for key, label in ACCESS_ROLES
    )


def location_can_order(location):
    return (location or {}).get("role", "standard") in ["standard", "b2b", "order_only", "manager"]


def location_can_time(location):
    return bool((location or {}).get("time_tracking_enabled")) and (location or {}).get("role", "standard") in ["standard", "time_only", "manager"]


def store_visible_locations(keys):
    ids = location_ids()
    unique = [key for key in ids if key in set(keys)]
    if not unique:
        return NO_LOCATIONS_KEY
    return ALL_LOCATIONS_KEY if set(unique) == set(ids) else ",".join(unique)


def apply_category_visibility_for_location(location_id, categories):
    allowed = {normalize_text_key(category) for category in categories if category}
    all_locations = location_ids()
    con = db()
    rows = con.execute("SELECT id, category, visible_to FROM products WHERE deleted_at IS NULL").fetchall()
    for row in rows:
        visible = product_visible_location_keys(row["visible_to"])
        category_keys = {normalize_text_key(category) for category in product_categories(row)}
        if category_keys & allowed:
            if location_id not in visible:
                visible.append(location_id)
        else:
            visible = [key for key in visible if key != location_id]
        visible = [key for key in all_locations if key in visible]
        con.execute("UPDATE products SET visible_to=? WHERE id=?", (store_visible_locations(visible), row["id"]))
    con.commit()
    con.close()


def time_greeting():
    today = berlin_now().date()
    if (today.month, today.day) in [(1, 1), (12, 25), (12, 26)]:
        return "Einen schönen Feiertag"
    hour = berlin_now().hour
    if hour < 11:
        return "Guten Morgen"
    if hour < 18:
        return "Guten Tag"
    return "Guten Abend"


def farewell_message():
    today = berlin_now().date()
    if today.weekday() >= 5:
        return "Hab ein schönes Wochenende und vielen Dank für deine Unterstützung."
    hour = berlin_now().hour
    if hour >= 18:
        return "Genieß deinen Abend und danke für deine Arbeit."
    if hour < 12:
        return "Danke, dass du heute schon mit angepackt hast."
    return "Danke für deinen Einsatz heute."


def normalize_text_key(text):
    text = (text or "").strip().lower()
    replacements = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "é": "e", "è": "e", "á": "a", "à": "a",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"[^a-z0-9]+", "", text)


def admin_orders_return_url(message="", error="", pdf="", open_location="", open_day=""):
    params = []
    if message:
        params.append(("msg", message))
    if error:
        params.append(("error", error))
    if pdf:
        params.append(("pdf", pdf))
    if open_location in ("offen", "erledigt", "rechnung", "einkauf"):
        if open_location == "einkauf":
            url = "/admin/einkauf"
            if params:
                url += "?" + urlencode(params)
            return url
        params.append(("ansicht", open_location))
        open_location = ""
    if open_location:
        params.append(("open_location", open_location))
    if open_day:
        params.append(("open_day", open_day))
    url = "/admin/orders"
    if params:
        url += "?" + urlencode(params)
    if open_location and open_day:
        url += "#orders-" + normalize_text_key(open_location) + "-" + normalize_text_key(open_day)
    return url


def parse_visible_to(value):
    raw = (value or "").strip()
    if not raw or normalize_text_key(raw) in ["alle", "all", "jeder"]:
        return location_ids()
    parts = [p.strip() for p in re.split(r"[,;|/\n]+", raw) if p.strip()]
    keys = []
    for part in parts:
        norm = normalize_text_key(part)
        for location in get_locations():
            if norm == normalize_text_key(location["id"]) or norm == normalize_text_key(location["name"]):
                keys.append(location["id"])
                break
        else:
            for account in BUYER_ACCOUNTS:
                if norm == normalize_text_key(account["key"]) or norm == normalize_text_key(account["label"]):
                    keys.extend(location_ids())
                    break
    unique = []
    for key in keys:
        if key in location_ids() and key not in unique:
            unique.append(key)
    return unique or location_ids()


def make_buyer_token(key):
    sig = hmac.new(APP_SECRET.encode("utf-8"), key.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"{key}:{sig}"


def parse_buyer_token(token):
    if not token or ":" not in token:
        return None
    key, sig = token.split(":", 1)
    if not find_location(key):
        return None
    expected = make_buyer_token(key).split(":", 1)[1]
    if hmac.compare_digest(sig, expected):
        return key
    return None


def make_admin_token():
    sig = hmac.new(APP_SECRET.encode("utf-8"), b"admin", hashlib.sha256).hexdigest()[:32]
    return f"admin:{sig}"


def is_valid_admin_token(token):
    return hmac.compare_digest(token or "", make_admin_token())


def get_products(active_only=True, buyer_key=None, sort="name_az", category_filter=""):
    con = db()
    q = "SELECT * FROM products"
    params = []
    conditions = ["deleted_at IS NULL"]
    if active_only:
        conditions.append("active=1")
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    rows = list(con.execute(q, params).fetchall())
    con.close()

    if buyer_key:
        rows = [p for p in rows if buyer_key in product_visible_location_keys(p["visible_to"])]

    if category_filter:
        rows = [p for p in rows if product_has_category(p, category_filter)]

    def name_key(p):
        return (p["name"] or "").lower()

    if sort == "name_za":
        rows.sort(key=name_key, reverse=True)
    elif sort == "category":
        rows.sort(key=lambda p: (product_categories(p)[0].lower(), name_key(p)))
    elif sort == "package":
        rows.sort(key=lambda p: ((p["package_size"] or "").lower(), name_key(p)))
    elif sort == "newest":
        rows.sort(key=lambda p: p["created_at"] or "", reverse=True)
    else:
        rows.sort(key=name_key)
    return rows


def get_source_names():
    con = db()
    rows = con.execute(
        "SELECT DISTINCT source FROM products WHERE deleted_at IS NULL AND TRIM(source) != '' ORDER BY LOWER(source)"
    ).fetchall()
    con.close()
    return [row["source"] for row in rows if row["source"]]


def get_min_stock_items_for_location(location, buyer_key=None):
    if not location or not location.get("min_stock_enabled"):
        return []
    products_by_id = {
        str(product["id"]): product
        for product in get_products(False, sort="category")
    }
    items = []
    for item in normalize_min_stock_items(location.get("min_stock_items", [])):
        product = products_by_id.get(str(item.get("product_id")))
        if not product:
            continue
        items.append({"product": product, "quantity": int(item.get("quantity") or 0)})
    return items


def get_all_categories(active_only=True, include_deleted=False):
    con = db()
    q = "SELECT * FROM categories"
    conditions = []
    if active_only:
        conditions.append("active=1")
    if not include_deleted:
        conditions.append("deleted_at IS NULL")
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    q += " ORDER BY LOWER(name)"
    rows = con.execute(q).fetchall()
    con.close()
    return rows


def get_category_names(active_only=True):
    names = [row["name"] for row in get_all_categories(active_only=active_only)]
    if "Allgemein" not in names:
        names.insert(0, "Allgemein")
    return names


def split_categories(value):
    raw = str(value or "").strip()
    if not raw:
        return ["Allgemein"]
    parts = [part.strip() for part in re.split(r"[,;|/\n]+", raw) if part.strip()]
    unique = []
    seen = set()
    for part in parts or ["Allgemein"]:
        key = normalize_text_key(part)
        if key and key not in seen:
            unique.append(part)
            seen.add(key)
    return unique or ["Allgemein"]


def category_text(categories):
    return ", ".join(categories or ["Allgemein"])


def product_categories(product_or_value):
    if isinstance(product_or_value, sqlite3.Row) or isinstance(product_or_value, dict):
        return split_categories(product_or_value["category"] if product_or_value["category"] else "Allgemein")
    return split_categories(product_or_value)


def product_has_category(product_or_value, category):
    wanted = normalize_text_key(category or "Allgemein")
    return any(normalize_text_key(item) == wanted for item in product_categories(product_or_value))


def category_input_name(prefix, category):
    return f"{prefix}_{normalize_text_key(category)}"


def category_checkboxes(name_prefix, selected_categories=None):
    selected_keys = {normalize_text_key(category) for category in (selected_categories or [])}
    return "".join(
        f'<label class="visibility-option"><input type="checkbox" name="{esc(category_input_name(name_prefix, category))}" value="1" {"checked" if normalize_text_key(category) in selected_keys else ""}><span>{esc(category)}</span></label>'
        for category in visible_category_names(selected_keys)
    )


def visible_category_names(selected_keys=None):
    """Alle Gruppen – die leere Standardgruppe 'Allgemein' nur, wenn sie benutzt wird."""
    selected_keys = selected_keys or set()
    names = get_category_names(True)
    if "Allgemein" in names and "allgemein" not in selected_keys:
        con = db()
        rows = con.execute("SELECT category FROM products WHERE deleted_at IS NULL").fetchall()
        con.close()
        if not any(product_has_category(row["category"], "Allgemein") for row in rows):
            names = [name for name in names if name != "Allgemein"]
    return names


def form_categories(form, prefix, fallback=None):
    names = get_category_names(True)
    selected = [category for category in names if str(form.get(category_input_name(prefix, category), "")).strip()]
    return selected or (fallback or ["Allgemein"])


def ensure_category(name):
    con = db()
    name = ensure_category_in_connection(con, name, active=1)
    sync_category_metadata(con)
    con.commit()
    con.close()
    return name


def ensure_categories(categories):
    cleaned = []
    for category in categories or ["Allgemein"]:
        ensured = ensure_category(category)
        if normalize_text_key(ensured) not in {normalize_text_key(item) for item in cleaned}:
            cleaned.append(ensured)
    return cleaned or ["Allgemein"]


def get_product(product_id):
    con = db()
    row = con.execute("SELECT * FROM products WHERE id=? AND deleted_at IS NULL", (product_id,)).fetchone()
    con.close()
    return row


def get_categories_for_buyer(buyer_key=None):
    products = get_products(True, buyer_key=buyer_key, sort="category")
    categories = sorted({category for p in products for category in product_categories(p)}, key=lambda x: x.lower())
    return categories



def filter_products_by_search(products, search_text, include_source=False):
    term = (search_text or "").strip().lower()
    if not term:
        return products
    words = [w for w in re.split(r"\s+", term) if w]
    filtered = []
    for p in products:
        values = [p["name"], category_text(product_categories(p)), p["package_size"]]
        if include_source:
            values.extend([p["source"], visible_labels(p["visible_to"])])
        haystack = " ".join(str(v or "") for v in values).lower()
        if all(word in haystack for word in words):
            filtered.append(p)
    return filtered


def normalize_whatsapp_number(number):
    # WhatsApp wa.me erwartet internationale Nummern ohne +, Leerzeichen oder Sonderzeichen.
    return re.sub(r"\D+", "", number or "")


def whatsapp_order_link(order):
    settings = load_settings()
    number = normalize_whatsapp_number(settings.get("whatsapp_number", ""))
    if not number:
        return ""
    if order.get("pdf_filename") and order.get("base_url"):
        pdf_hint = f"PDF: {str(order.get('base_url')).rstrip('/')}/orders/{order.get('pdf_filename')}"
    elif order.get("pdf_filename"):
        pdf_hint = f"PDF im Adminbereich: /orders/{order.get('pdf_filename', '')}"
    else:
        pdf_hint = "PDF ist im Adminbereich abrufbar."
    text = (
        f"Neue interne Bestellung {order.get('order_number', '')}\n"
        f"Standortzugang: {order.get('buyer_group', '')}\n"
        f"Standort: {order.get('location', '')}\n"
        f"Besteller: {order.get('ordered_by', '')}\n"
        f"Datum: {order.get('created_at', '')}\n"
        f"{pdf_hint}"
    )
    return f"https://wa.me/{number}?text={quote_plus(text)}"


HEADER_ALIASES = {
    "name": ["produktname", "produkt", "name", "artikel", "artikelname"],
    "package_size": ["gebindegroesse", "gebindegrosse", "gebinde", "verpackung", "package_size", "packaging", "einheit"],
    "category": ["kategorie", "category", "gruppe", "warengruppe"],
    "source": ["bezugsquelle", "source", "lieferant", "lieferantname", "supplier"],
    "visible_to": ["sichtbarfuer", "sichtbarkeit", "visibleto", "visible_to", "bestellerbereich", "zielgruppe"],
}


def canonical_header(header):
    norm = normalize_text_key(header)
    for key, aliases in HEADER_ALIASES.items():
        if norm in aliases:
            return key
    return norm


def parse_csv_products(uploaded_file):
    raw = uploaded_file.content or b""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t,")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return normalize_import_rows(reader)


def parse_xlsx_products(uploaded_file):
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        raise RuntimeError("Für Excel-Import bitte zuerst installieren: python -m pip install openpyxl") from exc
    wb = load_workbook(io.BytesIO(uploaded_file.content), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(x or "").strip() for x in rows[0]]
    data = []
    for row in rows[1:]:
        if not row or not any(str(x or "").strip() for x in row):
            continue
        data.append({headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))})
    return normalize_import_rows(data)


def normalize_import_rows(rows):
    normalized = []
    for raw_row in rows:
        item = {}
        for header, value in dict(raw_row).items():
            key = canonical_header(header)
            if key in ["name", "package_size", "category", "source", "visible_to"]:
                item[key] = str(value or "").strip()
        if any(item.get(k) for k in ["name", "package_size", "source"]):
            normalized.append(item)
    return normalized


def parse_import_file(uploaded_file):
    filename = (uploaded_file.filename or "").lower()
    if filename.endswith(".xlsx"):
        return parse_xlsx_products(uploaded_file)
    if filename.endswith(".csv"):
        return parse_csv_products(uploaded_file)
    raise RuntimeError("Bitte eine CSV- oder Excel-Datei auswählen.")


def parse_order_created_at(value):
    text = str(value or "").strip()
    if not text:
        return datetime.min
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min


def order_created_sort_key(order):
    try:
        order_id = int(order["id"])
    except (TypeError, ValueError, KeyError):
        order_id = 0
    return (parse_order_created_at(order["created_at"] if "created_at" in order.keys() else ""), order_id)


def get_orders():
    con = db()
    rows = con.execute("SELECT * FROM orders").fetchall()
    con.close()
    rows = sorted(rows, key=order_created_sort_key, reverse=True)
    return rows


def order_day_key(order):
    dt = parse_order_created_at(order["created_at"] if "created_at" in order.keys() else "")
    if dt == datetime.min:
        return "ohne-datum"
    return dt.strftime("%Y-%m-%d")


def order_day_label(day_key):
    try:
        return datetime.strptime(day_key, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return "Ohne Datum"


def format_iso_date(value):
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value or ""


def order_day_sort_key(day_key):
    try:
        return datetime.strptime(day_key, "%Y-%m-%d")
    except ValueError:
        return datetime.min


def get_open_order_product_ids(location_name):
    return set(get_open_order_product_quantities(location_name).keys())


def get_open_order_product_quantities(location_name):
    con = db()
    rows = con.execute(
        """
        SELECT oi.product_id, SUM(oi.quantity) AS total_quantity
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE o.completed_at IS NULL
          AND oi.product_id IS NOT NULL
          AND oi.quantity > 0
          AND oi.completed_at IS NULL
          AND oi.deleted_at IS NULL
          AND o.location = ?
        GROUP BY oi.product_id
        """,
        (location_name or "",),
    ).fetchall()
    con.close()
    return {
        str(row["product_id"]): int(row["total_quantity"] or 0)
        for row in rows
        if row["product_id"] is not None and int(row["total_quantity"] or 0) > 0
    }


def set_orders_completed(order_ids, completed=True):
    order_ids = [str(order_id) for order_id in order_ids if str(order_id).isdigit()]
    if not order_ids:
        return 0
    placeholders = ",".join("?" for _ in order_ids)
    completed_at = berlin_now().strftime("%d.%m.%Y %H:%M") if completed else None
    con = db()
    con.execute(f"UPDATE orders SET completed_at=? WHERE id IN ({placeholders})", [completed_at] + order_ids)
    con.commit()
    changed = con.total_changes
    con.close()
    return changed


def set_order_items_completed(item_ids, completed=True):
    item_ids = [str(item_id) for item_id in item_ids if str(item_id).isdigit()]
    if not item_ids:
        return 0
    placeholders = ",".join("?" for _ in item_ids)
    completed_at = berlin_now().strftime("%d.%m.%Y %H:%M") if completed else None
    con = db()
    con.execute(
        f"UPDATE order_items SET completed_at=? WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        [completed_at] + item_ids,
    )
    con.commit()
    changed = con.total_changes
    con.close()
    return changed


def delete_order_items_by_ids(item_ids):
    item_ids = [str(item_id) for item_id in item_ids if str(item_id).isdigit()]
    if not item_ids:
        return 0
    placeholders = ",".join("?" for _ in item_ids)
    con = db()
    con.execute(
        f"UPDATE order_items SET deleted_at=? WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        [berlin_now().isoformat(timespec="seconds")] + item_ids,
    )
    con.commit()
    changed = con.total_changes
    con.close()
    return changed


def get_order_items(order_id):
    con = db()
    rows = con.execute(
        "SELECT * FROM order_items WHERE order_id=? AND deleted_at IS NULL ORDER BY category, product_name",
        (order_id,),
    ).fetchall()
    con.close()
    return rows


def empty_cart_state():
    return {"items": {}, "details": {"ordered_by": "", "note": ""}}


def sanitize_cart_state(raw_state):
    if not isinstance(raw_state, dict):
        return empty_cart_state()
    raw_items = raw_state.get("items")
    if not isinstance(raw_items, dict):
        raw_items = raw_state
    items = {}
    for product_id, qty in raw_items.items():
        product_id = str(product_id or "").strip()
        if not product_id.isdigit():
            continue
        try:
            number = int(qty)
        except (TypeError, ValueError):
            number = 0
        number = max(0, min(number, MAX_ORDER_QUANTITY))
        if number > 0:
            items[product_id] = number
    raw_details = raw_state.get("details") if isinstance(raw_state.get("details"), dict) else {}
    details = {
        "ordered_by": str(raw_details.get("ordered_by", ""))[:120].strip(),
        "note": str(raw_details.get("note", ""))[:2000].strip(),
    }
    return {"items": items, "details": details}


def cart_state_has_content(state):
    details = state.get("details") or {}
    return bool(state.get("items") or details.get("ordered_by") or details.get("note"))


def get_cart_draft(location_id):
    if not location_id:
        return empty_cart_state(), ""
    con = db()
    row = con.execute("SELECT cart_json, updated_at FROM cart_drafts WHERE location_id=?", (location_id,)).fetchone()
    con.close()
    if not row:
        return empty_cart_state(), ""
    try:
        state = sanitize_cart_state(json.loads(row["cart_json"] or "{}"))
    except Exception:
        state = empty_cart_state()
    return state, row["updated_at"] or ""


def save_cart_draft(location_id, state):
    if not location_id:
        return
    state = sanitize_cart_state(state)
    con = db()
    if cart_state_has_content(state):
        updated_at = berlin_now().isoformat(timespec="seconds")
        con.execute(
            """
            INSERT INTO cart_drafts (location_id, cart_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(location_id) DO UPDATE SET cart_json=excluded.cart_json, updated_at=excluded.updated_at
            """,
            (location_id, json.dumps(state, ensure_ascii=False), updated_at),
        )
    else:
        con.execute("DELETE FROM cart_drafts WHERE location_id=?", (location_id,))
    con.commit()
    con.close()


def delete_cart_draft(location_id):
    if not location_id:
        return
    con = db()
    con.execute("DELETE FROM cart_drafts WHERE location_id=?", (location_id,))
    con.commit()
    con.close()


def get_cockpit_states(location_id):
    if not location_id:
        return {}
    con = db()
    rows = con.execute(
        "SELECT item_type, item_id, state FROM cockpit_item_states WHERE location_id=?",
        (location_id,),
    ).fetchall()
    con.close()
    return {(row["item_type"], row["item_id"]): row["state"] for row in rows}


def get_cockpit_state_details(location_id):
    if not location_id:
        return {}
    con = db()
    rows = con.execute(
        "SELECT item_type, item_id, state, completed_by, updated_at FROM cockpit_item_states WHERE location_id=?",
        (location_id,),
    ).fetchall()
    con.close()
    return {
        (row["item_type"], row["item_id"]): {
            "state": row["state"],
            "completed_by": row["completed_by"] or "",
            "updated_at": row["updated_at"] or "",
        }
        for row in rows
    }


def cockpit_state_is_from_today(state_info):
    updated_at = (state_info or {}).get("updated_at", "")
    try:
        return datetime.strptime(updated_at, "%d.%m.%Y %H:%M").date() == berlin_now().date()
    except ValueError:
        return False


def set_cockpit_state(location_id, item_type, item_id, state, completed_by=""):
    if not location_id or item_type not in ["task", "order", "production_job"] or not item_id:
        return False
    state = str(state or "").strip()
    valid_states = {
        "task": ["open", "done"],
        "order": ["open", "paid_picked", "picked_invoice"],
        "production_job": ["open", "started", "done"],
    }
    if state not in valid_states[item_type]:
        state = "open"
    con = db()
    if state == "open":
        con.execute(
            "DELETE FROM cockpit_item_states WHERE location_id=? AND item_type=? AND item_id=?",
            (location_id, item_type, item_id),
        )
    else:
        con.execute(
            """
            INSERT INTO cockpit_item_states (location_id, item_type, item_id, state, completed_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(location_id, item_type, item_id) DO UPDATE SET state=excluded.state, completed_by=excluded.completed_by, updated_at=excluded.updated_at
            """,
            (location_id, item_type, item_id, state, completed_by, berlin_now().strftime("%d.%m.%Y %H:%M")),
        )
    con.commit()
    con.close()
    return True


def create_cockpit_message(location_id, recipient_name, sender_role, sender_name, message):
    location = find_location(location_id)
    recipient_name = (recipient_name or "").strip()
    sender_role = "admin" if sender_role == "admin" else "location"
    sender_name = (sender_name or "").strip() or ("Admin" if sender_role == "admin" else (location or {}).get("name", "Standort"))
    message = (message or "").strip()
    if not location or not recipient_name or not message:
        return False
    now = berlin_now().strftime("%d.%m.%Y %H:%M")
    con = db()
    con.execute(
        """
        INSERT INTO cockpit_messages (
            location_id, location_name, recipient_name, sender_role, sender_name,
            message, read_by_admin_at, read_by_location_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            location["id"],
            location["name"],
            recipient_name[:120],
            sender_role,
            sender_name[:120],
            message[:2000],
            now if sender_role == "admin" else None,
            now if sender_role == "location" else None,
            now,
        ),
    )
    con.commit()
    con.close()
    return True


def count_unread_messages(for_role, location_id=""):
    con = db()
    if for_role == "admin":
        row = con.execute("SELECT COUNT(*) AS count FROM cockpit_messages WHERE sender_role='location' AND read_by_admin_at IS NULL").fetchone()
    else:
        row = con.execute(
            "SELECT COUNT(*) AS count FROM cockpit_messages WHERE location_id=? AND sender_role='admin' AND read_by_location_at IS NULL",
            (location_id,),
        ).fetchone()
    con.close()
    return int(row["count"] or 0) if row else 0


def unread_message_recipients(location_id):
    return [
        thread for thread in cockpit_message_threads(location_id)
        if int(thread.get("unread_location") or 0) > 0
    ]


def cockpit_message_threads(location_id=""):
    con = db()
    if location_id:
        rows = con.execute(
            "SELECT * FROM cockpit_messages WHERE location_id=? ORDER BY id DESC",
            (location_id,),
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM cockpit_messages ORDER BY id DESC").fetchall()
    con.close()
    threads = {}
    for row in rows:
        key = message_thread_key(row["location_id"], row["recipient_name"])
        entry = threads.setdefault(
            key,
            {
                "location_id": row["location_id"],
                "location_name": row["location_name"],
                "recipient_name": row["recipient_name"],
                "last_created_at": row["created_at"],
                "last_message": row["message"],
                "count": 0,
                "unread_admin": 0,
                "unread_location": 0,
            },
        )
        entry["count"] += 1
        if row["sender_role"] == "location" and not row["read_by_admin_at"]:
            entry["unread_admin"] += 1
        if row["sender_role"] == "admin" and not row["read_by_location_at"]:
            entry["unread_location"] += 1
    return sorted(threads.values(), key=lambda item: item["last_created_at"], reverse=True)


def get_cockpit_messages(location_id, recipient_name, mark_read_for=""):
    location_id = (location_id or "").strip()
    recipient_name = (recipient_name or "").strip()
    if not location_id or not recipient_name:
        return []
    con = db()
    if mark_read_for in ["admin", "location"]:
        column = "read_by_admin_at" if mark_read_for == "admin" else "read_by_location_at"
        sender = "location" if mark_read_for == "admin" else "admin"
        con.execute(
            f"UPDATE cockpit_messages SET {column}=? WHERE location_id=? AND recipient_name=? COLLATE NOCASE AND sender_role=? AND {column} IS NULL",
            (berlin_now().strftime("%d.%m.%Y %H:%M"), location_id, recipient_name, sender),
        )
        con.commit()
    rows = con.execute(
        "SELECT * FROM cockpit_messages WHERE location_id=? AND recipient_name=? COLLATE NOCASE ORDER BY id",
        (location_id, recipient_name),
    ).fetchall()
    con.close()
    return rows


def cockpit_invoice_orders():
    invoice_rows = []
    for location in get_locations():
        if is_production_location(location):
            continue
        states = get_cockpit_state_details(location["id"])
        for item in normalize_cockpit_orders(location.get("cockpit_orders", [])):
            info = states.get(("order", item["id"]), {})
            if item.get("active") and info.get("state") == "picked_invoice":
                invoice_rows.append({
                    "location": location,
                    "item": item,
                    "updated_at": info.get("updated_at", ""),
                })
    return sorted(invoice_rows, key=lambda row: (row["location"]["name"].lower(), row["updated_at"], row["item"]["title"].lower()))


def get_order_by_pdf_filename(pdf_filename):
    con = db()
    row = con.execute("SELECT * FROM orders WHERE pdf_filename=?", (pdf_filename,)).fetchone()
    con.close()
    return row


def store_pdf_file(filename, data):
    con = db()
    con.execute(
        """
        INSERT INTO pdf_files (filename, pdf_data, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET pdf_data=excluded.pdf_data, created_at=excluded.created_at
        """,
        (filename, data, berlin_now().strftime("%d.%m.%Y %H:%M")),
    )
    con.commit()
    con.close()


def get_cached_pdf_file(filename):
    con = db()
    row = con.execute("SELECT pdf_data FROM pdf_files WHERE filename=?", (filename,)).fetchone()
    con.close()
    return bytes(row["pdf_data"]) if row and row["pdf_data"] else None


def get_orders_by_ids(order_ids):
    order_ids = [str(order_id) for order_id in order_ids if str(order_id).isdigit()]
    if not order_ids:
        return []
    placeholders = ",".join("?" for _ in order_ids)
    con = db()
    rows = con.execute(f"SELECT * FROM orders WHERE id IN ({placeholders}) ORDER BY created_at, id", order_ids).fetchall()
    con.close()
    return rows


def delete_orders_by_ids(order_ids):
    order_ids = [str(order_id) for order_id in order_ids if str(order_id).isdigit()]
    if not order_ids:
        return 0
    orders = get_orders_by_ids(order_ids)
    if not orders:
        return 0
    placeholders = ",".join("?" for _ in order_ids)
    con = db()
    try:
        con.execute(f"DELETE FROM order_items WHERE order_id IN ({placeholders})", order_ids)
        con.execute(f"DELETE FROM orders WHERE id IN ({placeholders})", order_ids)
        pdf_names = [order["pdf_filename"] for order in orders if order["pdf_filename"]]
        if pdf_names:
            pdf_placeholders = ",".join("?" for _ in pdf_names)
            con.execute(f"DELETE FROM pdf_files WHERE filename IN ({pdf_placeholders})", pdf_names)
        con.commit()
    finally:
        con.close()

    for order in orders:
        for directory, filename in (
            (ORDER_DIR, order["pdf_filename"]),
            (ORDER_IMAGE_DIR, order["order_image_filename"]),
        ):
            if not filename:
                continue
            path = os.path.join(directory, os.path.basename(filename))
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    return len(orders)


def today_iso():
    return berlin_now().date().isoformat()


def current_month():
    return berlin_now().strftime("%Y-%m")


def floor_now_to_quarter():
    now = berlin_now()
    return now.hour * 60 + (now.minute // 15) * 15


def time_options(limit_minutes=None):
    if limit_minutes is None:
        limit_minutes = 23 * 60 + 45
    limit_minutes = max(0, min(limit_minutes, 23 * 60 + 45))
    return [hhmm_from_minutes(minutes) for minutes in range(0, limit_minutes + 1, 15)]


def time_options_between(start_minutes, end_minutes):
    start_minutes = max(0, min(start_minutes, 23 * 60 + 45))
    end_minutes = max(0, min(end_minutes, 23 * 60 + 45))
    start_minutes = (start_minutes // 15) * 15
    end_minutes = (end_minutes // 15) * 15
    if start_minutes > end_minutes:
        start_minutes = end_minutes
    return [hhmm_from_minutes(minutes) for minutes in range(start_minutes, end_minutes + 1, 15)]


def recent_end_time_options(limit_minutes, window_minutes=120):
    return time_options_between(limit_minutes - window_minutes, limit_minutes)


def option_html(options, selected=""):
    return "".join(f'<option value="{esc(value)}" {"selected" if value == selected else ""}>{esc(value)}</option>' for value in options)


def location_time_limit_minutes(location):
    limits = [floor_now_to_quarter()]
    max_end = (location or {}).get("time_tracking_max_end", "")
    if is_valid_hhmm(max_end):
        limits.append(minutes_from_hhmm(max_end))
    return min(value for value in limits if value is not None)


def is_time_tracking_enabled(location_id):
    location = find_location(location_id)
    return bool(location and location.get("time_tracking_enabled"))


def format_duration(minutes):
    minutes = int(minutes or 0)
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours}:{rest:02d} Std."


def validate_time_entry(location, employee_name, work_location, start_time, end_time, admin=False):
    employee_name = (employee_name or "").strip()
    work_location = (work_location or "").strip()
    start_time = (start_time or "").strip()
    end_time = (end_time or "").strip()
    if not employee_name:
        return None, "Bitte den Namen des Mitarbeiters eintragen."
    if not work_location:
        return None, "Bitte Einsatzort oder Filiale eintragen."
    if not is_valid_clock_time(start_time):
        return None, "Bitte eine gültige Anfangszeit auswählen."
    if not is_valid_hhmm(end_time):
        return None, "Bitte die Endzeit in 15-Minuten-Schritten auswählen."
    start_minutes = minutes_from_clock_time(start_time)
    end_minutes = minutes_from_hhmm(end_time)
    if end_minutes <= start_minutes:
        return None, "Die Endzeit muss nach der Anfangszeit liegen."
    if not admin:
        if end_minutes > floor_now_to_quarter():
            return None, "Die Endzeit darf nicht in der Zukunft liegen."
        max_end = (location or {}).get("time_tracking_max_end", "")
        if is_valid_hhmm(max_end) and end_minutes > minutes_from_hhmm(max_end):
            return None, f"Die Endzeit darf für diesen Standort nicht nach {max_end} liegen."
        window_start = max(0, location_time_limit_minutes(location) - 120)
        if end_minutes < window_start:
            return None, "Die Endzeit muss im aktuellen 2-Stunden-Fenster liegen."
    return end_minutes - start_minutes, ""


def get_time_entries(month=None, employee="", work_location=""):
    month = (month or current_month()).strip()
    con = db()
    q = "SELECT * FROM time_entries WHERE work_date LIKE ?"
    params = [f"{month}%"]
    if employee:
        q += " AND employee_name LIKE ?"
        params.append(f"%{employee}%")
    if work_location:
        q += " AND work_location LIKE ?"
        params.append(f"%{work_location}%")
    q += " ORDER BY work_date DESC, employee_name COLLATE NOCASE, start_time"
    rows = con.execute(q, params).fetchall()
    con.close()
    return rows


def summarize_time_entries(entries):
    by_employee = {}
    by_location = {}
    matrix = {}
    for entry in entries:
        minutes = entry["duration_minutes"] or 0
        employee = entry["employee_name"] or "Ohne Namen"
        work_location = entry["work_location"] or "Ohne Einsatzort"
        by_employee[employee] = by_employee.get(employee, 0) + minutes
        by_location[work_location] = by_location.get(work_location, 0) + minutes
        matrix.setdefault(employee, {})
        matrix[employee][work_location] = matrix[employee].get(work_location, 0) + minutes
    return by_employee, by_location, matrix


def time_entry_by_id(entry_id):
    con = db()
    row = con.execute("SELECT * FROM time_entries WHERE id=?", (entry_id,)).fetchone()
    con.close()
    return row


def find_duplicate_time_entry(employee_name, work_date, start_time, end_time, exclude_id=None):
    start_minutes = minutes_from_clock_time(start_time)
    end_minutes = minutes_from_hhmm(end_time)
    if start_minutes is None or end_minutes is None:
        return None
    q = """
        SELECT * FROM time_entries
        WHERE work_date=?
          AND employee_name = ? COLLATE NOCASE
    """
    params = [work_date, employee_name]
    if exclude_id:
        q += " AND id<>?"
        params.append(exclude_id)
    con = db()
    rows = con.execute(q, params).fetchall()
    con.close()
    for row in rows:
        existing_start = minutes_from_clock_time(row["start_time"])
        existing_end = minutes_from_hhmm(row["end_time"])
        if existing_start is None or existing_end is None:
            continue
        if existing_start < end_minutes and start_minutes < existing_end:
            return row
    return None


def build_time_export(month):
    entries = get_time_entries(month)
    by_employee, by_location, matrix = summarize_time_entries(entries)
    employees_by_key = employee_settings_map()
    entries_by_employee = {}
    for entry in entries:
        entries_by_employee.setdefault(entry["employee_name"] or "Ohne Namen", []).append(entry)

    filename = f"zeiterfassung_{month}.pdf"
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, rightMargin=12 * mm, leftMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TimeExportTitle", parent=styles["Title"], fontSize=18, leading=22, spaceAfter=8)
    section_style = ParagraphStyle("TimeExportSection", parent=styles["Heading2"], fontSize=12.5, leading=15, spaceBefore=10, spaceAfter=6)
    small_style = ParagraphStyle("TimeExportSmall", parent=styles["BodyText"], fontSize=7.5, leading=9)
    normal_style = ParagraphStyle("TimeExportNormal", parent=styles["BodyText"], fontSize=9, leading=11)
    story = [
        Paragraph(f"Zeiterfassung {esc(month)}", title_style),
        Paragraph(f"Erstellt am {esc(berlin_now().strftime('%d.%m.%Y %H:%M'))}", normal_style),
        Spacer(1, 4 * mm),
    ]

    def p(value, style=small_style):
        return Paragraph(esc(str(value or "")), style)

    def styled_table(rows, widths):
        table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef3fb")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#24346b")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("LEADING", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7deea")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbfcff")]),
        ]))
        return table

    story.append(Paragraph("Summe je Mitarbeiter", section_style))
    employee_rows = [[p("Mitarbeiter"), p("Gesamtstunden"), p("Überstunden"), p("Gehalt")]]
    for name, minutes in sorted(by_employee.items(), key=lambda item: item[0].lower()):
        hours, overtime, salary = employee_payroll_values(name, minutes, employees_by_key)
        employee_rows.append([p(name), p(format_decimal(hours)), p(format_decimal(overtime)), p(format_money(salary))])
    if len(employee_rows) == 1:
        employee_rows.append([p("Keine Daten"), p(""), p(""), p("")])
    story.append(styled_table(employee_rows, [70 * mm, 35 * mm, 35 * mm, 38 * mm]))

    story.append(Paragraph("Summe je Einsatzort", section_style))
    location_rows = [[p("Einsatzort"), p("Stunden")]]
    for name, minutes in sorted(by_location.items(), key=lambda item: item[0].lower()):
        location_rows.append([p(name), p(format_decimal((minutes or 0) / 60))])
    if len(location_rows) == 1:
        location_rows.append([p("Keine Daten"), p("")])
    story.append(styled_table(location_rows, [120 * mm, 58 * mm]))

    story.append(Paragraph("Schichten je Mitarbeiter", section_style))
    for employee, employee_entries in sorted(entries_by_employee.items(), key=lambda item: item[0].lower()):
        employee_minutes = sum(entry["duration_minutes"] or 0 for entry in employee_entries)
        hours, overtime, salary = employee_payroll_values(employee, employee_minutes, employees_by_key)
        shift_rows = [[p("Datum"), p("Einsatzort"), p("Anfang"), p("Ende"), p("Stunden"), p("Vorkommnisse"), p("Bearbeitet")]]
        for entry in sorted(employee_entries, key=lambda item: (item["work_date"], item["start_time"], item["end_time"])):
            shift_rows.append([
                p(entry["work_date"]),
                p(entry["work_location"]),
                p(entry["start_time"]),
                p(entry["end_time"]),
                p(format_decimal((entry["duration_minutes"] or 0) / 60)),
                p(entry["note"] or ""),
                p("ja" if entry["edited"] else "nein"),
            ])
        employee_block = [
            Paragraph(
                f"{esc(employee)} - Gesamtstunden: {esc(format_decimal(hours))} - Überstunden: {esc(format_decimal(overtime))} - Gehalt: {esc(format_money(salary))}",
                section_style,
            ),
            styled_table(shift_rows, [22 * mm, 38 * mm, 18 * mm, 18 * mm, 20 * mm, 47 * mm, 15 * mm]),
        ]
        story.append(KeepTogether(employee_block))
    if not entries_by_employee:
        story.append(Paragraph("Keine Schichten für diesen Monat vorhanden.", normal_style))

    doc.build(story)
    return filename, out.getvalue(), "application/pdf"


def log_time_export(month, filename, sent_to, status, message, auto=False):
    con = db()
    con.execute(
        """
        INSERT INTO time_export_logs (export_month, filename, sent_to, status, message, auto, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (month, filename, sent_to, status, message, 1 if auto else 0, berlin_now().strftime("%d.%m.%Y %H:%M")),
    )
    con.commit()
    con.close()


def latest_time_export_log():
    con = db()
    row = con.execute("SELECT * FROM time_export_logs ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row


def auto_time_export_attempted(month):
    con = db()
    row = con.execute("SELECT id FROM time_export_logs WHERE export_month=? AND auto=1 LIMIT 1", (month,)).fetchone()
    con.close()
    return bool(row)


def smtp_oeffnen(host, port):
    """Port 465 = SSL von Anfang an (Strato-Empfehlung), sonst STARTTLS (z. B. 587)."""
    port = int(port or 587)
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=30)
    verbindung = smtplib.SMTP(host, port, timeout=30)
    verbindung.starttls()
    return verbindung


def send_time_export_email(month, filename, data, content_type):
    settings = load_settings()
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    recipient = (settings.get("time_export_email") or "info@opapeters.de").strip()
    sender = os.environ.get("SMTP_FROM", smtp_user or recipient or "")
    if not (smtp_host and smtp_user and smtp_password and recipient and sender):
        raise RuntimeError("SMTP-Daten oder Empfängeradresse sind nicht vollständig konfiguriert.")
    msg = EmailMessage()
    msg["Subject"] = f"Zeiterfassung {month}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(f"Der Monats-Export der Zeiterfassung für {month} befindet sich im Anhang.")
    if content_type == "application/pdf":
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    elif "spreadsheet" in content_type:
        msg.add_attachment(data, maintype="application", subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=filename)
    else:
        msg.add_attachment(data, maintype="text", subtype="csv", filename=filename)
    with smtp_oeffnen(smtp_host, smtp_port) as smtp:
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)
    return recipient


def read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def push_public_pem():
    return (os.environ.get("PUSH_VAPID_PUBLIC_KEY") or read_text_file(os.environ.get("PUSH_VAPID_PUBLIC_KEY_FILE", "/etc/opa-peters/push_public.pem"))).strip()


def push_private_pem():
    return (os.environ.get("PUSH_VAPID_PRIVATE_KEY") or read_text_file(os.environ.get("PUSH_VAPID_PRIVATE_KEY_FILE", "/etc/opa-peters/push_private.pem"))).strip()


def push_private_key_for_webpush():
    configured_file = os.environ.get("PUSH_VAPID_PRIVATE_KEY_FILE", "/etc/opa-peters/push_private.pem")
    if configured_file and os.path.exists(configured_file):
        return configured_file
    return push_private_pem()


def push_contact_email():
    contact = (os.environ.get("PUSH_CONTACT_EMAIL") or "mailto:info@opapeters.de").strip()
    return contact if ":" in contact else f"mailto:{contact}"


def push_public_application_key():
    public_pem = push_public_pem()
    if not public_pem:
        return ""
    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
        numbers = key.public_numbers()
        raw = b"\x04" + int(numbers.x).to_bytes(32, "big") + int(numbers.y).to_bytes(32, "big")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    except Exception:
        return ""


def push_is_configured():
    return bool(push_public_application_key() and push_private_pem())


def push_subscription_rows(enabled_only=True):
    con = db()
    q = "SELECT * FROM push_subscriptions"
    if enabled_only:
        q += " WHERE enabled=1"
    rows = con.execute(q).fetchall()
    con.close()
    return rows


def push_subscription_count(enabled_only=True):
    try:
        return len(push_subscription_rows(enabled_only=enabled_only))
    except sqlite3.Error:
        return 0


def save_push_subscription(subscription, user_agent="", owner=""):
    endpoint = str((subscription or {}).get("endpoint") or "").strip()
    keys = subscription.get("keys") if isinstance(subscription, dict) else None
    if not endpoint or not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        return False
    now = berlin_now().strftime("%d.%m.%Y %H:%M")
    subscription_json = json.dumps(subscription, ensure_ascii=False, separators=(",", ":"))
    con = db()
    con.execute(
        """
        INSERT INTO push_subscriptions (endpoint, subscription_json, enabled, user_agent, owner, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            subscription_json=excluded.subscription_json,
            enabled=1,
            user_agent=excluded.user_agent,
            owner=excluded.owner,
            updated_at=excluded.updated_at
        """,
        (endpoint, subscription_json, (user_agent or "")[:500], (owner or "")[:120], now, now),
    )
    con.commit()
    con.close()
    return True


def disable_push_subscription(endpoint):
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return False
    con = db()
    cur = con.execute(
        "UPDATE push_subscriptions SET enabled=0, updated_at=? WHERE endpoint=?",
        (berlin_now().strftime("%d.%m.%Y %H:%M"), endpoint),
    )
    con.commit()
    con.close()
    return cur.rowcount > 0


def send_push_notification(payload):
    if not push_is_configured():
        return {"sent": 0, "failed": 0, "disabled": 0, "configured": False}
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        return {"sent": 0, "failed": 0, "disabled": 0, "configured": False}
    private_key = push_private_key_for_webpush()
    claims = {"sub": push_contact_email()}
    data = json.dumps(payload, ensure_ascii=False)
    sent = failed = disabled = 0
    for row in push_subscription_rows(enabled_only=True):
        try:
            webpush(
                subscription_info=json.loads(row["subscription_json"]),
                data=data,
                vapid_private_key=private_key,
                vapid_claims=claims,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                disable_push_subscription(row["endpoint"])
                disabled += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"sent": sent, "failed": failed, "disabled": disabled, "configured": True}


def send_time_entry_push(employee_name, work_location, work_date, start_time, end_time, duration_minutes, note="", created_by_admin=False):
    payload = {
        "title": "Neue Zeiterfassung",
        "body": f"{employee_name} · {work_location} · {start_time}-{end_time} · {format_decimal((duration_minutes or 0) / 60)} Std.",
        "url": "/admin/time?month=" + quote_plus((work_date or current_month())[:7]),
        "tag": f"time-{normalize_text_key(employee_name)}-{work_date}-{start_time}-{end_time}",
        "icon": "/static/icons/icon-192.png",
        "badge": "/static/icons/icon-192.png",
        "details": {
            "employee": employee_name,
            "location": work_location,
            "date": work_date,
            "start": start_time,
            "end": end_time,
            "duration": format_duration(duration_minutes),
            "note": note or "",
            "source": "Admin" if created_by_admin else "Mitarbeiter",
        },
    }
    return send_push_notification(payload)


def send_order_push(order_number, location, ordered_by, items, note=""):
    item_count = len(items or [])
    total_quantity = sum(int(item.get("quantity") or 0) for item in (items or []))
    payload = {
        "title": "Neue Bestellung",
        "body": f"{location} · {ordered_by} · {item_count} Position(en), {total_quantity} Stück",
        "url": "/admin/orders",
        "tag": f"order-{order_number}",
        "icon": "/static/icons/icon-192.png",
        "badge": "/static/icons/icon-192.png",
        "details": {
            "order_number": order_number,
            "location": location,
            "ordered_by": ordered_by,
            "items": item_count,
            "quantity": total_quantity,
            "note": note or "",
        },
    }
    return send_push_notification(payload)


def send_push_test_notification():
    return send_push_notification({
        "title": "Push-Test",
        "body": "Push-Benachrichtigung funktioniert auf diesem Gerät.",
        "url": "/admin/settings",
        "tag": "opa-peters-push-test",
        "icon": "/static/icons/icon-192.png",
        "badge": "/static/icons/icon-192.png",
    })


def maybe_run_auto_time_export():
    now = berlin_now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    if now.day != last_day or now.hour < 23 or (now.hour == 23 and now.minute < 55):
        return
    month = now.strftime("%Y-%m")
    if auto_time_export_attempted(month):
        return
    filename, data, content_type = build_time_export(month)
    try:
        recipient = send_time_export_email(month, filename, data, content_type)
        log_time_export(month, filename, recipient, "gesendet", "Automatischer Export wurde versendet.", auto=True)
        settings = load_settings()
        settings["last_auto_time_export_month"] = month
        settings["last_auto_time_export_status"] = f"gesendet am {berlin_now().strftime('%d.%m.%Y %H:%M')}"
        save_settings(settings)
    except Exception as exc:
        log_time_export(month, filename, load_settings().get("time_export_email", ""), "fehlgeschlagen", str(exc), auto=True)
        settings = load_settings()
        settings["last_auto_time_export_month"] = month
        settings["last_auto_time_export_status"] = f"fehlgeschlagen am {berlin_now().strftime('%d.%m.%Y %H:%M')}: {exc}"
        save_settings(settings)


def send_pdf_email_if_configured(order, pdf_filename):
    """Versendet die PDF optional per SMTP, wenn SMTP-Daten und Empfänger-Adresse gesetzt sind."""
    settings = load_settings()
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    central_email = (settings.get("order_email_to") or os.environ.get("CENTRAL_EMAIL") or "").strip()
    sender = os.environ.get("SMTP_FROM", smtp_user or central_email or "")
    if not (smtp_host and smtp_user and smtp_password and central_email and sender):
        return False

    msg = EmailMessage()
    msg["Subject"] = f"Interne Bestellung {order['order_number']} - {order['location']}"
    msg["From"] = sender
    msg["To"] = central_email
    msg.set_content(
        f"Eine neue interne Bestellung wurde erstellt.\n\n"
        f"Bestellnummer: {order['order_number']}\n"
        f"Standortzugang: {order.get('buyer_group', '')}\n"
        f"Standort: {order['location']}\n"
        f"Besteller: {order['ordered_by']}\n"
        f"Datum: {order['created_at']}\n\n"
        f"Die PDF-Bestellung befindet sich im Anhang."
    )
    with open(os.path.join(ORDER_DIR, pdf_filename), "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=pdf_filename)
    if order.get("order_image_filename"):
        image_path = os.path.join(ORDER_IMAGE_DIR, order["order_image_filename"])
        if os.path.exists(image_path):
            ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
            subtype = "jpeg" if ext in ["jpg", "jpeg"] else ext
            with open(image_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="image", subtype=subtype, filename=order["order_image_filename"])

    with smtp_oeffnen(smtp_host, smtp_port) as smtp:
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)
    return True


def read_order_pdf_data(pdf_filename):
    path = os.path.join(ORDER_DIR, os.path.basename(pdf_filename or ""))
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    order = get_order_by_pdf_filename(pdf_filename)
    if order and order["pdf_data"]:
        return bytes(order["pdf_data"])
    cached = get_cached_pdf_file(pdf_filename)
    if cached:
        return cached
    if order:
        items = [dict(item) for item in get_order_items(order["id"])]
        order_dict = dict(order)
        regenerated = create_pdf(order_dict, items)
        regenerated_path = os.path.join(ORDER_DIR, regenerated)
        if os.path.exists(regenerated_path):
            with open(regenerated_path, "rb") as f:
                data = f.read()
            con = db()
            con.execute("UPDATE orders SET pdf_data=? WHERE id=?", (data, order["id"]))
            con.commit()
            con.close()
            return data
    return None


def create_pdf(order, items):
    filename = f"bestellung_{order['order_number']}.pdf"
    path = os.path.join(ORDER_DIR, filename)
    doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=14 * mm, leftMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    table_text = ParagraphStyle("A4TableText", parent=styles["BodyText"], fontSize=8.5, leading=10.5, wordWrap="CJK")
    table_head = ParagraphStyle("A4TableHead", parent=table_text, fontName="Helvetica-Bold")
    story = []

    story.append(Paragraph("Interne Bestellung", styles["Title"]))
    story.append(Spacer(1, 6 * mm))
    meta = [
        ["Bestellnummer", order["order_number"]],
        ["Datum / Uhrzeit", order["created_at"]],
        ["Standortzugang", order.get("buyer_group", "")],
        ["Standort", order["location"]],
        ["Besteller", order["ordered_by"]],
    ]
    if order.get("note"):
        meta.append(["Bemerkung", order["note"]])
    if order.get("order_image_filename"):
        meta.append(["Bild zur Bestellung", "im Adminbereich gespeichert"])
    meta_rows = [[pdf_text(label, table_head), pdf_text(value, table_text)] for label, value in meta]
    t = Table(meta_rows, colWidths=[38 * mm, 136 * mm], repeatRows=0, splitByRow=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 8 * mm))

    grouped = {}
    for item in items:
        grouped.setdefault(item["source"] or "Ohne Bezugsquelle", []).append(item)

    for source in sorted(grouped.keys(), key=lambda x: x.lower()):
        story.append(Paragraph(f"Bezugsquelle: {esc(source)}", styles["Heading2"]))
        data = [pdf_table_row(["Produkt", "Kategorie", "Gebindegröße", "Menge"], table_head)]
        for item in sorted(grouped[source], key=lambda x: ((x.get("category") or "").lower(), x["product_name"].lower())):
            data.append(pdf_table_row([item["product_name"], item.get("category") or "Allgemein", item["package_size"], str(item["quantity"])], table_text))
        table = Table(data, colWidths=[76 * mm, 38 * mm, 46 * mm, 14 * mm], repeatRows=1, splitByRow=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 7 * mm))

    doc.build(story)
    return filename


def create_combined_order_pdf(orders):
    combined = {}
    order_rows = []
    note_rows = []
    for order in sorted(orders, key=lambda o: ((o["location"] or "").lower(), o["created_at"] or "", o["order_number"] or "")):
        order_rows.append([order["created_at"], order["order_number"], order["location"], order["ordered_by"]])
        if (order["note"] or "").strip():
            note_rows.append([order["created_at"], order["order_number"], order["location"], order["note"]])
        for item in get_order_items(order["id"]):
            key = (
                order["location"] or "Ohne Standort",
                item["source"] or "Ohne Bezugsquelle",
                item["product_name"] or "",
                item["category"] or "Allgemein",
                item["package_size"] or "",
            )
            combined.setdefault(
                key,
                {
                    "location": key[0],
                    "source": key[1],
                    "product_name": key[2],
                    "category": key[3],
                    "package_size": key[4],
                    "quantity": 0,
                },
            )
            combined[key]["quantity"] += int(item["quantity"] or 0)

        number = berlin_now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()
    filename = f"gesamtbestellung_{number}.pdf"
    path = os.path.join(ORDER_DIR, filename)
    doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=14 * mm, leftMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    table_text = ParagraphStyle("A4CombinedTableText", parent=styles["BodyText"], fontSize=7.6, leading=9.2, wordWrap="CJK")
    table_head = ParagraphStyle("A4CombinedTableHead", parent=table_text, fontName="Helvetica-Bold")
    note_text = ParagraphStyle("A4CombinedNoteText", parent=table_text, textColor=colors.red, fontName="Helvetica-Bold")
    note_head = ParagraphStyle("A4CombinedNoteHead", parent=note_text, fontName="Helvetica-Bold")
    story = [
        Paragraph("Gesamtbestellung", styles["Title"]),
        Spacer(1, 5 * mm),
        Paragraph(f"Erstellt am: {berlin_now().strftime('%d.%m.%Y %H:%M')}", styles["Normal"]),
        Paragraph(f"Zusammengefasste Bestellungen: {len(orders)}", styles["Normal"]),
        Spacer(1, 7 * mm),
        Paragraph("Ausgewählte Einzelbestellungen", styles["Heading2"]),
    ]
    order_data = [pdf_table_row(["Datum", "Bestellnr.", "Standort", "Besteller"], table_head)] + [pdf_table_row(row, table_text) for row in order_rows]
    order_table = Table(order_data, colWidths=[34 * mm, 40 * mm, 50 * mm, 50 * mm], repeatRows=1, splitByRow=1)
    order_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(order_table)
    if note_rows:
        story.extend([Spacer(1, 5 * mm), Paragraph("Bemerkungen aus den Einzelbestellungen", styles["Heading2"])])
        note_data = [pdf_table_row(["Datum", "Bestellnr.", "Standort", "Bemerkung"], note_head)] + [pdf_table_row(row, note_text) for row in note_rows]
        note_table = Table(note_data, colWidths=[34 * mm, 38 * mm, 42 * mm, 60 * mm], repeatRows=1, splitByRow=1)
        note_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.mistyrose),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.red),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(note_table)
    story.extend([Spacer(1, 7 * mm), Paragraph("Addierte Produktmengen", styles["Heading2"])])
    by_location = {}
    for item in combined.values():
        if item["quantity"] > 0:
            by_location.setdefault(item["location"], {}).setdefault(item["source"], []).append(item)
    for location in sorted(by_location.keys(), key=lambda value: value.lower()):
        story.append(Paragraph(f"Standort: {esc(location)}", styles["Heading2"]))
        for source in sorted(by_location[location].keys(), key=lambda value: value.lower()):
            story.append(Paragraph(f"Bezugsquelle: {esc(source)}", styles["Heading3"]))
            item_rows = [
                [item["category"], item["product_name"], item["package_size"], str(item["quantity"])]
                for item in sorted(by_location[location][source], key=lambda x: (x["category"].lower(), x["product_name"].lower(), x["package_size"].lower()))
            ]
            item_data = [pdf_table_row(["Kategorie", "Produkt", "Gebinde", "Menge"], table_head)] + [pdf_table_row(row, table_text) for row in item_rows]
            item_table = Table(item_data, colWidths=[36 * mm, 82 * mm, 42 * mm, 14 * mm], repeatRows=1, splitByRow=1)
            item_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(item_table)
            story.append(Spacer(1, 5 * mm))
        story.append(Spacer(1, 3 * mm))
    doc.build(story)
    return filename


ADMIN_NAV = [
    # (Adresse, Name, Symbol, zugehörige Seiten, nur am PC)
    ("/admin", "Übersicht", "🏠", ["/admin"], False),
    ("/admin/orders", "Bestellungen", "🧾", ["/admin/orders", "/admin/einkauf"], False),
    ("/admin/messages", "Nachrichten", "✉️", ["/admin/messages"], False),
    ("/admin/products", "Sortiment", "📦", ["/admin/products", "/admin/edit-product", "/admin/categories", "/admin/import"], True),
    ("/admin/time", "Team", "👥", ["/admin/time", "/admin/employees", "/admin/aufgaben", "/admin/cockpit-content", "/admin/task-report"], False),
    ("/admin/buchhaltung", "Buchhaltung", "📒", ["/admin/buchhaltung"], True),
    ("/admin/locations", "Standorte", "📍", ["/admin/locations", "/admin/location", "/admin/visibility"], True),
    ("/admin/settings", "Einstellungen", "⚙️", ["/admin/settings", "/admin/mehr"], True),
    ("/admin/mehr", "Mehr", "☰", ["/admin/mehr", "/admin/tagesabschluss", "/admin/buchhaltung", "/admin/products", "/admin/edit-product", "/admin/categories", "/admin/import", "/admin/locations", "/admin/location", "/admin/settings"], None),
]


def admin_nav_html():
    try:
        unread_messages = count_unread_messages("admin")
        open_orders = len([order for order in get_orders() if not order["completed_at"]])
    except Exception:
        unread_messages = open_orders = 0
    dots = {"/admin/orders": open_orders, "/admin/messages": unread_messages}
    links = []
    for href, label, icon, matches, desktop_only in ADMIN_NAV:
        css = "nav-pc" if desktop_only else ("nav-handy" if desktop_only is None else "")
        dot = f'<span class="nav-dot" aria-label="{dots[href]} neu"></span>' if dots.get(href) else ""
        links.append(
            f'<a class="nav-with-dot {css}" href="{href}" title="{esc(label)}" data-icon="{icon}" data-match="{" ".join(matches)}">{esc(label)}{dot}</a>'
        )
    return "".join(links)


def admin_meldungen(msg="", error=""):
    html_parts = []
    if msg:
        html_parts.append(f'<div class="vw-meldung ok" role="status">{esc(msg)}</div>')
    if error:
        html_parts.append(f'<div class="vw-meldung fehler" role="alert">{esc(error)}</div>')
    return "".join(html_parts)


AKTIV_ATTR = ' aria-current="page"'


def admin_kopf(titel, unter="", aktionen="", reiter=None, aktiv=""):
    """Einheitlicher Seitenkopf der Verwaltung mit optionalen Reitern."""
    reiter_html = ""
    if reiter:
        reiter_html = '<div class="vw-reiter" role="navigation" aria-label="Bereiche">' + "".join(
            f'<a href="{esc(href)}"{AKTIV_ATTR if key == aktiv else ""}>{esc(label)}</a>'
            for key, label, href in reiter
        ) + "</div>"
    return f"""
    <header class="vw-kopf">
        <div class="vw-kopf-text">
            <h2>{esc(titel)}</h2>
            {f'<p>{unter}</p>' if unter else ''}
        </div>
        {f'<div class="vw-kopf-aktionen">{aktionen}</div>' if aktionen else ''}
    </header>
    {reiter_html}
    """


SORTIMENT_REITER = [
    ("produkte", "Produkte", "/admin/products"),
    ("kategorien", "Warengruppen", "/admin/categories"),
    ("import", "Import", "/admin/import"),
]
TEAM_REITER = [
    ("zeiten", "Zeiten", "/admin/time"),
    ("personen", "Personen", "/admin/employees"),
    ("aufgaben", "Aufgaben & Aufträge", "/admin/aufgaben"),
    ("auswertung", "Auswertung", "/admin/task-report"),
]
BESTELL_REITER = [
    ("offen", "Offen", "/admin/orders"),
    ("einkauf", "Einkaufsliste nach Händler", "/admin/einkauf"),
    ("erledigt", "Erledigt", "/admin/orders?ansicht=erledigt"),
    ("rechnung", "Rechnungsstellung", "/admin/orders?ansicht=rechnung"),
]


def page(title, body, admin=False, buyer_key=None):
    if admin:
        nav = admin_nav_html()
    elif buyer_key:
        location = find_location(buyer_key)
        nav_links = []
        if location_can_order(location):
            nav_links.append('<a href="/">Bestellen</a>')
        nav_links.append('<a href="/cockpit">Mein Tag</a>')
        if location_can_time(location):
            nav_links.append('<a href="/time">Zeiten</a>')
        unread_messages = count_unread_messages("location", buyer_key)
        message_dot = '<span class="nav-dot" aria-label="Neue Nachrichten"></span>' if unread_messages else ""
        nav_links.append(f'<a class="nav-with-dot" href="/messages/select">Nachrichten{message_dot}</a>')
        nav_links.append('<a href="/logout">Abmelden</a>')
        nav = "".join(nav_links)
    else:
        nav = '<a href="/login">Standort Login</a><a href="/admin/login">Admin</a>'
    subtitle = "" if title == "Besteller Login" else "<p>Opa Peters · internes Bestellsystem</p>"
    # Der Push-Schalter steht jetzt nur noch in den Einstellungen.
    push_control = ""
    body_class = ' class="vw"' if admin else ""
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="theme-color" content="{THEME_COLOR}">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{APP_SHORT_NAME}">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/static/icons/icon-192.png">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<link rel="stylesheet" href="/static/style.css?v={ASSET_VERSION}">
</head>
<body{body_class}>
<header class="topbar"><div class="brand"><div class="brand-mark"><img src="/static/icons/opa-peters-logo.png" alt="Opa Peters"></div><div><h1>{esc(title)}</h1>{subtitle}</div></div><nav>{nav}</nav></header>
<main>{push_control}{body}</main>
<button id="installApp" class="install-app" hidden>App installieren</button>
<div id="iosInstallHelp" class="install-help" hidden>
  <div class="install-help-card">
    <button type="button" class="install-help-close" aria-label="Schließen">×</button>
    <h2>App zum Home-Bildschirm hinzufügen</h2>
    <p>Auf iPhone und iPad öffnest du unten das Teilen-Symbol und wählst danach <strong>Zum Home-Bildschirm</strong>.</p>
    <p class="muted">Danach erscheint das Bestellsystem wie eine App auf dem Home-Bildschirm.</p>
  </div>
</div>
<script>
(function(){{var p=location.pathname;document.querySelectorAll('.topbar nav a').forEach(function(a){{var h=a.getAttribute('href');var m=(a.getAttribute('data-match')||'').split(' ').filter(Boolean);var treffer=m.length?m.some(function(x){{return x==='/admin'?p==='/admin':(p===x||p.indexOf(x+'/')===0||p.indexOf(x+'-')===0);}}):(h===p||(h==='/messages/select'&&p.indexOf('/messages')===0));if(treffer){{a.setAttribute('aria-current','page');}}}});}})();
</script>
<script src="/static/app.js?v={ASSET_VERSION}" defer></script>
</body>
</html>""".encode("utf-8")


def admin_menu():
    return ""


def _altes_admin_menu():
    unread_messages = count_unread_messages("admin")
    open_orders = len([order for order in get_orders() if not order["completed_at"]])
    def badge(count):
        return f'<span class="nav-dot" aria-label="{count} neu"></span>' if count else ""
    groups = [
        ("Sortiment", "shop", [
            ("/admin", "Produkte", "P"),
            ("/admin/categories", "Kategorien", "K"),
            ("/admin/import", "Import", "I"),
            ("/admin/visibility", "Sichtbarkeit", "V"),
        ]),
        ("Bestellung & Cockpit", "orders", [
            ("/admin/orders", f"Bestellungen{badge(open_orders)}", "B"),
            ("/admin/cockpit-content", "Aufträge & Aufgaben", "A"),
            ("/admin/messages", f"Nachrichten{badge(unread_messages)}", "N"),
        ]),
        ("Personal", "people", [
            ("/admin/time", "Zeiterfassung", "Z"),
            ("/admin/employees", "Personen", "M"),
            ("/admin/task-report", "Aufgabenreport", "R"),
        ]),
        ("System", "system", [
            ("/admin/locations", "Standorte", "S"),
            ("/admin/settings", "Einstellungen", "E"),
            ("/admin/logout", "Admin Logout", "X"),
        ]),
    ]
    links = []
    for title, group_class, items in groups:
        group_links = "".join(
            f'<a class="button admin-nav-{group_class} {"logout-button" if href.endswith("/logout") else ""}" href="{href}"><span class="nav-icon" aria-hidden="true">{icon}</span><span>{label}</span></a>'
            for href, label, icon in items
        )
        links.append(f'<div class="admin-menu-group admin-menu-{group_class}"><strong>{title}</strong><div>{group_links}</div></div>')
    links = "".join(links)
    return """
    <details class="admin-mobile-menu">
        <summary>Admin-Menü</summary>
        <div class="admin-mobile-links">
            {links}
        </div>
    </details>
    <section class="admin-menu">
        {links}
    </section>
    """.format(links=links)



# ---------------------------------------------------------------- Buchhaltung: Mail + Monatserinnerung
def mail_senden(an, betreff, text):
    """Einfache Text-Mail über den eingerichteten SMTP-Zugang. -> (ok, fehlertext)"""
    host, user, passwort = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not (host and user and passwort):
        return False, "E-Mail-Versand ist auf dem Server nicht eingerichtet."
    msg = EmailMessage()
    msg["Subject"] = betreff
    msg["From"] = os.environ.get("SMTP_FROM", user)
    msg["To"] = an
    msg.set_content(text)
    try:
        with smtp_oeffnen(host, os.environ.get("SMTP_PORT", "587")) as smtp:
            smtp.login(user, passwort)
            smtp.send_message(msg)
        return True, ""
    except Exception as fehler:
        return False, str(fehler)[:200]


MONATSNAMEN = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]


def bh_erinnerung_text(monat):
    """Kurzer Stand eines Monats für die Erinnerung -> (betreff, zeilen, alles_gut)"""
    z = dict(bh.monats_zahlen(monat))
    con = bh.db()
    z["belege_neu"] = con.execute("SELECT COUNT(*) FROM belege WHERE substr(datum,1,7)=? AND status='neu'", (monat,)).fetchone()[0]
    z["unsicher"] = con.execute("SELECT COUNT(*) FROM belege WHERE status='unsicher'").fetchone()[0]
    z["portal"] = con.execute("SELECT COUNT(*) FROM belege WHERE substr(datum,1,7)=? AND status='portal'", (monat,)).fetchone()[0]
    con.close()
    name = f"{MONATSNAMEN[int(monat[5:]) - 1]} {monat[:4]}"
    zeilen = []
    if not z.get("gesamt"):
        zeilen.append("• Der Kontoauszug fehlt noch – bitte hochladen (Buchhaltung → Kontoauszüge).")
    if z.get("offen"):
        zeilen.append(f"• {z['offen']} Zahlung(en) ohne Beleg.")
    if z.get("vorschlag"):
        zeilen.append(f"• {z['vorschlag']} Vorschlag/Vorschläge warten auf ein kurzes OK.")
    if z.get("belege_neu"):
        zeilen.append(f"• {z['belege_neu']} neue Belege noch nicht geprüft.")
    if z.get("unsicher"):
        zeilen.append(f"• Bei {z['unsicher']} Dokument(en) ist unklar, ob es eine Rechnung ist – bitte kurz ansehen.")
    if z.get("portal"):
        zeilen.append(f"• {z['portal']} Rechnung(en) liegen nur im Kundenportal und müssen heruntergeladen werden.")
    alles_gut = not zeilen
    betreff = f"Buchhaltung {name}: " + ("alles bereit für den Export ✓" if alles_gut else f"{len(zeilen)} Punkt(e) offen")
    return betreff, zeilen, alles_gut


def bh_monatserinnerung():
    """Läuft einmal am Tag. Ab dem 3. des Monats einmalig Mail + Push für den Vormonat."""
    heute = bh.jetzt()
    if heute.day < 3:
        return
    vormonat = (heute.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    con = bh.db()
    schon = bh.einstellung(con, f"erinnerung_{vormonat}")
    abgeschlossen = con.execute("SELECT 1 FROM monate WHERE monat=? AND abgeschlossen_am IS NOT NULL AND abgeschlossen_am != ''", (vormonat,)).fetchone()
    hat_daten = con.execute("SELECT 1 FROM belege WHERE substr(datum,1,7)=? UNION SELECT 1 FROM buchungen WHERE substr(datum,1,7)=? LIMIT 1", (vormonat, vormonat)).fetchone()
    if schon or abgeschlossen or not hat_daten:
        con.close()
        return
    bh.setze_einstellung(con, f"erinnerung_{vormonat}", bh.jetzt_text())
    con.commit()
    con.close()
    betreff, zeilen, alles_gut = bh_erinnerung_text(vormonat)
    basis = os.environ.get("PUBLIC_BASE_URL", "https://bestellung.opapeters.de").rstrip("/")
    link = f"{basis}/admin/buchhaltung/export?monat={vormonat}" if alles_gut else f"{basis}/admin/buchhaltung?monat={vormonat}"
    text = ("Hallo,\n\n" + ("alle Zahlungen haben einen Beleg. Der Monat kann exportiert werden."
                          if alles_gut else "für den letzten Monat ist noch Folgendes offen:\n\n" + "\n".join(zeilen))
            + f"\n\nDirekt hier weitermachen:\n{link}\n\nDein Bestellsystem")
    con = bh.db()
    an = (bh.einstellung(con, "erinnerung_an") or load_settings().get("time_export_email") or "").strip()
    con.close()
    if an and email_sieht_gueltig_aus(an):
        ok, fehler = mail_senden(an, betreff, text)
        con = bh.db()
        bh.protokolliere(con, "erinnerung", f"{vormonat}: Mail an {an} " + ("gesendet" if ok else f"fehlgeschlagen ({fehler})"))
        if not ok:
            bh.setze_einstellung(con, f"erinnerung_{vormonat}", "")  # morgen nochmal versuchen
        con.commit()
        con.close()
    try:
        send_push_notification({"title": betreff, "body": " ".join(z.lstrip("• ") for z in zeilen)[:180] or "Bereit für den Export.",
                                "url": link.replace(basis, ""), "tag": f"buchhaltung-{vormonat}",
                                "icon": "/static/icons/icon-192.png", "badge": "/static/icons/icon-192.png"})
    except Exception:
        pass

# ---------------------------------------------------------------- Bestellungen: eine Sammel-Mail am Tag
TAGESMAIL_STUNDE = 6
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def bestellungen_vom_tag(tag):
    """Alle Bestellungen eines Kalendertags (tag = date), älteste zuerst."""
    con = db()
    zeilen = con.execute("SELECT id, order_number, location, ordered_by, buyer_group, note, pdf_filename, order_image_filename, created_at FROM orders").fetchall()
    con.close()
    treffer = [o for o in zeilen if parse_order_created_at(o["created_at"]).date() == tag]
    return sorted(treffer, key=lambda o: parse_order_created_at(o["created_at"]))


def tageszusammenfassung_bauen(tag):
    """-> (betreff, text, anhaenge[(name, bytes, maintype, subtype)]) oder None, wenn es keine Bestellungen gab."""
    orders = bestellungen_vom_tag(tag)
    if not orders:
        return None
    tag_text = f"{WOCHENTAGE[tag.weekday()]}, {tag.strftime('%d.%m.%Y')}"
    teile = [f"Guten Morgen,\n\nhier sind alle Bestellungen von {tag_text}: {len(orders)} Bestellung(en).\n"]
    haendler = {}
    for o in orders:
        items = get_order_items(o["id"])
        uhr = parse_order_created_at(o["created_at"]).strftime("%H:%M")
        teile.append(f"\n— {o['location']} · {uhr} Uhr · {o['ordered_by']} · Nr. {o['order_number']}")
        if (o["note"] or "").strip():
            teile.append(f"  Notiz: {o['note'].strip()}")
        for it in items:
            teile.append(f"  {it['quantity']} × {it['product_name']} ({it['package_size']})")
            h = haendler.setdefault(it["source"] or "Ohne Bezugsquelle", {})
            key = (it["product_name"], it["package_size"])
            eintrag = h.setdefault(key, {"menge": 0, "orte": {}})
            eintrag["menge"] += int(it["quantity"] or 0)
            eintrag["orte"][o["location"]] = eintrag["orte"].get(o["location"], 0) + int(it["quantity"] or 0)
    teile.append("\n\nZUSAMMEN NACH HÄNDLER")
    for name in sorted(haendler):
        teile.append(f"\n{name}")
        for (produkt, gebinde), e in sorted(haendler[name].items()):
            orte = ", ".join(f"{ort} {m}" for ort, m in e["orte"].items())
            teile.append(f"  {e['menge']} × {produkt} ({gebinde}) – {orte}")
    # Anhänge: alle Bestell-PDFs (+ Fotos), insgesamt höchstens ~18 MB
    anhaenge, groesse, weggelassen = [], 0, 0
    for o in orders:
        daten = None
        pfad = os.path.join(ORDER_DIR, o["pdf_filename"] or "")
        if o["pdf_filename"] and os.path.exists(pfad):
            with open(pfad, "rb") as f:
                daten = f.read()
        else:
            con = db()
            r = con.execute("SELECT pdf_data FROM orders WHERE id=?", (o["id"],)).fetchone()
            con.close()
            daten = r["pdf_data"] if r and r["pdf_data"] else None
        liste = [(o["pdf_filename"], daten, "application", "pdf")] if daten else []
        if o["order_image_filename"]:
            bild = os.path.join(ORDER_IMAGE_DIR, o["order_image_filename"])
            if os.path.exists(bild):
                ext = os.path.splitext(bild)[1].lower().lstrip(".") or "jpeg"
                with open(bild, "rb") as f:
                    liste.append((o["order_image_filename"], f.read(), "image", "jpeg" if ext in ("jpg", "jpeg") else ext))
        for a in liste:
            if groesse + len(a[1]) > 18 * 1024 * 1024:
                weggelassen += 1
                continue
            groesse += len(a[1])
            anhaenge.append(a)
    teile.append("\n\nDie einzelnen Bestellungen hängen als PDF an." + (f" ({weggelassen} Anhang/Anhänge waren zu groß und fehlen – sie liegen im Bestellsystem.)" if weggelassen else ""))
    basis = os.environ.get("PUBLIC_BASE_URL", "https://bestellung.opapeters.de").rstrip("/")
    teile.append(f"\nAlle Bestellungen im System: {basis}/admin/orders\n\nDein Bestellsystem")
    betreff = f"Bestellungen {tag_text}: {len(orders)} Bestellung(en)"
    return betreff, "\n".join(teile), anhaenge


def tageszusammenfassung_senden(tag):
    """-> (ok, text)"""
    settings = load_settings()
    an = (settings.get("order_email_to") or os.environ.get("CENTRAL_EMAIL") or "").strip()
    if not smtp_is_configured():
        return False, "E-Mail-Versand ist auf dem Server nicht eingerichtet."
    if not email_sieht_gueltig_aus(an):
        return False, "Keine gültige Adresse für Bestellungen eingetragen."
    inhalt = tageszusammenfassung_bauen(tag)
    if inhalt is None:
        return True, f"Am {tag.strftime('%d.%m.%Y')} gab es keine Bestellungen – keine Mail nötig."
    betreff, text, anhaenge = inhalt
    msg = EmailMessage()
    msg["Subject"] = betreff
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
    msg["To"] = an
    msg.set_content(text)
    for name, daten, haupt, unter in anhaenge:
        msg.add_attachment(daten, maintype=haupt, subtype=unter, filename=name)
    try:
        with smtp_oeffnen(os.environ.get("SMTP_HOST"), os.environ.get("SMTP_PORT", "587")) as smtp:
            smtp.login(os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"))
            smtp.send_message(msg)
    except Exception as fehler:
        return False, f"Senden fehlgeschlagen: {str(fehler)[:150]}"
    return True, f"Zusammenfassung an {an} gesendet."


def tagesmail_uhr_starten():
    """Hintergrund-Uhr: täglich ab 6 Uhr (Berlin) einmal die Bestellungen des Vortags verschicken."""
    import threading
    import time as _time

    def schleife():
        while True:
            try:
                jetzt_b = berlin_now()
                gestern = (jetzt_b - timedelta(days=1)).date()
                settings = load_settings()
                if jetzt_b.hour >= TAGESMAIL_STUNDE and settings.get("tagesmail_zuletzt") != gestern.isoformat():
                    ok, text = tageszusammenfassung_senden(gestern)
                    print(f"Tages-Mail Bestellungen {gestern}: {text}")
                    if ok:
                        settings = load_settings()
                        settings["tagesmail_zuletzt"] = gestern.isoformat()
                        save_settings(settings)
            except Exception as fehler:
                print(f"Tages-Mail Fehler: {fehler}")
            _time.sleep(300)

    threading.Thread(target=schleife, name="tagesmail", daemon=True).start()

# =================================================================
#   KASSE & TAGESABSCHLUSS (Lightspeed-Berichte, Kassenabschluss, Push am Abend)
# =================================================================
KASSE_FOTO_DIR = os.path.join(DATA_DIR, "kasse_fotos")


def kasse_init():
    os.makedirs(KASSE_FOTO_DIR, exist_ok=True)
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS kasse_abschluss (
            location_id TEXT NOT NULL, datum TEXT NOT NULL,
            umsatz INTEGER, bar INTEGER, karte INTEGER, gezaehlt INTEGER, entnommen INTEGER DEFAULT 0,
            entnahme_zweck TEXT DEFAULT '', notiz TEXT DEFAULT '', name TEXT DEFAULT '', foto TEXT DEFAULT '',
            erstellt TEXT, PRIMARY KEY (location_id, datum)
        );
        CREATE TABLE IF NOT EXISTS kasse_lightspeed (
            location_id TEXT NOT NULL, datum TEXT NOT NULL,
            umsatz INTEGER, netto INTEGER, bar INTEGER, karte INTEGER, bons INTEGER, storno INTEGER, rabatt INTEGER, trinkgeld INTEGER,
            kassendiff INTEGER, gezaehlt INTEGER,
            bericht_id INTEGER, erstellt TEXT, PRIMARY KEY (location_id, datum)
        );
        CREATE TABLE IF NOT EXISTS kasse_berichte (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dateiname TEXT, herkunft TEXT, empfangen TEXT,
            status TEXT, meldung TEXT, text_auszug TEXT
        );
        """
    )
    spalten = {r[1] for r in con.execute("PRAGMA table_info(kasse_lightspeed)")}
    for spalte in ("kassendiff", "gezaehlt"):
        if spalte not in spalten:
            con.execute(f"ALTER TABLE kasse_lightspeed ADD COLUMN {spalte} INTEGER")
    con.commit()
    con.close()


def kasse_einstellungen():
    s = load_settings().get("tagesabschluss") or {}
    return {
        "aktiv": s.get("aktiv", True),
        "uhrzeit": s.get("uhrzeit") or "22:30",
        "zeitpunkt": s.get("zeitpunkt") or "morgens",  # morgens = sobald der Lightspeed-Bericht da ist; abends = zur Uhrzeit
        "team_zaehlt": bool(s.get("team_zaehlt", False)),
        "mail_an": s.get("mail_an", ""),
        "schwelle": int(s.get("schwelle", 500)),  # Cent
        "standorte": s.get("standorte"),  # None = alle
        "wechselgeld": s.get("wechselgeld") or {},  # location_id -> Cent
        "lightspeed_namen": s.get("lightspeed_namen") or {},  # location_id -> Name in Lightspeed
        "absender": s.get("absender", ""),
        "zuletzt": s.get("zuletzt", ""),
    }


def kasse_einstellungen_speichern(neu):
    settings = load_settings()
    alt = settings.get("tagesabschluss") or {}
    alt.update(neu)
    settings["tagesabschluss"] = alt
    save_settings(settings)


def kasse_standorte():
    erlaubt = kasse_einstellungen()["standorte"]
    return [l for l in get_locations() if erlaubt is None or l["id"] in erlaubt]


def euro_text(cent_wert, vorzeichen=False):
    if cent_wert is None:
        return "–"
    text = f"{abs(cent_wert) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"
    if vorzeichen:
        return ("+" if cent_wert > 0 else "−" if cent_wert < 0 else "±") + text
    return ("−" if cent_wert < 0 else "") + text


def euro_kurz(cent_wert):
    if cent_wert is None:
        return "–"
    return f"{round(cent_wert / 100):,}".replace(",", ".") + " €"


# ---------------------------------------------------------- Lightspeed-Berichte lesen
LS_FELDER = [
    # (Feld, Muster für die Beschriftung) – Reihenfolge wichtig: Speziellere zuerst
    ("netto", r"netto\s*-?\s*umsatz|umsatz\s*netto|net\s*sales|net\s*revenue|nettoumsatz|total\s*excl|exkl\.?\s*mwst|summe\s*netto|^netto$"),
    ("umsatz", r"brutto\s*-?\s*umsatz|umsatz\s*brutto|gesamt\s*-?\s*umsatz|gross\s*sales|total\s*sales|total\s*revenue|sales\s*incl|total\s*incl|inkl\.?\s*mwst|summe\s*brutto|^umsatz$|^brutto$|^total$|^gesamt$|^sales$|^revenue$|^takings$|einnahmen"),
    ("bar", r"^bar$|^bar\s*geld|barzahlung|^cash$|cash\s*payments?|^bargeld"),
    ("karte", r"^karte|kartenzahlung|^card|cards?$|^ec\b|girocard|kredit\s*karte|credit\s*card|debit|visa|mastercard|maestro|amex|american\s*express|lightspeed\s*payments|v\s*pay"),
    ("bons", r"anzahl\s*bons|^bons$|belege\s*anzahl|anzahl\s*belege|receipts|number\s*of\s*(?:receipts|orders|transactions|checks)|^transaktionen|^transactions|^orders$|^checks$|^g(?:ä|ae)ste$|^covers$"),
    ("storno", r"storno|stornierung|cancell?ation|^voids?$|void\s*total"),
    ("rabatt", r"rabatt|discount|nachlass"),
    ("trinkgeld", r"trinkgeld|^tips?$|gratuit"),
    ("kassendiff", r"kassendifferenz|^differenz|difference|discrepanc|over\s*/\s*short|(?:ü|ue)berschuss\s*/\s*fehlbetrag|fehlbetrag"),
    ("gezaehlt", r"gez(?:ä|ae)hlt|counted|ist\s*-?\s*bestand|actual\s*cash"),
]
LS_MUSTER = [(f, re.compile(m, re.I)) for f, m in LS_FELDER]
LS_BETRAG = re.compile(r"-?\(?\s*(?:€|EUR)?\s*-?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|-?\(?\s*(?:€|EUR)?\s*-?\d+(?:[.,]\d{2})|-?\d+")
LS_DATUM = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b|\b(20\d{2})-(\d{2})-(\d{2})\b")


def _ls_zahl(text, ganzzahl=False):
    t = str(text if text is not None else "").strip().replace("€", "").replace("EUR", "").replace(" ", " ").strip()
    if not t:
        return None
    negativ = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = t.strip("()- ").replace(" ", "")
    if not re.match(r"^[\d.,]+$", t):
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") == 1 and len(t.split(".")[1]) == 3 and ganzzahl:
        t = t.replace(".", "")
    try:
        wert = float(t)
    except ValueError:
        return None
    wert = -wert if negativ else wert
    return int(round(wert)) if ganzzahl else int(round(wert * 100))


def _ls_datum(text):
    m = LS_DATUM.search(text or "")
    if not m:
        return ""
    try:
        if m.group(4):
            return date(int(m.group(4)), int(m.group(5)), int(m.group(6))).isoformat()
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return ""


def _ls_tabellen(inhalt, dateiname):
    """Datei -> Liste von Tabellen (Liste von Zeilen, Zellen als Text)."""
    endung = os.path.splitext(dateiname or "")[1].lower()
    ist_binaer = inhalt[:4] == b"%PDF" or inhalt[:2] == b"PK" or inhalt[:4] == b"\xd0\xcf\x11\xe0"
    if endung in (".csv", ".txt") or (endung not in (".xls", ".xlsx", ".pdf") and not ist_binaer):
        text = ""
        for kodierung in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = inhalt.decode(kodierung)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialekt = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
        except csv.Error:
            dialekt = csv.excel
            dialekt.delimiter = ";" if text.count(";") > text.count(",") else ","
        return [[[c.strip() for c in zeile] for zeile in csv.reader(io.StringIO(text), dialekt)]]
    if endung == ".xlsx" or inhalt[:2] == b"PK":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(inhalt), read_only=True, data_only=True)
        return [[["" if c is None else (c.strftime("%d.%m.%Y") if hasattr(c, "strftime") else str(c)) for c in zeile] for zeile in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    if endung == ".xls":
        import xlrd
        wb = xlrd.open_workbook(file_contents=inhalt)
        tabellen = []
        for ws in wb.sheets():
            zeilen = []
            for r in range(ws.nrows):
                zeile = []
                for c in range(ws.ncols):
                    zelle = ws.cell(r, c)
                    if zelle.ctype == xlrd.XL_CELL_DATE:
                        zeile.append(xlrd.xldate_as_datetime(zelle.value, wb.datemode).strftime("%d.%m.%Y"))
                    elif zelle.ctype == xlrd.XL_CELL_NUMBER:
                        zeile.append(str(zelle.value).replace(".", ",") if zelle.value != int(zelle.value) else str(int(zelle.value)))
                    else:
                        zeile.append(str(zelle.value))
                zeilen.append(zeile)
            tabellen.append(zeilen)
        return tabellen
    if endung == ".pdf" or inhalt[:4] == b"%PDF":
        text, _ = bh.pdf_lesen(inhalt)
        zeilen = []
        for z in text.splitlines():
            z = z.strip()
            if not z:
                continue
            # "Bar 1.234,56 €" -> ["Bar", "1.234,56 €"]; mehrere Spalten durch 2+ Leerzeichen
            teile = [t for t in re.split(r"\s{2,}|\t", z) if t.strip()]
            if len(teile) == 1:
                m = re.match(r"^(.*?[A-Za-zÄÖÜäöüß%)\]])\s+(-?\(?(?:€\s*)?[\d.,]+\s*(?:€|EUR)?\)?)\s*$", z)
                teile = [m.group(1), m.group(2)] if m else [z]
            zeilen.append(teile)
        return [zeilen]
    raise ValueError("Dateiformat nicht unterstützt")


def _ls_feld(beschriftung):
    b = re.sub(r"\s+", " ", str(beschriftung or "")).strip(" :")
    if not b or len(b) > 60:
        return None
    for feld in ("kassendiff", "gezaehlt"):
        if dict(LS_MUSTER)[feld].search(b):
            return feld
    for feld, muster in LS_MUSTER:
        if muster.search(b):
            return feld
    return None


def lightspeed_lesen(inhalt, dateiname, standard_datum=""):
    """Kassenbericht -> {'tage': [{datum, standort, umsatz, netto, bar, karte, bons, storno, rabatt, trinkgeld}], 'text': ...}
    Versteht Tabellen mit Kopfzeile (eine Zeile pro Tag/Standort) und Berichte im Stil 'Beschriftung | Betrag'."""
    tabellen = _ls_tabellen(inhalt, dateiname)
    flach = "\n".join(" | ".join(z) for t in tabellen for z in t)
    tage = []
    # 1) Tabelle mit Kopfzeile, in der eine Datums-Spalte und mind. eine Betragsspalte steht
    for t in tabellen:
        for i, kopf in enumerate(t[:15]):
            spalten = {}
            datum_spalte = standort_spalte = None
            for j, zelle in enumerate(kopf):
                z = str(zelle).strip().lower()
                if re.search(r"^(datum|date|tag|day|business\s*day|gesch(?:ä|ae)ftstag|business\s*date)$", z):
                    datum_spalte = j
                elif re.search(r"standort|location|filiale|business\s*location|^gesch(?:ä|ae)ft$|^store$|^shop$", z):
                    standort_spalte = j
                else:
                    feld = _ls_feld(zelle)
                    if feld and feld not in spalten:
                        spalten[feld] = j
            if datum_spalte is None or not spalten:
                continue
            for zeile in t[i + 1:]:
                if datum_spalte >= len(zeile):
                    continue
                d = _ls_datum(zeile[datum_spalte])
                if not d:
                    continue
                eintrag = {"datum": d, "standort": zeile[standort_spalte] if standort_spalte is not None and standort_spalte < len(zeile) else ""}
                for feld, j in spalten.items():
                    eintrag[feld] = _ls_zahl(zeile[j], ganzzahl=(feld == "bons")) if j < len(zeile) else None
                tage.append(eintrag)
            if tage:
                break
        if tage:
            break
    # 2) Bericht im Stil "Beschriftung | Betrag" (z. B. Tagesbericht als PDF)
    if not tage:
        eintrag = {"datum": "", "standort": ""}
        zahlungsarten = {"bar": 0, "karte": 0}
        gefunden_zahlung = {"bar": False, "karte": False}
        for t in tabellen:
            for zeile in t:
                zellen = [str(z).strip() for z in zeile if str(z).strip()]
                if len(zellen) < 2:
                    continue
                feld = _ls_feld(zellen[0])
                if not feld:
                    continue
                # letzte Zahl in der Zeile = Betrag (bei "Anzahl | Betrag" die Summe)
                werte = [z for z in zellen[1:] if _ls_zahl(z, ganzzahl=(feld == "bons")) is not None]
                if not werte:
                    continue
                wert = _ls_zahl(werte[-1], ganzzahl=(feld == "bons"))
                if feld in ("bar", "karte"):
                    # mehrere Kartenarten (Visa, Mastercard, …) aufsummieren
                    zahlungsarten[feld] += wert
                    gefunden_zahlung[feld] = True
                elif eintrag.get(feld) is None:
                    eintrag[feld] = wert
        for f in ("bar", "karte"):
            if gefunden_zahlung[f]:
                eintrag[f] = zahlungsarten[f]
        if any(eintrag.get(f) is not None for f in ("umsatz", "netto", "bar", "karte", "kassendiff", "gezaehlt")):
            eintrag["datum"] = _ls_datum(flach[:1500]) or standard_datum
            if eintrag.get("umsatz") is None and eintrag.get("bar") is not None and eintrag.get("karte") is not None:
                eintrag["umsatz"] = eintrag["bar"] + eintrag["karte"]
            tage.append(eintrag)
    return {"tage": tage, "text": flach[:4000]}


def kasse_standort_finden(name, text=""):
    """Lightspeed-Standortname -> location_id (über eingetragene Namen oder Namensähnlichkeit)."""
    namen = kasse_einstellungen()["lightspeed_namen"]
    kandidaten = kasse_standorte() or get_locations()
    suchtext = normalize_text_key(name or "")
    for loc in kandidaten:
        eigener = normalize_text_key(namen.get(loc["id"], ""))
        if eigener and suchtext and (eigener in suchtext or suchtext in eigener):
            return loc["id"]
    for loc in kandidaten:
        if suchtext and normalize_text_key(loc["name"]) in suchtext:
            return loc["id"]
    gesamt = normalize_text_key(text or "")
    treffer = [loc["id"] for loc in kandidaten if (normalize_text_key(namen.get(loc["id"], "")) and normalize_text_key(namen.get(loc["id"], "")) in gesamt)]
    treffer = treffer or [loc["id"] for loc in kandidaten if normalize_text_key(loc["name"]) and normalize_text_key(loc["name"]) in gesamt]
    if len(treffer) == 1:
        return treffer[0]
    if len(kandidaten) == 1:
        return kandidaten[0]["id"]
    return None


def kassenbericht_importieren(inhalt, dateiname, herkunft, standard_datum=""):
    """-> (anzahl_tage, meldung)"""
    jetzt_t = berlin_now().strftime("%Y-%m-%d %H:%M")
    try:
        ergebnis = lightspeed_lesen(inhalt, dateiname, standard_datum)
    except Exception as fehler:
        ergebnis, meldung = {"tage": [], "text": ""}, f"Datei nicht lesbar ({str(fehler)[:80]})"
    else:
        meldung = ""
    con = db()
    cur = con.execute("INSERT INTO kasse_berichte (dateiname, herkunft, empfangen, status, meldung, text_auszug) VALUES (?, ?, ?, '', '', ?)",
                      (dateiname[:200], herkunft[:200], jetzt_t, ergebnis.get("text", "")[:4000]))
    bericht_id = cur.lastrowid
    gespeichert, ohne_standort = [], 0
    for tag in ergebnis["tage"]:
        loc = kasse_standort_finden(tag.get("standort", ""), ergebnis.get("text", "") + " " + dateiname)
        if not loc or not tag.get("datum"):
            ohne_standort += 1
            continue
        con.execute(
            """INSERT INTO kasse_lightspeed (location_id, datum, umsatz, netto, bar, karte, bons, storno, rabatt, trinkgeld, kassendiff, gezaehlt, bericht_id, erstellt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(location_id, datum) DO UPDATE SET
                 umsatz=COALESCE(excluded.umsatz, umsatz), netto=COALESCE(excluded.netto, netto), bar=COALESCE(excluded.bar, bar),
                 karte=COALESCE(excluded.karte, karte), bons=COALESCE(excluded.bons, bons), storno=COALESCE(excluded.storno, storno),
                 rabatt=COALESCE(excluded.rabatt, rabatt), trinkgeld=COALESCE(excluded.trinkgeld, trinkgeld),
                 kassendiff=COALESCE(excluded.kassendiff, kassendiff), gezaehlt=COALESCE(excluded.gezaehlt, gezaehlt),
                 bericht_id=excluded.bericht_id, erstellt=excluded.erstellt""",
            (loc, tag["datum"], tag.get("umsatz"), tag.get("netto"), tag.get("bar"), tag.get("karte"), tag.get("bons"),
             tag.get("storno"), tag.get("rabatt"), tag.get("trinkgeld"), tag.get("kassendiff"), tag.get("gezaehlt"), bericht_id, jetzt_t))
        gespeichert.append((loc, tag["datum"]))
    if not meldung:
        if gespeichert:
            meldung = f"{len(gespeichert)} Tag(e) übernommen" + (f", {ohne_standort} ohne passenden Standort" if ohne_standort else "")
        elif ohne_standort:
            meldung = "Zahlen gefunden, aber keinem Standort zuordenbar – bitte unter Tagesabschluss → Einstellungen den Lightspeed-Namen je Standort eintragen."
        else:
            meldung = "Keine Umsatzzahlen erkannt – bitte diesen Bericht an Claude schicken, damit das Auslesen angepasst wird."
    con.execute("UPDATE kasse_berichte SET status=?, meldung=? WHERE id=?", ("ok" if gespeichert else "fehler", meldung, bericht_id))
    con.commit()
    con.close()
    return gespeichert, meldung


def kasse_mail_haken(mail, anhaenge, text):
    """Wird für jede abgeholte Mail aufgerufen. Kassenberichte (Lightspeed) -> Tagesabschluss statt Beleg."""
    absender = (kasse_einstellungen()["absender"] or "").lower().strip()
    kennung = " ".join([mail.get("von", ""), mail.get("betreff", "")]).lower()
    if not ("lightspeed" in kennung or (absender and absender in kennung)):
        return False
    dateien = [a for a in anhaenge if os.path.splitext(a[0])[1].lower() in (".csv", ".xls", ".xlsx", ".pdf", ".txt")]
    if not dateien:
        return False
    # Tagesberichte kommen morgens um 5 und betreffen den Vortag
    try:
        standard = (datetime.strptime(mail.get("datum", "")[:10], "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    except ValueError:
        standard = ""
    alle = []
    for name, _typ, inhalt in dateien:
        gespeichert, _ = kassenbericht_importieren(inhalt, name, f"Mail von {mail.get('von_adresse', '')}: {mail.get('betreff', '')}", standard)
        alle += gespeichert
    if alle:
        try:
            kasse_nach_import(sorted({d for _, d in alle}))
        except Exception as fehler:
            print(f"Tagesabschluss nach Import fehlgeschlagen: {fehler}")
    return True


def kasse_nach_import(daten_liste):
    einst = kasse_einstellungen()
    gestern = (berlin_now().date() - timedelta(days=1)).isoformat()
    if einst["aktiv"] and einst["zeitpunkt"] == "morgens" and gestern in daten_liste and einst["zuletzt"] != gestern:
        kasse_einstellungen_speichern({"zuletzt": gestern})
        tagesabschluss_senden(gestern)
    elif einst["team_zaehlt"]:
        kasse_abgleich_melden(daten_liste)


# ---------------------------------------------------------- Tageszahlen zusammenstellen
def kasse_tag(location_id, datum):
    con = db()
    ls = con.execute("SELECT * FROM kasse_lightspeed WHERE location_id=? AND datum=?", (location_id, datum)).fetchone()
    ab = con.execute("SELECT * FROM kasse_abschluss WHERE location_id=? AND datum=?", (location_id, datum)).fetchone()
    con.close()
    return (dict(ls) if ls else None), (dict(ab) if ab else None)


def kasse_umsatz(location_id, datum):
    ls, ab = kasse_tag(location_id, datum)
    if ls and ls.get("umsatz") is not None:
        return ls["umsatz"]
    if ab and ab.get("umsatz") is not None:
        return ab["umsatz"]
    return None


def kasse_vergleich(location_id, datum):
    """-> (vorwoche, schnitt_4_wochen) für den gleichen Wochentag"""
    tag = datetime.strptime(datum, "%Y-%m-%d").date()
    werte = [kasse_umsatz(location_id, (tag - timedelta(days=7 * w)).isoformat()) for w in range(1, 5)]
    vorwoche = werte[0]
    vorhanden = [w for w in werte if w is not None]
    schnitt = round(sum(vorhanden) / len(vorhanden)) if len(vorhanden) >= 2 else None
    return vorwoche, schnitt


def kasse_differenz(location_id, ls, ab):
    """Bargeld gezählt + Entnahmen − Wechselgeld − Barumsatz. None, wenn nicht genug Angaben."""
    if ls and ls.get("kassendiff") is not None:
        return ls["kassendiff"]  # Kassenzählung direkt in Lightspeed (Cash drawer report)
    if not ab or ab.get("gezaehlt") is None:
        return None
    bar = ls["bar"] if ls and ls.get("bar") is not None else ab.get("bar")
    if bar is None:
        return None
    wechsel = int(kasse_einstellungen()["wechselgeld"].get(location_id, 0) or 0)
    return ab["gezaehlt"] + (ab.get("entnommen") or 0) - wechsel - bar


def team_am_tag(location, datum):
    con = db()
    zeilen = con.execute("SELECT * FROM time_entries WHERE work_date=? ORDER BY start_time", (datum,)).fetchall()
    con.close()
    schluessel = normalize_text_key(location["name"])
    eigene = [z for z in zeilen if z["location_id"] == location["id"] or schluessel and schluessel in normalize_text_key(z["work_location"])]
    return eigene


def aufgaben_am_tag(location, datum):
    """(erledigt, gesamt, offene_titel) – nur für heute verlässlich, sonst None."""
    if datum != berlin_now().date().isoformat():
        return None
    tasks = [t for t in normalize_cockpit_tasks(location.get("cockpit_tasks", [])) if t.get("active")]
    if not tasks:
        return None
    states = get_cockpit_state_details(location["id"])
    offen = [t["title"] for t in tasks if not (states.get(("task", t["id"]), {}).get("state") == "done" and cockpit_state_is_from_today(states.get(("task", t["id"]))))]
    return len(tasks) - len(offen), len(tasks), offen


def tagesabschluss_daten(datum):
    einst = kasse_einstellungen()
    standorte = []
    hinweise = []
    summe, summe_vorwoche, summe_vorhanden = 0, 0, False
    for loc in kasse_standorte():
        ls, ab = kasse_tag(loc["id"], datum)
        umsatz = ls["umsatz"] if ls and ls.get("umsatz") is not None else (ab.get("umsatz") if ab else None)
        bar = ls["bar"] if ls and ls.get("bar") is not None else (ab.get("bar") if ab else None)
        karte = ls["karte"] if ls and ls.get("karte") is not None else (ab.get("karte") if ab else None)
        if karte is None and umsatz is not None and bar is not None:
            karte = umsatz - bar
        vorwoche, schnitt = kasse_vergleich(loc["id"], datum)
        team = team_am_tag(loc, datum)
        minuten = sum(int(z["duration_minutes"] or 0) for z in team)
        notizen = [(z["employee_name"], z["note"].strip()) for z in team if (z["note"] or "").strip()]
        diff = kasse_differenz(loc["id"], ls, ab)
        aufgaben = aufgaben_am_tag(loc, datum)
        name = loc["name"]
        # Auffälligkeiten
        if diff is not None and abs(diff) >= einst["schwelle"]:
            hinweise.append(("rot", f"{name}: Kassendifferenz {euro_text(diff, True)}"))
        if umsatz is None and not ab:
            hinweise.append(("grau", f"{name}: kein Kassenabschluss eingetragen" if einst["team_zaehlt"] else f"{name}: noch keine Kassenzahlen von Lightspeed"))
        if umsatz is not None and schnitt:
            abw = umsatz / schnitt - 1
            if abw <= -0.3:
                hinweise.append(("honig", f"{name}: Umsatz {round(-abw * 100)} % unter dem üblichen {WOCHENTAGE[datetime.strptime(datum, '%Y-%m-%d').weekday()]}"))
            elif abw >= 0.4:
                hinweise.append(("gruen", f"{name}: Umsatz {round(abw * 100)} % über dem üblichen {WOCHENTAGE[datetime.strptime(datum, '%Y-%m-%d').weekday()]}"))
        if umsatz and not team:
            hinweise.append(("honig", f"{name}: Umsatz, aber keine Arbeitszeiten eingetragen"))
        if ls and umsatz and ls.get("storno") and abs(ls["storno"]) >= max(2000, umsatz * 0.03):
            hinweise.append(("honig", f"{name}: viele Stornos ({euro_text(abs(ls['storno']))})"))
        if ls and ab and ab.get("umsatz") is not None and ls.get("umsatz") is not None and abs(ls["umsatz"] - ab["umsatz"]) >= 100:
            hinweise.append(("honig", f"{name}: Lightspeed {euro_text(ls['umsatz'])}, Team hat {euro_text(ab['umsatz'])} eingetragen"))
        if aufgaben and aufgaben[2]:
            hinweise.append(("grau", f"{name}: {len(aufgaben[2])} von {aufgaben[1]} Tagesaufgaben offen"))
        if umsatz is not None:
            summe += umsatz
            summe_vorhanden = True
            if vorwoche is not None:
                summe_vorwoche += vorwoche
        standorte.append({
            "loc": loc, "ls": ls, "ab": ab, "umsatz": umsatz, "bar": bar, "karte": karte,
            "bons": ls.get("bons") if ls else None, "storno": ls.get("storno") if ls else None, "rabatt": ls.get("rabatt") if ls else None,
            "vorwoche": vorwoche, "schnitt": schnitt, "team": team, "minuten": minuten, "notizen": notizen,
            "diff": diff, "aufgaben": aufgaben,
            "pro_stunde": round(umsatz / (minuten / 60)) if umsatz and minuten else None,
        })
    vergleich = round((summe / summe_vorwoche - 1) * 100) if summe_vorhanden and summe_vorwoche else None
    return {"datum": datum, "standorte": standorte, "hinweise": hinweise, "summe": summe if summe_vorhanden else None, "vergleich": vergleich}


def tagesabschluss_kurztext(daten):
    tag = datetime.strptime(daten["datum"], "%Y-%m-%d")
    titel = f"Tagesabschluss {WOCHENTAGE_KURZ[tag.weekday()]} {tag.strftime('%d.%m.')}: " + (euro_kurz(daten["summe"]) if daten["summe"] is not None else "noch keine Kassenzahlen")
    if daten["vergleich"] is not None:
        titel += f" ({daten['vergleich']:+d} % zur Vorwoche)"
    wichtige = [t for f, t in daten["hinweise"] if f in ("rot", "honig")]
    if wichtige:
        text = f"{len(wichtige)} Hinweis(e): " + " · ".join(wichtige[:3])
    else:
        teile = [f"{s['loc']['name']} {euro_kurz(s['umsatz'])}" for s in daten["standorte"] if s["umsatz"] is not None]
        text = " · ".join(teile) or "Keine Auffälligkeiten."
    notizen = sum(len(s["notizen"]) for s in daten["standorte"])
    if notizen:
        text += f" · {notizen} Schichtnotiz(en)"
    return titel, text


def tagesabschluss_senden(datum):
    daten = tagesabschluss_daten(datum)
    titel, text = tagesabschluss_kurztext(daten)
    ergebnis = send_push_notification({"title": titel, "body": text[:220], "url": f"/admin/tagesabschluss?datum={datum}",
                                       "tag": f"tagesabschluss-{datum}", "icon": "/static/icons/icon-192.png", "badge": "/static/icons/icon-192.png"})
    an = kasse_einstellungen()["mail_an"]
    if an and email_sieht_gueltig_aus(an):
        basis = os.environ.get("PUBLIC_BASE_URL", "https://bestellung.opapeters.de").rstrip("/")
        zeilen = [text, ""]
        for s in daten["standorte"]:
            zeilen.append(f"{s['loc']['name']}: Umsatz {euro_text(s['umsatz'])} · Bar {euro_text(s['bar'])} · Karte {euro_text(s['karte'])}"
                          + (f" · Kasse {euro_text(s['diff'], True)}" if s["diff"] is not None else ""))
            for person, notiz in s["notizen"]:
                zeilen.append(f"   Notiz {person}: {notiz}")
        zeilen += ["", f"Alles im Detail: {basis}/admin/tagesabschluss?datum={datum}"]
        mail_senden(an, titel, "\n".join(zeilen))
    return ergebnis


def kasse_abgleich_melden(daten_liste):
    """Nach dem Lightspeed-Import: Push, wenn Lightspeed-Zahlen eine Kassendifferenz zeigen, die abends noch nicht sichtbar war."""
    einst = kasse_einstellungen()
    meldungen = []
    for datum in daten_liste:
        for loc in kasse_standorte():
            ls, ab = kasse_tag(loc["id"], datum)
            if not ls or not ab:
                continue
            diff_ls = kasse_differenz(loc["id"], ls, ab)
            diff_team = kasse_differenz(loc["id"], None, ab)
            if diff_ls is not None and abs(diff_ls) >= einst["schwelle"] and (diff_team is None or abs(diff_team - diff_ls) >= 100):
                meldungen.append(f"{loc['name']} {datetime.strptime(datum, '%Y-%m-%d').strftime('%d.%m.')}: {euro_text(diff_ls, True)}")
    if meldungen:
        send_push_notification({"title": "Kassendifferenz laut Lightspeed", "body": " · ".join(meldungen)[:220],
                                "url": f"/admin/tagesabschluss?datum={daten_liste[-1]}", "tag": "kasse-abgleich",
                                "icon": "/static/icons/icon-192.png", "badge": "/static/icons/icon-192.png"})


def tagesabschluss_uhr_starten():
    import threading
    import time as _time

    def schleife():
        while True:
            try:
                einst = kasse_einstellungen()
                jetzt_b = berlin_now()
                heute = jetzt_b.date().isoformat()
                gestern = (jetzt_b.date() - timedelta(days=1)).isoformat()
                if einst["aktiv"] and einst["zeitpunkt"] == "abends" and jetzt_b.strftime("%H:%M") >= einst["uhrzeit"] and einst["zuletzt"] != heute:
                    kasse_einstellungen_speichern({"zuletzt": heute})
                    tagesabschluss_senden(heute)
                # morgens: kommt normalerweise direkt nach dem Lightspeed-Bericht; spätestens um 9 Uhr auch ohne
                if einst["aktiv"] and einst["zeitpunkt"] == "morgens" and jetzt_b.hour >= 9 and einst["zuletzt"] < gestern:
                    kasse_einstellungen_speichern({"zuletzt": gestern})
                    con = db()
                    schon_mal_bericht = con.execute("SELECT 1 FROM kasse_berichte LIMIT 1").fetchone()
                    con.close()
                    if schon_mal_bericht or einst["team_zaehlt"]:  # erst melden, wenn Lightspeed überhaupt eingerichtet ist
                        tagesabschluss_senden(gestern)
            except Exception as fehler:
                print(f"Tagesabschluss Fehler: {fehler}")
            _time.sleep(60)

    threading.Thread(target=schleife, name="tagesabschluss", daemon=True).start()

# =====================================================================
#   VERWALTUNG NEU (November 2026) – gemeinsame Hilfsfunktionen
# =====================================================================

WOCHENTAGE_KURZ = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def smtp_is_configured():
    return all(os.environ.get(key) for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


def email_sieht_gueltig_aus(address):
    address = str(address or "").strip()
    if not address or " " in address or address.count("@") != 1:
        return False
    domain = address.split("@", 1)[1]
    return "." in domain and not domain.startswith(".") and not domain.endswith(".")


def freundliches_datum(value, mit_zeit=True):
    """'heute, 12:15' · 'gestern, 09:02' · 'Mo 16.11., 08:30' · '03.10.2025'"""
    text = str(value or "").strip()
    moment = parse_order_created_at(text)
    if moment == datetime.min:
        return text
    heute = berlin_now().date()
    tag = moment.date()
    zeit = moment.strftime("%H:%M") if (mit_zeit and (moment.hour or moment.minute)) else ""
    if tag == heute:
        datum = "heute"
    elif tag == heute - timedelta(days=1):
        datum = "gestern"
    elif abs((heute - tag).days) < 7:
        datum = f"{WOCHENTAGE_KURZ[tag.weekday()]} {tag.strftime('%d.%m.')}"
    elif tag.year == heute.year:
        datum = tag.strftime("%d.%m.")
    else:
        datum = tag.strftime("%d.%m.%Y")
    return f"{datum}, {zeit}" if zeit else datum


def stunden_text(minutes, mit_vorzeichen=False):
    """Einheitliche Stundenanzeige: 82:30 Std. bzw. +2:30 / −7:30"""
    minutes = int(round(minutes or 0))
    vorzeichen = ""
    if mit_vorzeichen:
        vorzeichen = "+" if minutes > 0 else ("−" if minutes < 0 else "±")
    minutes = abs(minutes)
    return f"{vorzeichen}{minutes // 60}:{minutes % 60:02d} Std."


def offene_bestellungen():
    return [order for order in get_orders() if not order["completed_at"]]


def bestell_positionen(order_ids, nur_offene=True):
    """Alle Positionen der angegebenen Bestellungen in einem Rutsch."""
    order_ids = [int(order_id) for order_id in order_ids if str(order_id).isdigit()]
    if not order_ids:
        return []
    placeholders = ",".join("?" for _ in order_ids)
    con = db()
    query = f"SELECT * FROM order_items WHERE deleted_at IS NULL AND order_id IN ({placeholders})"
    if nur_offene:
        query += " AND completed_at IS NULL"
    rows = con.execute(query, order_ids).fetchall()
    con.close()
    return rows


def einkaufsliste_offen():
    """Offene Positionen aller Standorte, gruppiert nach Händler (Bezugsquelle)."""
    orders = offene_bestellungen()
    orders_by_id = {order["id"]: order for order in orders}
    haendler = {}
    for item in bestell_positionen(orders_by_id.keys()):
        order = orders_by_id.get(item["order_id"])
        if not order:
            continue
        source = (item["source"] or "").strip() or "Ohne Händler"
        key = (normalize_text_key(item["product_name"]), normalize_text_key(item["package_size"]))
        entry = haendler.setdefault(source, {}).setdefault(key, {
            "product_name": item["product_name"] or "",
            "package_size": item["package_size"] or "",
            "category": item["category"] or "",
            "total": 0,
            "by_location": {},
            "item_ids": [],
        })
        quantity = int(item["quantity"] or 0)
        entry["total"] += quantity
        location = order["location"] or "Ohne Standort"
        entry["by_location"][location] = entry["by_location"].get(location, 0) + quantity
        entry["item_ids"].append(str(item["id"]))
    return haendler


def bestellungen_automatisch_erledigen():
    """Eine Bestellung, deren Positionen alle abgehakt sind, gilt als erledigt."""
    con = db()
    rows = con.execute(
        """
        SELECT o.id FROM orders o
        WHERE o.completed_at IS NULL
          AND EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id AND i.deleted_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id AND i.deleted_at IS NULL AND i.completed_at IS NULL)
        """
    ).fetchall()
    con.close()
    ids = [str(row["id"]) for row in rows]
    if ids:
        set_orders_completed(ids, completed=True)
    return len(ids)


def create_supplier_pdf(source, entries):
    """Einkaufsliste für einen Händler als A4-PDF."""
    number = berlin_now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()
    filename = f"einkauf_{slug_filename(source).rsplit('.', 1)[0][:30]}_{number}.pdf"
    path = os.path.join(ORDER_DIR, filename)
    doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=14 * mm, leftMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    text = ParagraphStyle("EinkaufText", parent=styles["BodyText"], fontSize=8.6, leading=10.5, wordWrap="CJK")
    head = ParagraphStyle("EinkaufHead", parent=text, fontName="Helvetica-Bold")
    story = [
        Paragraph(f"Einkaufsliste: {esc(source)}", styles["Title"]),
        Paragraph(f"Erstellt am {berlin_now().strftime('%d.%m.%Y %H:%M')} · alle offenen Bestellungen", styles["Normal"]),
        Spacer(1, 6 * mm),
    ]
    rows = [pdf_table_row(["Produkt", "Gebinde", "Menge", "Aufteilung je Standort"], head)]
    for entry in sorted(entries, key=lambda e: (e["category"].lower(), e["product_name"].lower())):
        aufteilung = ", ".join(f"{loc}: {qty}" for loc, qty in sorted(entry["by_location"].items()))
        rows.append(pdf_table_row([entry["product_name"], entry["package_size"], str(entry["total"]), aufteilung], text))
    table = Table(rows, colWidths=[70 * mm, 38 * mm, 16 * mm, 58 * mm], repeatRows=1, splitByRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 1), (2, -1), "RIGHT"),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    doc.build(story)
    with open(path, "rb") as f:
        store_pdf_file(filename, f.read())
    return filename


def rename_category(old_name, new_name):
    """Benennt eine Warengruppe um – auch bei allen Produkten. Gibt die Zahl geänderter Produkte zurück."""
    old_key = normalize_text_key(old_name)
    con = db()
    changed = 0
    for row in con.execute("SELECT id, category FROM products WHERE deleted_at IS NULL").fetchall():
        categories = product_categories(row)
        if any(normalize_text_key(c) == old_key for c in categories):
            updated = []
            for category in categories:
                category = new_name if normalize_text_key(category) == old_key else category
                if normalize_text_key(category) not in {normalize_text_key(u) for u in updated}:
                    updated.append(category)
            con.execute("UPDATE products SET category=? WHERE id=?", (category_text(updated), row["id"]))
            changed += 1
    jetzt = berlin_now().isoformat(timespec="seconds")
    new_row = con.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (new_name,)).fetchone()
    old_row = con.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE AND deleted_at IS NULL", (old_name,)).fetchone()
    if new_row and (not old_row or new_row["id"] != old_row["id"]):
        con.execute("UPDATE categories SET active=1, deleted_at=NULL, category_key=? WHERE id=?", (category_key(new_name), new_row["id"]))
        if old_row:
            con.execute("UPDATE categories SET active=0, deleted_at=? WHERE id=?", (jetzt, old_row["id"]))
    elif old_row:
        con.execute("UPDATE categories SET name=?, category_key=? WHERE id=?", (new_name, category_key(new_name), old_row["id"]))
    con.commit()
    con.close()
    if not new_row and not old_row:
        ensure_category(new_name)
    return changed


GRUPPEN_TRENNZEICHEN = ",;|/\n"


def gruppenname_pruefen(name):
    name = str(name or "").strip()
    if not name:
        return "Bitte einen Namen eingeben."
    if any(zeichen in name for zeichen in GRUPPEN_TRENNZEICHEN):
        return "Bitte im Namen kein Komma, Semikolon, / oder | verwenden – sonst wird die Gruppe beim Speichern in zwei geteilt. Tipp: „&“ statt Komma."
    return ""


def admin_hinweise():
    """Dinge, um die sich der Admin kümmern sollte – erscheinen auf der Übersicht."""
    hinweise = []
    settings = load_settings()
    if not smtp_is_configured():
        hinweise.append(("E-Mail-Versand ist auf dem Server nicht eingerichtet. Bestell-PDFs und der Monats-Export gehen dadurch nicht per Mail raus.", "/admin/settings", "Einstellungen"))
    for key, label in (("order_email_to", "Bestellungen"), ("time_export_email", "Zeiten-Export")):
        address = settings.get(key, "")
        if address and not email_sieht_gueltig_aus(address):
            hinweise.append((f"Die E-Mail-Adresse für {label} sieht unvollständig aus: {address}", "/admin/settings", "Korrigieren"))
    status = str(settings.get("last_auto_time_export_status") or "")
    if "fehl" in status.lower() or "error" in status.lower():
        hinweise.append((f"Der letzte automatische Zeiten-Export hat nicht geklappt: {status}", "/admin/time", "Ansehen"))
    if not push_is_configured():
        hinweise.append(("Push-Nachrichten bei neuen Bestellungen sind auf dem Server noch nicht eingerichtet.", "/admin/settings", "Mehr dazu"))
    rechnungen = len(cockpit_invoice_orders())
    if rechnungen:
        hinweise.append((f"{rechnungen} abgeholte Kundenaufträge warten auf eine Rechnung.", "/admin/orders?ansicht=rechnung", "Ansehen"))
    return hinweise

ERLEDIGT_MARKE = '<span class="vw-marke gruen">✓ erledigt</span>'


WOCHENTAGE_LANG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def tag_titel(iso):
    """'Heute · Freitag, 20.11.' bzw. 'Mittwoch, 18.11.'"""
    try:
        tag = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return iso
    text = f"{WOCHENTAGE_LANG[tag.weekday()]}, {tag.strftime('%d.%m.')}"
    heute = berlin_now().date()
    if tag == heute:
        return "Heute · " + text
    if tag == heute - timedelta(days=1):
        return "Gestern · " + text
    return text


def stunden_kurz(minutes, mit_vorzeichen=False):
    return stunden_text(minutes, mit_vorzeichen).replace(" Std.", "")


class App(BaseHTTPRequestHandler):
    def is_admin(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie"))
        return c.get("admin") and is_valid_admin_token(c["admin"].value)

    def current_buyer_key(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie"))
        morsel = c.get("buyer")
        if not morsel:
            return None
        return parse_buyer_token(morsel.value)

    def base_url(self):
        host = self.headers.get("Host") or f"localhost:{PORT}"
        return f"http://{host}"

    def send_html(self, html_bytes, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html_bytes)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path):
        self.send_response(303)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Location", path)
        self.end_headers()

    def read_form(self):
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_FORM_BYTES:
            raise RequestTooLarge("Die hochgeladenen Daten sind zu groß.")
        body = self.rfile.read(length)

        if content_type.startswith("multipart/form-data"):
            msg = BytesParser(policy=email_policy).parsebytes(
                b"Content-Type: " + content_type.encode("utf-8") + b"\r\n"
                b"MIME-Version: 1.0\r\n\r\n" + body
            )
            data = {}
            for part in msg.iter_parts():
                disposition = part.get("Content-Disposition", "")
                if "form-data" not in disposition:
                    continue
                name = part.get_param("name", header="Content-Disposition")
                filename = part.get_param("filename", header="Content-Disposition")
                payload = part.get_payload(decode=True) or b""
                if not name:
                    continue
                if filename:
                    data[name] = UploadedFile(filename, payload)
                else:
                    charset = part.get_content_charset() or "utf-8"
                    data[name] = payload.decode(charset, errors="replace")
            return data

        raw = body.decode("utf-8", errors="replace")
        return {k: v[0] if v else "" for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def read_json_payload(self, max_bytes=MAX_CART_DRAFT_BYTES):
        length = int(self.headers.get("Content-Length", 0))
        if length > max_bytes:
            raise RequestTooLarge("Die gespeicherten Warenkorbdaten sind zu groß.")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def form_value(self, form, key, default=""):
        item = form.get(key, default)
        if isinstance(item, UploadedFile):
            return default
        return item if item is not None else default

    def do_OPTIONS(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/production-job":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()
            return
        self.send_error(404)

    def show_cart_draft(self):
        buyer_key = self.current_buyer_key()
        location = find_location(buyer_key)
        if not buyer_key or not location_can_order(location):
            return self.send_json({"ok": False, "error": "Nicht angemeldet."}, 401)
        state, updated_at = get_cart_draft(buyer_key)
        return self.send_json({"ok": True, "state": state, "updated_at": updated_at})

    def handle_cart_draft(self):
        buyer_key = self.current_buyer_key()
        location = find_location(buyer_key)
        if not buyer_key or not location_can_order(location):
            return self.send_json({"ok": False, "error": "Nicht angemeldet."}, 401)
        payload = self.read_json_payload()
        if payload.get("action") == "clear":
            delete_cart_draft(buyer_key)
            return self.send_json({"ok": True, "cleared": True})
        state = sanitize_cart_state(payload.get("state") if isinstance(payload.get("state"), dict) else payload)
        save_cart_draft(buyer_key, state)
        return self.send_json({"ok": True})

    def current_push_owner(self):
        if self.is_admin():
            return "Admin"
        return ""

    def show_push_config(self):
        owner = self.current_push_owner()
        if not owner:
            return self.send_json({"ok": False, "error": "Nicht angemeldet."}, 401)
        public_key = push_public_application_key()
        return self.send_json({
            "ok": True,
            "enabled": bool(public_key and push_private_pem()),
            "publicKey": public_key,
        })

    def handle_push_subscribe(self):
        owner = self.current_push_owner()
        if not owner:
            return self.send_json({"ok": False, "error": "Nicht angemeldet."}, 401)
        payload = self.read_json_payload(max_bytes=MAX_PUSH_SUBSCRIPTION_BYTES)
        subscription = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else payload
        if not save_push_subscription(subscription, self.headers.get("User-Agent", ""), owner):
            return self.send_json({"ok": False, "error": "Push-Abo konnte nicht gespeichert werden."}, 400)
        return self.send_json({"ok": True})

    def handle_push_unsubscribe(self):
        owner = self.current_push_owner()
        if not owner:
            return self.send_json({"ok": False, "error": "Nicht angemeldet."}, 401)
        payload = self.read_json_payload(max_bytes=MAX_PUSH_SUBSCRIPTION_BYTES)
        endpoint = str(payload.get("endpoint") or "").strip()
        disable_push_subscription(endpoint)
        return self.send_json({"ok": True})

    def handle_admin_push_test(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        result = send_push_test_notification()
        if not result.get("configured"):
            return self.redirect("/admin/settings?error=" + quote_plus("Push ist auf dem Server noch nicht vollständig eingerichtet."))
        if result.get("sent", 0) > 0:
            return self.redirect("/admin/settings?msg=" + quote_plus(f"Push-Test gesendet an {result.get('sent', 0)} Gerät(e)."))
        return self.redirect("/admin/settings?error=" + quote_plus("Push-Test konnte nicht gesendet werden. Bitte Gerät im Adminbereich neu aktivieren."))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        maybe_run_auto_time_export()
        if path == "/manifest.json":
            return self.serve_manifest()
        if path == "/service-worker.js":
            return self.serve_file(os.path.join(STATIC_DIR, "service-worker.js"), "application/javascript", STATIC_DIR)
        if path.startswith("/static/"):
            return self.serve_file(os.path.join(STATIC_DIR, path[len("/static/"):]), None, STATIC_DIR)
        if path == "/cart-draft":
            return self.show_cart_draft()
        if path == "/push/config":
            return self.show_push_config()
        if path == "/admin/backup":
            if not self.is_admin():
                return self.redirect("/admin/login")
            return self.handle_backup()
        if path.startswith("/uploads/"):
            upload_name = os.path.basename(path.replace("/uploads/", "", 1))
            return self.serve_file(os.path.join(UPLOAD_DIR, upload_name), None, UPLOAD_DIR)
        if path.startswith("/order-images/"):
            if not self.is_admin():
                return self.redirect("/admin/login")
            image_name = os.path.basename(path.replace("/order-images/", "", 1))
            return self.serve_file(os.path.join(ORDER_IMAGE_DIR, image_name), None, ORDER_IMAGE_DIR)
        if path.startswith("/orders-download/"):
            if not (self.is_admin() or self.current_buyer_key()):
                return self.redirect("/login")
            pdf_name = os.path.basename(path.replace("/orders-download/", "", 1))
            pdf_data = read_order_pdf_data(pdf_name)
            if not pdf_data:
                self.send_error(404, "PDF wurde nicht gefunden.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{pdf_name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(pdf_data)
            return
        if path.startswith("/orders/"):
            # PDF darf vom Admin und vom eingeloggten Standort direkt geöffnet/gedruckt werden.
            if not (self.is_admin() or self.current_buyer_key()):
                return self.redirect("/login")
            pdf_name = os.path.basename(path.replace("/orders/", "", 1))
            pdf_data = read_order_pdf_data(pdf_name)
            if not pdf_data:
                self.send_error(404, "PDF wurde nicht gefunden.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{pdf_name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(pdf_data)
            return
        if path == "/":
            return self.show_order_form(query=parse_qs(parsed.query))
        if path == "/cockpit":
            return self.show_location_cockpit(query=parse_qs(parsed.query))
        if path == "/messages/select":
            return self.show_location_message_select(query=parse_qs(parsed.query))
        if path == "/messages":
            return self.show_location_messages(query=parse_qs(parsed.query))
        if path == "/choose":
            return self.show_buyer_choice()
        if path == "/time":
            return self.show_time_form(query=parse_qs(parsed.query))
        if path == "/login":
            return self.show_buyer_login()
        if path == "/logout":
            self.send_response(303)
            self.send_header("Set-Cookie", "buyer=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if path == "/admin/login":
            return self.show_admin_login()
        if path == "/admin/logout":
            self.send_response(303)
            self.send_header("Set-Cookie", "admin=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path == "/admin":
            return self.show_admin_overview(query=parse_qs(parsed.query))
        if path == "/admin/mehr":
            return self.show_admin_more(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung":
            return self.show_bh_uebersicht(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/status":
            return self.show_bh_status(query=parse_qs(parsed.query))
        if path in ("/admin/buchhaltung/quellen", "/admin/buchhaltung/einstellungen"):
            return self.show_bh_einstellungen(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/lieferanten":
            return self.show_bh_lieferanten(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/belege":
            return self.show_bh_belege(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/beleg":
            return self.show_bh_beleg(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/datei":
            return self.show_bh_datei(query=parse_qs(parsed.query))
        if path in ("/admin/buchhaltung/konten", "/admin/buchhaltung/bank"):
            return self.show_bh_bank(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/buchung":
            return self.show_bh_buchung(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/export":
            return self.show_bh_export(query=parse_qs(parsed.query))
        if path == "/admin/buchhaltung/export.zip":
            return self.show_bh_export_zip(query=parse_qs(parsed.query))
        if path == "/beleg":
            return self.show_beleg_foto(query=parse_qs(parsed.query))
        if path == "/kasse":
            return self.show_kasse(query=parse_qs(parsed.query))
        if path == "/admin/tagesabschluss":
            return self.show_tagesabschluss(query=parse_qs(parsed.query))
        if path == "/admin/kasse-foto":
            return self.show_kasse_foto(query=parse_qs(parsed.query))
        if path == "/admin/products":
            return self.show_products(query=parse_qs(parsed.query))
        if path == "/admin/edit-product":
            return self.show_edit_product(query=parse_qs(parsed.query))
        if path == "/admin/orders":
            return self.show_admin_orders(query=parse_qs(parsed.query))
        if path == "/admin/einkauf":
            return self.show_einkauf(query=parse_qs(parsed.query))
        if path == "/admin/cockpit-content":
            return self.redirect("/admin/aufgaben")
        if path == "/admin/aufgaben":
            return self.show_aufgaben(query=parse_qs(parsed.query))
        if path == "/admin/messages":
            return self.show_admin_messages(query=parse_qs(parsed.query))
        if path == "/pdf-viewer":
            return self.show_pdf_viewer(query=parse_qs(parsed.query))
        if path == "/admin/time":
            return self.show_admin_time(query=parse_qs(parsed.query))
        if path == "/admin/employees":
            return self.show_employees(query=parse_qs(parsed.query))
        if path == "/admin/task-report":
            return self.show_task_report(query=parse_qs(parsed.query))
        if path == "/admin/time/export":
            return self.serve_time_export(query=parse_qs(parsed.query))
        if path == "/admin/categories":
            return self.show_categories(query=parse_qs(parsed.query))
        if path == "/admin/import":
            return self.show_import(query=parse_qs(parsed.query))
        if path == "/admin/import-template.csv":
            return self.serve_import_template()
        if path == "/admin/settings":
            return self.show_settings(query=parse_qs(parsed.query))
        if path == "/admin/locations":
            return self.show_locations(query=parse_qs(parsed.query))
        if path == "/admin/visibility":
            ort = (parse_qs(parsed.query).get("location", [""])[0] or (get_locations()[0]["id"] if get_locations() else ""))
            return self.redirect("/admin/location?tab=sortiment&id=" + quote_plus(ort))
        if path == "/admin/location":
            return self.show_location_detail(query=parse_qs(parsed.query))
        self.send_html(page("Nicht gefunden", "<p>Diese Seite gibt es nicht.</p>", buyer_key=self.current_buyer_key()), 404)


    def serve_manifest(self):
        manifest = {
            "name": APP_NAME,
            "short_name": APP_SHORT_NAME,
            "description": "Internes Bestellsystem für Opa Peters.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": BACKGROUND_COLOR,
            "theme_color": THEME_COLOR,
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
            ]
        }
        data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def handle_backup(self):
        """Vollstaendige Sicherung als ZIP: Datenbank, Einstellungen, Bilder.

        Die Datenbank wird ueber die SQLite-Backup-Schnittstelle kopiert.
        Dadurch ist die Kopie auch dann in sich stimmig, wenn waehrend der
        Sicherung jemand bestellt oder Zeiten erfasst.
        """
        import tempfile
        import zipfile

        stempel = datetime.now().strftime("%Y-%m-%d_%H%M")
        ordner = tempfile.mkdtemp(prefix="sicherung-")
        kopie = os.path.join(ordner, "bestellsystem.db")
        try:
            quelle = sqlite3.connect(DB_PATH, timeout=30)
            ziel = sqlite3.connect(kopie)
            with ziel:
                quelle.backup(ziel)
            ziel.close()
            quelle.close()

            puffer = io.BytesIO()
            with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as paket:
                paket.write(kopie, "bestellsystem.db")
                if os.path.exists(SETTINGS_PATH):
                    paket.write(SETTINGS_PATH, "settings.json")
                bh_db = os.path.join(DATA_DIR, "buchhaltung", "buchhaltung.db")
                if os.path.exists(bh_db):
                    bh_kopie = kopie + ".bh"
                    quelle_bh = sqlite3.connect(bh_db)
                    ziel_bh = sqlite3.connect(bh_kopie)
                    quelle_bh.backup(ziel_bh)
                    ziel_bh.close()
                    quelle_bh.close()
                    paket.write(bh_kopie, "buchhaltung/buchhaltung.db")
                    os.remove(bh_kopie)
                bh_ordner = os.path.join(DATA_DIR, "buchhaltung")
                if os.path.isdir(bh_ordner):
                    for ordnerpfad, _unter, dateien in os.walk(bh_ordner):
                        if os.sep + "export" in ordnerpfad:
                            continue
                        for datei in dateien:
                            if datei.endswith(".db") or datei.endswith(".db-journal"):
                                continue
                            voll = os.path.join(ordnerpfad, datei)
                            paket.write(voll, os.path.join("buchhaltung", os.path.relpath(voll, bh_ordner)))
                for quellordner, name in ((UPLOAD_DIR, "uploads"), (ORDER_IMAGE_DIR, "order_images"), (KASSE_FOTO_DIR, "kasse_fotos")):
                    if not os.path.isdir(quellordner):
                        continue
                    for datei in sorted(os.listdir(quellordner)):
                        voll = os.path.join(quellordner, datei)
                        if os.path.isfile(voll):
                            paket.write(voll, name + "/" + datei)
            daten = puffer.getvalue()
        except Exception as fehler:
            self.send_error(500, "Sicherung fehlgeschlagen: %s" % fehler)
            return
        finally:
            try:
                if os.path.exists(kopie):
                    os.remove(kopie)
                os.rmdir(ordner)
            except Exception:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="sicherung_%s.zip"' % stempel)
        self.send_header("Content-Length", str(len(daten)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(daten)

    def serve_file(self, filepath, content_type, root):
        # Nur Dateien ausliefern, die wirklich im erlaubten Ordner liegen.
        # realpath löst ".." und Verknüpfungen auf, commonpath prüft die Lage.
        root_real = os.path.realpath(root)
        file_real = os.path.realpath(filepath)
        try:
            inside = os.path.commonpath([root_real, file_real]) == root_real
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(file_real):
            self.send_error(404)
            return
        filepath = file_real
        if content_type is None:
            ext = os.path.splitext(filepath)[1].lower()
            content_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".heic": "image/heic",
                ".heif": "image/heif",
                ".svg": "image/svg+xml",
                ".js": "application/javascript",
                ".json": "application/json",
                ".css": "text/css",
                ".woff2": "font/woff2",
                ".woff": "font/woff",
            }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    def serve_import_template(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        content = (
            "Produktname;Kategorie;Gebindegröße;Bezugsquelle\n"
            "Kaffeebecher;Verbrauchsmaterial;Karton à 100 Stück;Großhandel A\n"
            "Servietten;Verbrauchsmaterial;Packung à 250 Stück;Großhandel A\n"
        ).encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=produkt_import_vorlage.csv")
        self.end_headers()
        self.wfile.write(content)

    def serve_time_export(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        month = (query.get("month", [current_month()])[0] or current_month()).strip()
        if not re.match(r"^\d{4}-\d{2}$", month):
            month = current_month()
        filename, data, content_type = build_time_export(month)
        log_time_export(month, filename, "", "erstellt", "Manueller Export wurde heruntergeladen.", auto=False)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def show_buyer_choice(self):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location:
            return self.redirect("/login")
        order_button = '<a class="button primary" href="/">Shop</a>' if location_can_order(location) else ""
        time_button = '<a class="button primary" href="/time">Zeiterfassung</a>' if location_can_time(location) else ""
        body = f"""
        <section class="box narrow choice-box">
            <h2>{esc(location['name'])}</h2>
            <p class="muted">Rolle: {esc(role_label(location.get('role')))}</p>
            <div class="choice-actions">
                {order_button}
                {time_button}
            </div>
            {'' if order_button or time_button else '<p class="error">Für diese Rolle ist aktuell kein Bereich freigeschaltet.</p>'}
        </section>
        """
        self.send_html(page("Auswahl", body, buyer_key=buyer_key))

    def show_location_cockpit(self, query=None):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location:
            return self.redirect("/login")
        query = query or {}
        msg = (query.get("msg", [""])[0] or "").strip()
        open_location = (query.get("open_location", [""])[0] or "").strip()
        try:
            week_offset = int((query.get("week", ["0"])[0] or "0").strip())
        except ValueError:
            week_offset = 0
        states = get_cockpit_states(buyer_key)
        state_details = get_cockpit_state_details(buyer_key)
        employee_names = get_time_employee_names(True)
        employee_options = "".join(f'<option value="{esc(name)}">{esc(name)}</option>' for name in employee_names)
        tasks = []
        for task in normalize_cockpit_tasks(location.get("cockpit_tasks", [])):
            if not task.get("active"):
                continue
            info = state_details.get(("task", task["id"]), {})
            if info.get("state") == "done" and not cockpit_state_is_from_today(info):
                continue
            tasks.append(task)
        cockpit_infos = [item for item in normalize_cockpit_infos(location.get("cockpit_infos", [])) if item.get("active")]
        cockpit_orders = []
        if not is_production_location(location):
            for item in normalize_cockpit_orders(location.get("cockpit_orders", [])):
                if not item.get("active"):
                    continue
                state_info = state_details.get(("order", item["id"]), {})
                if state_info.get("state") == "paid_picked" and not cockpit_state_is_from_today(state_info):
                    continue
                cockpit_orders.append(item)
        task_cards = []
        for task in tasks:
            info = state_details.get(("task", task["id"]), {})
            done = info.get("state") == "done"
            completed_by = info.get("completed_by", "")
            image = f'<img class="cockpit-thumb" src="/uploads/{esc(task["image_filename"])}" alt="">' if task.get("image_filename") else ""
            task_cards.append(
                f"""
                <form method="post" action="/cockpit/task" class="cockpit-item cockpit-task-form {'is-done' if done else ''}">
                    <input type="hidden" name="task_id" value="{esc(task['id'])}">
                    <input type="hidden" name="state" value="{'open' if done else 'done'}">
                    <input type="hidden" name="completed_by" value="">
                    <button type="submit" class="cockpit-check" aria-label="Aufgabe abhaken">{'✓' if done else ''}</button>
                    <div>{image}<strong>{esc(task['title'])}</strong>{f'<span>erledigt von {esc(completed_by)}</span>' if done and completed_by else ('<span>erledigt</span>' if done else '<span>offen</span>')}</div>
                </form>
                """
            )
        order_section = ""
        order_cards = []
        def cockpit_order_card(item):
            state = states.get(("order", item["id"]), "open")
            image = f'<img class="cockpit-thumb" src="/uploads/{esc(item["image_filename"])}" alt="">' if item.get("image_filename") else ""
            state_label = {
                "paid_picked": "bezahlt & abgeholt",
                "picked_invoice": "abgeholt & Rechnungsstellung",
            }.get(state, "offen")
            return f"""
            <article class="cockpit-order {'is-done' if state != 'open' else ''}">
                <div>
                    {image}
                    <strong>{esc(item['title'])}</strong>
                    {f'<p class="cockpit-abholung">Abholung: {esc(format_iso_date(item["pickup_date"]))}</p>' if item.get("pickup_date") else ''}
                    {f'<p>{esc(item["note"])}</p>' if item.get("note") else ''}
                    <span class="cockpit-state">{esc(state_label)}</span>
                </div>
                <div class="cockpit-order-actions">
                    <form method="post" action="/cockpit/order-state">
                        <input type="hidden" name="order_id" value="{esc(item['id'])}">
                        <input type="hidden" name="state" value="paid_picked">
                        <button class="button" type="submit">bezahlt & abgeholt</button>
                    </form>
                    <form method="post" action="/cockpit/order-state">
                        <input type="hidden" name="order_id" value="{esc(item['id'])}">
                        <input type="hidden" name="state" value="picked_invoice">
                        <button class="button" type="submit">abgeholt & Rechnungsstellung</button>
                    </form>
                    <form method="post" action="/cockpit/order-state">
                        <input type="hidden" name="order_id" value="{esc(item['id'])}">
                        <input type="hidden" name="state" value="open">
                        <button class="button" type="submit">zurücksetzen</button>
                    </form>
                </div>
            </article>
            """
        if is_schwarzenbek_location(location):
            for item in cockpit_orders:
                order_cards.append(cockpit_order_card(item))
            today_iso = berlin_now().date().strftime("%Y-%m-%d")
            production_states = {}
            for source_location in get_locations():
                if normalize_production_jobs(source_location.get("production_jobs", [])):
                    production_states.update(get_cockpit_states(source_location["id"]))
            today_jobs = [
                job for job in production_jobs_from_all_locations()
                if job.get("active") and job["section"] in ["eisvitrinen", "eistorten"] and job["due_date"] == today_iso
            ]
            for job in sorted(today_jobs, key=lambda item: (item["section"], item["title"].lower())):
                job_state = production_states.get(("production_job", job["id"]), "open")
                done = job_state == "done"
                image = f'<img class="cockpit-thumb" src="/uploads/{esc(job["image_filename"])}" alt="">' if job.get("image_filename") else ""
                order_cards.append(
                    f"""
                    <form method="post" action="/cockpit/production-job" class="cockpit-order production-job {'is-done' if done else ''}">
                        <input type="hidden" name="job_id" value="{esc(job['id'])}">
                        <input type="hidden" name="state" value="{'open' if done else 'done'}">
                        <button type="submit" class="cockpit-check" aria-label="Auftrag abhaken">{'✓' if done else ''}</button>
                        <div>
                            {image}
                            <strong>{esc(PRODUCTION_SECTIONS.get(job['section'], job['section']))}: {esc(job.get('customer_name') or job['title'])}</strong>
                            {f'<p>{esc(job.get("order_text", ""))}</p>' if job.get("order_text") else ''}
                            {f'<p>{esc(job["note"])}</p>' if job.get("note") else ''}
                            <span class="cockpit-state">{'fertiggestellt' if done else ('angefangen' if job_state == 'started' else 'heute')}</span>
                        </div>
                    </form>
                    """
                )
            order_section = f"""
            <section class="box cockpit-panel">
                <div class="section-head"><div><h2>Bestellungen</h2><p class="muted">Hinterlegte Bestellungen und heute fällige Eisvitrinen/Eistorten.</p></div><span class="pill">{len(order_cards)}</span></div>
                <div class="cockpit-list">{''.join(order_cards) if order_cards else '<p class="muted">Für diesen Standort sind aktuell keine Bestellungen hinterlegt.</p>'}</div>
            </section>
            """
        elif cockpit_orders:
            for item in cockpit_orders:
                order_cards.append(cockpit_order_card(item))
            order_section = f"""
            <section class="box cockpit-panel">
                <div class="section-head"><div><h2>Bestellungen</h2><p class="muted">Status für hinterlegte Bestellinformationen setzen.</p></div><span class="pill">{len(cockpit_orders)}</span></div>
                <div class="cockpit-list">{''.join(order_cards)}</div>
            </section>
            """
        quick_actions = []
        if location_can_order(location):
            quick_actions.append('<a class="button primary" href="/">Shop öffnen</a>')
        if location_can_time(location):
            quick_actions.append('<a class="button primary" href="/time">Zeiterfassung öffnen</a>')
        quick_actions.append('<a class="button" href="/beleg">📷 Beleg fotografieren</a>')
        if kasse_einstellungen()["team_zaehlt"] and location["id"] in [l["id"] for l in kasse_standorte()]:
            quick_actions.append('<a class="button" href="/kasse">🧾 Kassenabschluss</a>')
        info_cards = []
        for info_item in cockpit_infos:
            image = f'<img class="cockpit-thumb" src="/uploads/{esc(info_item["image_filename"])}" alt="">' if info_item.get("image_filename") else ""
            info_cards.append(
                f"""
                <article class="cockpit-info-card">
                    {image}
                    <strong>{esc(info_item['title'])}</strong>
                    {f'<p>{esc(info_item["text"])}</p>' if info_item.get("text") else ''}
                </article>
                """
            )
        info_section = f"""
            <section class="box cockpit-panel cockpit-info-panel">
                <div class="section-head"><div><h2>Info</h2><p class="muted">Wichtige Hinweise für diesen Standort.</p></div><span class="pill">{len(cockpit_infos)}</span></div>
                <div class="cockpit-list">{''.join(info_cards) if info_cards else '<p class="muted">Für diesen Standort sind aktuell keine Infos hinterlegt.</p>'}</div>
            </section>
        """
        production_section = ""
        if is_production_location(location):
            weekday_labels = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
            week_days = current_workweek(week_offset)
            week_start = week_days[0]
            week_end = week_days[-1]
            active_jobs = [job for job in normalize_production_jobs(location.get("production_jobs", [])) if job.get("active")]
            jobs_by_section = {key: [] for key in PRODUCTION_SECTIONS}
            for job in active_jobs:
                jobs_by_section.setdefault(job["section"], []).append(job)

            def production_job_card(job):
                job_info = state_details.get(("production_job", job["id"]), {})
                job_state = job_info.get("state", "open")
                done = job_state == "done"
                started_by = job_info.get("completed_by", "")
                image = f'<img class="cockpit-thumb" src="/uploads/{esc(job["image_filename"])}" alt="">' if job.get("image_filename") else ""
                return f"""
                <article class="production-job {'is-done' if done else ('is-started' if job_state == 'started' else '')}">
                    <div class="production-job-content">
                        {image}
                        <strong>{esc(job.get('customer_name') or job['title'])}</strong>
                        {f'<p>{esc(job.get("order_text", ""))}</p>' if job.get("order_text") else ''}
                        {f'<p>{esc(job["note"])}</p>' if job.get("note") else ''}
                        <span>{'fertiggestellt' if done else (f'angefangen von {esc(started_by)}' if job_state == 'started' and started_by else esc(format_iso_date(job['due_date'])))}</span>
                    </div>
                    <div class="production-job-actions">
                        <form method="post" action="/cockpit/production-job" class="production-start-form">
                            <input type="hidden" name="job_id" value="{esc(job['id'])}">
                            <input type="hidden" name="state" value="started">
                            <input type="hidden" name="completed_by" value="">
                            <button class="button production-start-button" type="submit">Angefangen</button>
                        </form>
                        <form method="post" action="/cockpit/production-job">
                            <input type="hidden" name="job_id" value="{esc(job['id'])}">
                            <input type="hidden" name="state" value="done">
                            <button class="primary production-done-button" type="submit">Fertiggestellt</button>
                        </form>
                    </div>
                </article>
                """

            def production_week(section_key):
                week_columns = []
                for day, label in zip(week_days, weekday_labels):
                    iso = day.strftime("%Y-%m-%d")
                    day_jobs = [job for job in jobs_by_section.get(section_key, []) if job["due_date"] == iso]
                    day_count = len(day_jobs)
                    week_columns.append(
                        f"""
                        <details class="production-day">
                            <summary>
                                <span><strong>{label}</strong><small>{day.strftime('%d.%m.')}</small></span>
                                <b>{day_count}</b>
                            </summary>
                            <div class="production-day-list">{''.join(production_job_card(job) for job in day_jobs) if day_jobs else '<p class="muted">Keine Aufträge</p>'}</div>
                        </details>
                        """
                    )
                return ''.join(week_columns)

            production_section = f"""
            <section class="box production-cockpit">
                <div class="section-head">
                    <div><h2>Produktion</h2><p class="muted">Woche vom {week_start.strftime('%d.%m.%Y')} bis {week_end.strftime('%d.%m.%Y')}</p></div>
                    <div class="table-actions">
                        <a class="button week-arrow" href="/cockpit?week={week_offset - 1}" aria-label="Vorwoche">‹</a>
                        <a class="button" href="/cockpit">Aktuelle Woche</a>
                        <a class="button week-arrow" href="/cockpit?week={week_offset + 1}" aria-label="Folgewoche">›</a>
                    </div>
                </div>
                <details class="category-panel production-panel">
                    <summary>Backen <span>Wochenansicht</span></summary>
                    <div class="production-week">{production_week('backen')}</div>
                </details>
                <details class="category-panel production-panel">
                    <summary>Eisvitrinen <span>Wochenansicht</span></summary>
                    <div class="production-week">{production_week('eisvitrinen')}</div>
                </details>
                <details class="category-panel production-panel">
                    <summary>Eistorten <span>Wochenansicht</span></summary>
                    <div class="production-week">{production_week('eistorten')}</div>
                </details>
            </section>
            """
        body = f"""
        {f'<div class="success box narrow">{esc(msg)}</div>' if msg else ''}
        <div class="task-complete-modal" id="taskCompleteModal" hidden>
            <div class="task-complete-card">
                <h2>Wer hat die Aufgabe erledigt?</h2>
                <label>Personal<select id="taskCompletePerson" size="6">{employee_options}</select></label>
                <div class="table-actions">
                    <button class="button" type="button" id="taskCompleteCancel">Abbrechen</button>
                    <button class="primary" type="button" id="taskCompleteConfirm">Speichern</button>
                </div>
            </div>
        </div>
        <div class="task-complete-modal" id="productionStartModal" hidden>
            <div class="task-complete-card">
                <h2>Wer beginnt den Auftrag?</h2>
                <label>Name<input id="productionStartPerson" placeholder="Name eingeben"></label>
                <div class="table-actions">
                    <button class="button" type="button" id="productionStartCancel">Abbrechen</button>
                    <button class="primary" type="button" id="productionStartConfirm">Speichern</button>
                </div>
            </div>
        </div>
        <section class="box cockpit-hero">
            <div>
                <h2>Cockpit {esc(location['name'])}</h2>
                <p class="muted">Übersicht für Tagesaufgaben und Bestellinformationen.</p>
            </div>
            <div class="table-actions cockpit-actions">{''.join(quick_actions)}</div>
        </section>
        <section class="cockpit-grid">
            <section class="box cockpit-panel">
                <div class="section-head"><div><h2>Tagesaufgaben</h2><p class="muted">Zum Erledigen antippen.</p></div><span class="pill">{len(tasks)}</span></div>
                <div class="cockpit-list">{''.join(task_cards) if task_cards else '<p class="muted">Für diesen Standort sind aktuell keine Tagesaufgaben hinterlegt.</p>'}</div>
            </section>
            {info_section}
            {order_section}
        </section>
        {production_section}
        """
        self.send_html(page("Cockpit", body, buyer_key=buyer_key))

    def show_location_message_select(self, query=None):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location:
            return self.redirect("/login")
        employees = get_time_employee_names(True)
        unread_threads = unread_message_recipients(buyer_key)
        unread_names = [
            f"<span>{esc(thread['recipient_name'])} <b>{int(thread.get('unread_location') or 0)}</b></span>"
            for thread in unread_threads
        ]
        employee_buttons = [
            f'<a class="button message-person-button" href="/messages?recipient={quote_plus(name)}#messageChat">{esc(name)}</a>'
            for name in employees
        ]
        body = f"""
        <div class="task-complete-modal message-select-modal">
            <div class="task-complete-card message-select-card">
                <h2>Wer möchte Nachrichten öffnen?</h2>
                <p class="muted">Bitte wähle zuerst den Namen aus. Danach werden nur die Nachrichten dieser Person angezeigt.</p>
                <div class="message-unread-box">
                    <strong>Ungelesene Nachrichten</strong>
                    <div>{''.join(unread_names) if unread_names else '<span>Keine ungelesenen Nachrichten.</span>'}</div>
                </div>
                <div class="message-person-grid">{''.join(employee_buttons) if employee_buttons else '<p class="error">Es sind noch keine Personen angelegt.</p>'}</div>
                <a class="button" href="/cockpit">Zurück zum Cockpit</a>
            </div>
        </div>
        """
        self.send_html(page("Nachrichten auswählen", body, buyer_key=buyer_key))

    def show_location_messages(self, query=None):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location:
            return self.redirect("/login")
        query = query or {}
        msg = (query.get("msg", [""])[0] or "").strip()
        error = (query.get("error", [""])[0] or "").strip()
        selected_recipient = (query.get("recipient", [""])[0] or "").strip()
        employees = get_time_employee_names(True)
        if not selected_recipient or selected_recipient not in employees:
            return self.redirect("/messages/select")
        messages = get_cockpit_messages(buyer_key, selected_recipient, mark_read_for="location")
        chat_rows = []
        for message in messages:
            own = message["sender_role"] == "location"
            chat_rows.append(
                f"""
                <article class="chat-message {'own' if own else 'other'}">
                    <strong>{esc(message['sender_name'])}</strong>
                    <p>{esc(message['message'])}</p>
                    <span>{esc(message['created_at'])}</span>
                </article>
                """
            )
        reply_form = ""
        if selected_recipient:
            reply_form = f"""
            <form method="post" action="/messages/send" class="message-compose" data-preserve-scroll>
                <input type="hidden" name="recipient_name" value="{esc(selected_recipient)}">
                <label>Nachricht<textarea name="message" rows="4" required placeholder="Nachricht schreiben"></textarea></label>
                <button class="primary" type="submit">Senden</button>
            </form>
            """
        body = f"""
        {f'<div class="success box narrow">{esc(msg)}</div>' if msg else ''}
        {f'<div class="error box narrow">{esc(error)}</div>' if error else ''}
        <section class="box">
            <div class="section-head">
                <div><h2>Nachrichten für {esc(selected_recipient)}</h2><p class="muted">Hier werden nur Nachrichten von oder für diese Person angezeigt.</p></div>
                <div class="table-actions">
                    <a class="button" href="/messages/select">Person wechseln</a>
                    <a class="button" href="/cockpit">Zurück zum Cockpit</a>
                </div>
            </div>
        </section>
        <section class="message-layout message-layout-single" id="messageChat">
            <div class="box message-chat">
                <h2>{esc(selected_recipient) if selected_recipient else 'Chat'}</h2>
                <div class="chat-list">{''.join(chat_rows) if chat_rows else '<p class="muted">Noch keine Nachrichten für diese Person.</p>'}</div>
                {reply_form}
            </div>
        </section>
        """
        self.send_html(page("Nachrichten", body, buyer_key=buyer_key))

    def show_time_form(self, error="", query=None):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location or not location_can_time(location):
            return self.redirect("/")
        query = query or {}
        msg = (query.get("msg", [""])[0] or "").strip()
        limit_minutes = location_time_limit_minutes(location)
        end_options = '<option value="">Bitte auswählen</option>' + option_html(recent_end_time_options(limit_minutes))
        employee_names = get_time_employee_names(True)
        employee_options = '<option value="">Bitte auswählen</option>' + option_html(employee_names)
        employee_field = (
            f'<label>Mitarbeiter *<select name="employee_name" required>{employee_options}</select></label>'
            if employee_names
            else '<div class="error full">Es sind noch keine Personen für die Zeiterfassung angelegt. Bitte im Adminbereich unter Personen mindestens eine Person anlegen.</div>'
        )
        submit_disabled = "" if employee_names else " disabled"
        order_link = '<a class="button" href="/">Zum Shop</a>' if location_can_order(location) else ""
        body = f"""
        {f'<div class="error">{esc(error)}</div>' if error else ''}
        {f'<div class="success box narrow">{esc(msg)}</div>' if msg else ''}
        <section class="box">
            <div class="section-head">
                <div>
                    <h2>Zeiterfassung</h2>
                    <p class="muted">Standort: {esc(location['name'])} · Datum: {esc(today_iso())}</p>
                </div>
                {order_link}
            </div>
            <form method="post" action="/time" class="two time-form">
                {employee_field}
                <label>Einsatzort / Filiale *<input name="work_location" required value="{esc(location['name'])}" placeholder="z. B. Schwarzenbek"></label>
                <label>Anfangszeit *<input name="start_time" type="time" required></label>
                <label>Endzeit *<select name="end_time" required>{end_options}</select></label>
                <label class="full">Besondere Vorkommnisse<textarea name="note" rows="4" placeholder="Optional"></textarea></label>
                <button class="primary" type="submit"{submit_disabled}>Zeiterfassung speichern</button>
            </form>
            <p class="muted">Endzeiten zeigen nur das aktuelle 2-Stunden-Fenster und bleiben zusätzlich durch Uhrzeit und maximale Standort-Endzeit begrenzt.</p>
        </section>
        """
        self.send_html(page("Zeiterfassung", body, buyer_key=buyer_key))

    def show_pdf_viewer(self, query=None):
        if not (self.is_admin() or self.current_buyer_key()):
            return self.redirect("/login")
        query = query or {}
        pdf_name = os.path.basename((query.get("file", [""])[0] or "").strip())
        if not pdf_name or not read_order_pdf_data(pdf_name):
            self.send_error(404, "PDF wurde nicht gefunden.")
            return
        close_href = "/admin/orders" if self.is_admin() else "/"
        body = f"""
        <section class="pdf-viewer-shell">
            <div class="pdf-viewer-bar">
                <strong>{esc(pdf_name)}</strong>
                <div class="pdf-viewer-actions">
                    <a class="button" href="{esc(pdf_direct_href(pdf_name))}" target="_blank" rel="noopener">PDF direkt öffnen / drucken</a>
                    <a class="button primary" href="{esc(pdf_download_href(pdf_name))}">PDF herunterladen</a>
                    <a class="pdf-close-button" href="{close_href}" aria-label="PDF schließen">×</a>
                </div>
            </div>
            <iframe class="pdf-frame" src="/orders/{esc(pdf_name)}" title="PDF Vorschau"></iframe>
        </section>
        """
        self.send_html(page("PDF", body, admin=self.is_admin(), buyer_key=self.current_buyer_key()))

    def show_buyer_login(self, error=""):
        options = "".join(f'<option value="{esc(location["id"])}">{esc(location["name"])} · {esc(role_label(location.get("role")))}</option>' for location in get_locations())
        body = f"""
        {f'<div class="error">{esc(error)}</div>' if error else ''}
        <form class="box narrow" method="post" action="/login">
            <h2>{esc(time_greeting())}</h2>
            <p class="muted">Bitte wähle deinen Standort und melde dich an.</p>
            <label>Standort *<select name="buyer_key" required><option value="">Bitte auswählen</option>{options}</select></label>
            <label>Standort-Passwort
                <span class="password-wrap"><input class="numeric-pin password-field" type="password" name="pin" inputmode="numeric" placeholder="PIN eingeben"><button type="button" class="password-toggle" aria-label="Passwort anzeigen">Anzeigen</button></span>
            </label>
            <button class="primary" type="submit">Einloggen</button>
        </form>"""
        self.send_html(page("Besteller Login", body))

    def show_order_form(self, error="", query=None):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location_data = find_location(buyer_key)
        if not location_can_order(location_data):
            return self.redirect("/time" if location_can_time(location_data) else "/choose")

        query = query or {}
        sort = (query.get("sort", ["name_az"])[0] or "name_az").strip()
        category_filter = (query.get("category", [""])[0] or "").strip()
        search_text = (query.get("q", [""])[0] or "").strip()
        # Alle Produkte werden ausgeliefert; Suche und Warengruppen filtern
        # direkt im Browser (static/shop.js), ohne die Seite neu zu laden.
        all_order_products = get_products(True, buyer_key=buyer_key, sort=sort)
        categories = get_categories_for_buyer(buyer_key)
        location_name = location_data["name"] if location_data else buyer_label(buyer_key)
        open_order_product_quantities = get_open_order_product_quantities(location_name)
        contact_name = (location_data or {}).get("contact_name", "").strip()
        role_text = role_label((location_data or {}).get("role"))
        greeting = f"Moin Moin, {esc(contact_name)}" if contact_name else "Moin Moin"

        min_stock_items = get_min_stock_items_for_location(location_data, buyer_key=buyer_key)
        min_stock_html = ""
        if min_stock_items:
            min_stock_cards = "".join(
                f"""
                <article class="min-stock-info-card">
                    <strong>{esc(item['product']['name'])}</strong>
                    <span>{esc(category_text(product_categories(item['product'])))}</span>
                    <small>{esc(item['product']['package_size'])} · {esc(item['product']['source'])}</small>
                    <b>{esc(item['quantity'])} mindestens bestellen</b>
                    <button class="button min-stock-cart-button" type="button" data-product-id="{item['product']['id']}">1 in den Warenkorb</button>
                </article>
                """
                for item in min_stock_items
            )
            min_stock_html = f"""
            <details class="shop-mindest">
                <summary>Mindestbestellmengen <span>{len(min_stock_items)}</span></summary>
                <p class="muted">Orientierung für {esc(location_name)}.</p>
                <div class="min-stock-info-grid">{min_stock_cards}</div>
            </details>
            """

        logo_farben = ["#C44D49", "#D8A69F", "#F0C072", "#AABDCE", "#5A7290"]
        zeichen = {
            "becher & verpackung": "🥤", "variegato & saucen": "🍯", "aromen & pasten": "🍦",
            "getränke": "🧃", "toppings": "🍒", "früchte & füllungen": "🍓", "reinigung": "🧽",
            "milchprodukte & eier": "🥛", "backzutaten": "🌾", "nüsse & marzipan": "🌰",
            "schokolade & kuvertüre": "🍫", "schnecken b2b": "🥐", "tk": "❄️", "eis": "🍨",
            "crumbie b2b": "🧁", "tüten & waffeln": "🥠", "frische": "🍌", "kaffee & co": "☕",
            "backwaren": "🍪", "süßwaren": "🍬", "kakao": "🤎", "backmischungen": "🥣",
            "geräte & zubehör": "🥄", "divers": "📦",
        }
        reihenfolge = list(categories)

        def erste_gruppe(p):
            cats = product_categories(p)
            return cats[0] if cats else "Weitere"

        def farbe(gruppe):
            nummer = reihenfolge.index(gruppe) if gruppe in reihenfolge else len(reihenfolge)
            return logo_farben[nummer % len(logo_farben)]

        def product_card(p):
            cats = product_categories(p)
            gruppe = erste_gruppe(p)
            f = farbe(gruppe)
            bild = f'<span aria-hidden="true">{zeichen.get(gruppe.lower(), "📦")}</span>'
            if p["image_filename"]:
                bild += f'<img src="/uploads/{esc(p["image_filename"])}" alt="" loading="lazy" onerror="this.remove()">'
            open_quantity = open_order_product_quantities.get(str(p["id"]), 0)
            status_badge = f'<div class="ordered-open-badge">Bereits offen bestellt: {open_quantity}</div>' if open_quantity > 0 else ""
            card_class = "product-card ordered-open-product" if open_quantity > 0 else "product-card"
            suchtext = f"{p['name']} {p['package_size']}".lower()
            return f"""
            <article class="{card_class}" data-product-id="{p['id']}" data-product-name="{esc(p['name'])}" data-product-package="{esc(p['package_size'])}" data-kategorien="{esc('|'.join(cats))}" data-suche="{esc(suchtext)}">
                <div class="pk-bild" style="--pk-farbe:{f}">{bild}</div>
                {status_badge}
                <div class="pk-text"><h3>{esc(p['name'])}</h3><p>{esc(p['package_size'])}</p></div>
                <button type="button" class="qty-plus pk-hinzu" data-product-id="{p['id']}">Hinzufügen</button>
                <div class="quantity-control" aria-label="Menge für {esc(p['name'])}">
                    <button type="button" class="qty-minus" data-product-id="{p['id']}" aria-label="weniger">−</button>
                    <input class="qty-display" type="number" data-product-id="{p['id']}" min="0" max="{MAX_ORDER_QUANTITY}" step="1" value="0" inputmode="numeric" aria-label="Menge">
                    <button type="button" class="qty-plus" data-product-id="{p['id']}" aria-label="mehr">+</button>
                </div>
            </article>"""

        hidden_products = {str(p["id"]): p for p in all_order_products}
        for item in min_stock_items:
            hidden_products[str(item["product"]["id"])] = item["product"]
        hidden_qty_inputs = "".join(
            f'<input type="hidden" name="qty_{p["id"]}" id="qty_{p["id"]}" value="0" data-product-id="{p["id"]}" data-product-name="{esc(p["name"])}" data-product-package="{esc(p["package_size"])}">'
            for p in hidden_products.values()
        )

        nach_gruppe = {}
        for p in all_order_products:
            nach_gruppe.setdefault(erste_gruppe(p), []).append(p)
        gruppen_folge = [g for g in reihenfolge if g in nach_gruppe] + [g for g in nach_gruppe if g not in reihenfolge]
        anzahl_je_gruppe = {g: 0 for g in reihenfolge}
        for p in all_order_products:
            for c in product_categories(p):
                anzahl_je_gruppe[c] = anzahl_je_gruppe.get(c, 0) + 1

        chips = f'<button type="button" class="shop-chip" data-gruppe="" aria-pressed="true">Alle <span>{len(all_order_products)}</span></button>' + "".join(
            f'<button type="button" class="shop-chip" data-gruppe="{esc(g)}" aria-pressed="false">{zeichen.get(g.lower(), "")} {esc(g)} <span>{anzahl_je_gruppe.get(g, 0)}</span></button>'
            for g in reihenfolge if anzahl_je_gruppe.get(g, 0)
        )
        abschnitte = "".join(
            f"""<section class="shop-abschnitt" data-gruppe="{esc(g)}">
                <h3 class="shop-abschnitt-titel">{esc(g)} <span>{len(nach_gruppe[g])}</span></h3>
                <div class="shop-gitter">{''.join(product_card(p) for p in nach_gruppe[g])}</div>
            </section>"""
            for g in gruppen_folge
        )
        if not all_order_products:
            abschnitte = '<p class="shop-leer">Für diesen Standort sind noch keine Produkte sichtbar.</p>'

        streifen = "".join(f'<i style="background:{c}"></i>' for c in logo_farben)
        body = f"""
        {f'<div class="error">{esc(error)}</div>' if error else ''}
        <div class="shop" data-start-suche="{esc(search_text)}" data-start-gruppe="{esc(category_filter)}">
            <header class="shop-kopf">
                <p class="shop-augenbraue">{esc(location_name)} · {esc(role_text)}</p>
                <h2 class="shop-titel">Bestellen</h2>
                <div class="shop-streifen">{streifen}</div>
                <p class="shop-gruss">{greeting}</p>
            </header>
            <form method="get" action="/" class="shop-suche" role="search">
                <span aria-hidden="true">🔍</span>
                <input id="shopSuche" name="q" type="search" value="{esc(search_text)}" placeholder="Produkt suchen …" autocomplete="off" enterkeyhint="search" aria-label="Produkt suchen">
            </form>
            <div class="shop-chips" id="shopChips">{chips}</div>
            <form method="post" action="/order" enctype="multipart/form-data">
                {hidden_qty_inputs}
                <input type="hidden" name="location" value="{esc(buyer_key)}">
                {min_stock_html}
                <div class="shop-flaeche">
                    <div class="shop-produkte" id="shopProdukte">
                        <p class="shop-treffer" id="shopTreffer" aria-live="polite"></p>
                        {abschnitte}
                        <p class="shop-leer" id="shopLeer" hidden>Nichts gefunden. Tippe weniger Buchstaben oder wähle eine andere Gruppe.</p>
                    </div>
                    <aside class="shop-seitenkorb" aria-label="Warenkorb">
                        <h2>Warenkorb</h2>
                        <span id="sidebarOrderCount" class="order-count">0 Positionen</span>
                        <div id="cartSidebarItems" class="cart-sidebar-items"></div>
                    </aside>
                </div>
                <div class="shop-korbleiste" id="shopKorbleiste" data-leer="1">
                    <div class="shop-korbleiste-text"><b id="orderCount">0 Positionen</b><span>im Warenkorb</span></div>
                    <button id="reviewOrder" class="shop-korbleiste-knopf" type="button">Prüfen →</button>
                </div>
                <section id="cartReview" class="cart-review" hidden>
                    <div class="cart-card">
                        <div class="cart-card-head">
                            <h2>Warenkorb</h2>
                            <button type="button" id="cartMinimize" class="cart-minimize" aria-label="Warenkorb minimieren" title="Zurück zur Bestellung">−</button>
                        </div>
                        <p class="muted">Standort: {esc(location_name)}. Bitte prüfe deine Bestellung und ergänze die Bestelldaten.</p>
                        <div id="cartItems"></div>
                        <section class="cart-details">
                            <h3>Bestelldaten</h3>
                            <label>Name des Bestellers *<input name="ordered_by" required placeholder="Name eingeben"></label>
                            <label>Bemerkung / Freitext<textarea name="note" rows="3" placeholder="Optionale Hinweise"></textarea></label>
                            <fieldset class="visibility-box">
                                <legend>Bild zur Bestellung</legend>
                                <label>Bild zur Bestellung hinzufügen<input type="file" name="order_image" accept="image/*"></label>
                                <p class="muted">Optional, zum Beispiel für Notizen, Schäden oder Lagerbestand. Maximal 10 MB.</p>
                            </fieldset>
                        </section>
                        <div class="cart-actions">
                            <button type="button" id="cartCancel">Weiter einkaufen</button>
                            <button class="button-yellow" type="button" id="cartSubmit">Bestellung absenden</button>
                        </div>
                    </div>
                </section>
            </form>
        </div>
        <script src="/static/shop.js?v={ASSET_VERSION}" defer></script>"""
        self.send_html(page("Bestellen", body, buyer_key=buyer_key))

    def show_admin_login(self, error=""):
        body = f"""
        {f'<div class="error">{esc(error)}</div>' if error else ''}
        <form class="box narrow" method="post" action="/admin/login">
            <label>Admin-PIN
                <span class="password-wrap"><input class="numeric-pin password-field" type="password" name="pin" inputmode="numeric" required><button type="button" class="password-toggle" aria-label="Passwort anzeigen">Anzeigen</button></span>
            </label>
            <button class="primary" type="submit">Einloggen</button>
        </form>"""
        self.send_html(page("Admin Login", body, buyer_key=self.current_buyer_key()))

    def show_admin(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        search_text = (query.get("q", [""])[0] or "").strip()
        msg = (query.get("msg", [""])[0] or "").strip()
        error = (query.get("error", [""])[0] or "").strip()
        all_products = get_products(False)
        products = filter_products_by_search(all_products, search_text, include_source=True)
        source_options = "".join(f'<option value="{esc(source)}"></option>' for source in get_source_names())
        active_count = sum(1 for p in all_products if p["active"])
        inactive_count = len(all_products) - active_count
        all_orders = get_orders()
        open_orders = [order for order in all_orders if not order["completed_at"]]
        today = today_iso()
        current_entries = get_time_entries(current_month())
        today_time_entries = [entry for entry in current_entries if entry["work_date"] == today]
        locations = get_locations()
        active_locations = [location for location in locations if location_can_order(location) or location_can_time(location)]
        daily_task_count = 0
        open_production_jobs = 0
        min_stock_locations = 0
        for location in locations:
            if location.get("min_stock_enabled"):
                min_stock_locations += 1
            daily_task_count += len([task for task in normalize_cockpit_tasks(location.get("cockpit_tasks", [])) if task.get("active")])
            open_production_jobs += len([job for job in normalize_production_jobs(location.get("production_jobs", [])) if job.get("active")])
        backup_hint = os.path.abspath(DATA_DIR)
        admin_overview = f"""
        <section class="admin-dashboard">
            <a class="admin-dashboard-card dashboard-orders" href="/admin/orders">
                <span>Offene Bestellungen</span>
                <strong>{len(open_orders)}</strong>
                <small>{len(all_orders)} insgesamt</small>
            </a>
            <a class="admin-dashboard-card dashboard-time" href="/admin/time">
                <span>Zeiterfassung heute</span>
                <strong>{len(today_time_entries)}</strong>
                <small>{esc(format_duration(sum(entry['duration_minutes'] for entry in today_time_entries)))}</small>
            </a>
            <a class="admin-dashboard-card dashboard-cockpit" href="/admin/cockpit-content">
                <span>Aufgaben & Produktion</span>
                <strong>{daily_task_count + open_production_jobs}</strong>
                <small>{daily_task_count} Tagesaufgaben · {open_production_jobs} Produktion</small>
            </a>
            <a class="admin-dashboard-card dashboard-system" href="/admin/locations">
                <span>Aktive Standorte</span>
                <strong>{len(active_locations)}</strong>
                <small>{min_stock_locations} mit Mindestbestand</small>
            </a>
        </section>
        <details class="category-panel admin-system-note">
            <summary>System & Datensicherheit <span>Info</span></summary>
            <div class="admin-note-grid">
                <p><strong>Datenordner:</strong><br>{esc(backup_hint)}</p>
                <p><strong>Wichtig:</strong><br>Bestellungen, Arbeitszeiten, Bilder und Einstellungen sollten regelmäßig als Server-Backup gesichert werden.</p>
            </div>
        </details>
        """
        rows = []
        rows_by_category = {}
        for p in products:
            img = f'<img class="thumb" src="/uploads/{esc(p["image_filename"])}">' if p["image_filename"] else "—"
            status = "aktiv" if p["active"] else "deaktiviert"
            visibility = visible_labels(p["visible_to"])
            actions = (
                f"<div class='table-actions'>"
                f"<a class='button' href='/admin/edit-product?id={p['id']}'>Bearbeiten</a>"
                f"<form method='post' action='/admin/delete-product' data-confirm='Produkt wirklich löschen? Diese Aktion entfernt es aus dem Bestellmenü.'>"
                f"<input type='hidden' name='id' value='{p['id']}'><button class='danger'>Löschen</button></form>"
                f"</div>"
            )
            categories_label = category_text(product_categories(p))
            category_keys = " ".join(normalize_text_key(category) for category in product_categories(p))
            row_html = f"<tr class='product-row' data-category-keys='{esc(category_keys)}'><td class='select-cell'><label class='bulk-select-label'><input form='bulkProductForm' class='bulk-product-check' type='checkbox' name='selected_{p['id']}' value='1' aria-label='Produkt auswählen'><span class='bulk-checkmark'>✓</span></label> {img}</td><td>{esc(p['name'])}</td><td>{esc(categories_label)}</td><td>{esc(p['package_size'])}</td><td>{esc(p['source'])}</td><td>{esc(visibility)}</td><td>{status}</td><td>{actions}</td></tr>"
            rows.append(row_html)
            for category in product_categories(p):
                rows_by_category.setdefault(category, []).append(row_html)
        visibility_checks = "".join(
            f'<label class="visibility-option"><input type="checkbox" name="visible_{esc(location["id"])}" value="1" checked><span>{esc(location["name"])}</span></label>'
            for location in get_locations()
        )
        category_checks = category_checkboxes("category", ["Allgemein"])
        bulk_category_checks = category_checkboxes("bulk_category")
        bulk_category_select_buttons = "".join(
            f'<button class="button bulk-category-select" type="button" data-category-key="{esc(normalize_text_key(category))}">{esc(category)}</button>'
            for category in get_category_names(True)
        )
        bulk_visibility_checks = "".join(
            f'<label class="visibility-option"><input type="checkbox" name="bulk_visible_{esc(location["id"])}" value="1"><span>{esc(location["name"])}</span></label>'
            for location in get_locations()
        )
        product_table_header = "<tr><th>Auswahl / Bild</th><th>Name</th><th>Kategorie</th><th>Gebinde</th><th>Bezugsquelle</th><th>Sichtbar für</th><th>Status</th><th></th></tr>"
        def product_table(row_html):
            product_rows = "".join(row_html) if row_html else '<tr><td colspan="8">Keine passenden Produkte gefunden.</td></tr>'
            return f"<div class='table-wrap'><table>{product_table_header}{product_rows}</table></div>"
        admin_category_panels = []
        if rows:
            admin_category_panels.append(f"<details class='category-panel admin-product-panel'><summary>Alle Produkte <span>{len(rows)}</span></summary>{product_table(rows)}</details>")
            for category in sorted(rows_by_category.keys(), key=lambda item: item.lower()):
                category_rows = rows_by_category[category]
                admin_category_panels.append(f"<details class='category-panel admin-product-panel'><summary>{esc(category)} <span>{len(category_rows)}</span></summary>{product_table(category_rows)}</details>")
        body = f"""
        {admin_menu()}
        {f'<div class="success box narrow">{esc(msg)}</div>' if msg else ''}
        {f'<div class="error box narrow">{esc(error)}</div>' if error else ''}
        {admin_overview}
        <section class="stats">
            <div class="stat"><strong>{active_count}</strong><span>aktive Produkte</span></div>
            <div class="stat"><strong>{inactive_count}</strong><span>deaktiviert</span></div>
        </section>
        <details class="category-panel admin-toggle-panel">
            <summary>Produkte anlegen <span>+</span></summary>
            <form method="post" action="/admin/add-product" enctype="multipart/form-data" class="two">
                <datalist id="sourceSuggestions">{source_options}</datalist>
                <p class="muted full">Kategorien legst du separat an und wählst sie hier aus.</p>
                <label>Produktname *<input name="name" required></label>
                <fieldset class="visibility-box full">
                    <legend>Kategorien *</legend>
                    <div class="visibility-grid">{category_checks}</div>
                </fieldset>
                <label>Gebindegröße *<input name="package_size" required placeholder="z. B. Karton à 12 Stück"></label>
                <label>Bezugsquelle *<input name="source" list="sourceSuggestions" required placeholder="Vorhandene wählen oder neue eintragen"></label>
                <label>Produktbild<input type="file" name="image" accept="image/*"></label>
                <fieldset class="visibility-box">
                    <legend>Sichtbar für Standorte *</legend>
                    <div class="visibility-grid">{visibility_checks}</div>
                </fieldset>
                <button class="primary" type="submit">Produkt speichern</button>
                <a class="button" href="/admin/categories">Kategorien verwalten</a>
            </form>
            <details class="category-panel bulk-add-panel">
                <summary>Mehrere Produkte gleichzeitig anlegen <span>+</span></summary>
                <form method="post" action="/admin/add-product" class="bulk-add-products">
                    <input type="hidden" id="bulkAddProductCount" name="bulk_add_count" value="3">
                    <p class="muted">Trage mehrere Produkte untereinander ein. Leere Zeilen werden automatisch übersprungen. Kategorien können mit Komma getrennt werden.</p>
                    <div id="bulkAddProductRows" class="bulk-add-product-rows">
                        <div class="bulk-add-product-row" data-bulk-add-row>
                            <label>Produktname<input name="bulk_name_0" placeholder="Produktname"></label>
                            <label>Kategorien<input name="bulk_category_0" placeholder="z. B. Getränke, Verpackung"></label>
                            <label>Gebinde<input name="bulk_package_size_0" placeholder="z. B. Karton à 12 Stück"></label>
                            <label>Bezugsquelle<input name="bulk_source_0" list="sourceSuggestions" placeholder="Vorhandene wählen oder neu eintragen"></label>
                        </div>
                        <div class="bulk-add-product-row" data-bulk-add-row>
                            <label>Produktname<input name="bulk_name_1" placeholder="Produktname"></label>
                            <label>Kategorien<input name="bulk_category_1" placeholder="z. B. Getränke, Verpackung"></label>
                            <label>Gebinde<input name="bulk_package_size_1" placeholder="z. B. Karton à 12 Stück"></label>
                            <label>Bezugsquelle<input name="bulk_source_1" list="sourceSuggestions" placeholder="Vorhandene wählen oder neu eintragen"></label>
                        </div>
                        <div class="bulk-add-product-row" data-bulk-add-row>
                            <label>Produktname<input name="bulk_name_2" placeholder="Produktname"></label>
                            <label>Kategorien<input name="bulk_category_2" placeholder="z. B. Getränke, Verpackung"></label>
                            <label>Gebinde<input name="bulk_package_size_2" placeholder="z. B. Karton à 12 Stück"></label>
                            <label>Bezugsquelle<input name="bulk_source_2" list="sourceSuggestions" placeholder="Vorhandene wählen oder neu eintragen"></label>
                        </div>
                    </div>
                    <template id="bulkAddProductTemplate">
                        <div class="bulk-add-product-row" data-bulk-add-row>
                            <label>Produktname<input name="bulk_name___INDEX__" placeholder="Produktname"></label>
                            <label>Kategorien<input name="bulk_category___INDEX__" placeholder="z. B. Getränke, Verpackung"></label>
                            <label>Gebinde<input name="bulk_package_size___INDEX__" placeholder="z. B. Karton à 12 Stück"></label>
                            <label>Bezugsquelle<input name="bulk_source___INDEX__" list="sourceSuggestions" placeholder="Vorhandene wählen oder neu eintragen"></label>
                            <button class="button bulk-add-remove" type="button">Zeile entfernen</button>
                        </div>
                    </template>
                    <div class="table-actions">
                        <button class="button" id="addBulkProductRow" type="button">Weitere Zeile hinzufügen</button>
                        <button class="primary" type="submit">Mehrere Produkte speichern</button>
                    </div>
                </form>
            </details>
        </details>
        <section class="box">
            <h2>Produkte</h2>
            <details class="category-panel admin-toggle-panel">
                <summary>Mehrere Produkte bearbeiten <span>+</span></summary>
            <form id="bulkProductForm" method="post" action="/admin/bulk-products" class="bulk-editor" data-confirm="Mehrfachbearbeitung auf ausgewählte Produkte anwenden?">
                <div class="bulk-editor-head">
                    <strong>Mehrere Produkte bearbeiten</strong>
                    <span class="muted">Produkte unten anhaken, gewünschte Felder setzen und anwenden.</span>
                </div>
                <div class="bulk-editor-grid">
                    <fieldset class="visibility-box bulk-visibility bulk-category-tools">
                        <legend>Produkte nach Kategorie auswählen</legend>
                        <label>Kategorie suchen<input id="bulkCategorySearch" type="search" placeholder="Kategorie suchen"></label>
                        <div class="bulk-category-buttons">{bulk_category_select_buttons}</div>
                        <p class="muted">Ein Klick wählt alle sichtbaren Produkte dieser Kategorie aus.</p>
                    </fieldset>
                    <label>Aktion
                        <select name="bulk_action">
                            <option value="update">Ausgewählte bearbeiten</option>
                            <option value="delete">Ausgewählte löschen</option>
                        </select>
                    </label>
                    <fieldset class="visibility-box bulk-visibility">
                        <legend>Kategorien setzen</legend>
                        <div class="visibility-grid">{bulk_category_checks}</div>
                        <p class="muted">Leer lassen = Kategorien behalten.</p>
                    </fieldset>
                    <label>Bezugsquelle
                        <input name="bulk_source" list="sourceSuggestions" placeholder="leer lassen = behalten">
                    </label>
                    <label>Status
                        <select name="bulk_active">
                            <option value="keep">Status behalten</option>
                            <option value="1">aktiv setzen</option>
                            <option value="0">deaktivieren</option>
                        </select>
                    </label>
                    <label>Sichtbarkeit
                        <select name="bulk_visibility_mode">
                            <option value="keep">Sichtbarkeit behalten</option>
                            <option value="all">für alle Standorte sichtbar</option>
                            <option value="custom">unten gewählte Standorte setzen</option>
                        </select>
                    </label>
                    <fieldset class="visibility-box bulk-visibility">
                        <legend>Standorte für Sichtbarkeit</legend>
                        <div class="visibility-grid">{bulk_visibility_checks}</div>
                    </fieldset>
                </div>
                <div class="bulk-editor-actions">
                    <label class="check"><input id="selectAllProducts" type="checkbox"> Alle sichtbaren Produkte auswählen</label>
                    <span class="bulk-selected-count" id="bulkSelectedCount">0 Produkte ausgewählt</span>
                    <button class="primary" type="submit">Auf Auswahl anwenden</button>
                </div>
            </form>
            </details>
            <form method="get" action="/admin" class="filters compact-form product-search">
                <label>Produktsuche<input name="q" value="{esc(search_text)}" placeholder="Name, Kategorie, Gebinde, Bezugsquelle oder Standort suchen"></label>
                <button type="submit">Suchen</button>
                <a class="button" href="/admin">Zurücksetzen</a>
            </form>
            <p class="muted">{len(products)} von {len(all_products)} Produkt(en) angezeigt.</p>
            <section class="category-sections">{''.join(admin_category_panels) if admin_category_panels else '<section class="box"><p>Keine passenden Produkte gefunden.</p></section>'}</section>
        </section>
        """
        self.send_html(page("Adminbereich", body, admin=True, buyer_key=self.current_buyer_key()))

    def show_admin_cockpit_content(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        msg = (query.get("msg", [""])[0] or "").strip()
        error = (query.get("error", [""])[0] or "").strip()
        selected_location = (query.get("location", [""])[0] or "").strip()
        locations = get_locations()

        def image_preview(image_filename):
            image_filename = str(image_filename or "").strip()
            if not image_filename:
                return '<span class="muted">Kein Bild hinterlegt.</span>'
            return f'<a class="cockpit-image-preview" href="/uploads/{esc(image_filename)}" target="_blank" rel="noopener"><img src="/uploads/{esc(image_filename)}" alt="">Bild öffnen</a>'

        def task_rows(prefix, tasks):
            row_items = normalize_cockpit_tasks(tasks) or [{"id": "", "title": "", "template": False, "active": True, "image_filename": ""}]
            rows_html = []
            template_cards = []
            for row_index, item in enumerate(row_items):
                image_filename = item.get("image_filename", "")
                rows_html.append(
                    f"""
                    <div class="cockpit-admin-row" data-cockpit-task-row>
                        <input type="hidden" name="{prefix}_task_id_{row_index}" value="{esc(item.get('id', ''))}">
                        <input type="hidden" name="{prefix}_task_image_current_{row_index}" value="{esc(image_filename)}">
                        <label>Aufgabe<input name="{prefix}_task_title_{row_index}" value="{esc(item.get('title', ''))}" placeholder="z. B. Kühlschrank prüfen"></label>
                        <label>Bild<input type="file" name="{prefix}_task_image_{row_index}" accept="image/*">{image_preview(image_filename)}</label>
                        <label class="check"><input id="{prefix}_task_active_{row_index}" type="checkbox" name="{prefix}_task_active_{row_index}" value="1" {"checked" if item.get('active', True) else ""}> Im Cockpit anzeigen</label>
                        <label class="check"><input type="checkbox" name="{prefix}_task_template_{row_index}" value="1" {"checked" if item.get('template') else ""}> Als Muster speichern</label>
                        <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                    </div>
                    """
                )
                if item.get("template"):
                    template_cards.append(
                        f"""
                        <div class="task-template-card">
                            <strong>{esc(item.get('title', ''))}</strong>
                            <button class="button activate-template-task" type="button" data-target="{prefix}_task_active_{row_index}">Im Cockpit anzeigen</button>
                        </div>
                        """
                    )
            template = f"""
            <template id="{prefix}_task_template">
                <div class="cockpit-admin-row" data-cockpit-task-row>
                    <input type="hidden" name="{prefix}_task_id___INDEX__" value="">
                    <input type="hidden" name="{prefix}_task_image_current___INDEX__" value="">
                    <label>Aufgabe<input name="{prefix}_task_title___INDEX__" placeholder="z. B. Kühlschrank prüfen"></label>
                    <label>Bild<input type="file" name="{prefix}_task_image___INDEX__" accept="image/*"><span class="muted">Optional</span></label>
                    <label class="check"><input id="{prefix}_task_active___INDEX__" type="checkbox" name="{prefix}_task_active___INDEX__" value="1" checked> Im Cockpit anzeigen</label>
                    <label class="check"><input type="checkbox" name="{prefix}_task_template___INDEX__" value="1"> Als Muster speichern</label>
                    <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                </div>
            </template>
            """
            template_collection = f"""
            <div class="task-template-box">
                <h3>Musteraufgaben</h3>
                <p class="muted">Gespeicherte Muster kannst du hier schnell aktivieren.</p>
                <div class="task-template-list">{''.join(template_cards) if template_cards else '<p class="muted">Noch keine Musteraufgaben gespeichert.</p>'}</div>
            </div>
            """
            return "".join(rows_html), len(row_items), template, template_collection

        def order_rows(prefix, orders):
            row_items = normalize_cockpit_orders(orders) or [{"id": "", "title": "", "note": "", "active": True, "image_filename": ""}]
            rows_html = []
            for row_index, item in enumerate(row_items):
                image_filename = item.get("image_filename", "")
                rows_html.append(
                    f"""
                    <div class="cockpit-admin-row cockpit-admin-order-row" data-cockpit-order-row>
                        <input type="hidden" name="{prefix}_order_id_{row_index}" value="{esc(item.get('id', ''))}">
                        <input type="hidden" name="{prefix}_order_image_current_{row_index}" value="{esc(image_filename)}">
                        <label>Bestellung<input name="{prefix}_order_title_{row_index}" value="{esc(item.get('title', ''))}" placeholder="z. B. Tortenbestellung Müller"></label>
                        <label>Hinweis<textarea name="{prefix}_order_note_{row_index}" rows="2" placeholder="Optional">{esc(item.get('note', ''))}</textarea></label>
                        <label>Bild<input type="file" name="{prefix}_order_image_{row_index}" accept="image/*">{image_preview(image_filename)}</label>
                        <label class="check"><input type="checkbox" name="{prefix}_order_active_{row_index}" value="1" {"checked" if item.get('active', True) else ""}> Im Cockpit anzeigen</label>
                        <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                    </div>
                    """
                )
            template = f"""
            <template id="{prefix}_order_template">
                <div class="cockpit-admin-row cockpit-admin-order-row" data-cockpit-order-row>
                    <input type="hidden" name="{prefix}_order_id___INDEX__" value="">
                    <input type="hidden" name="{prefix}_order_image_current___INDEX__" value="">
                    <label>Bestellung<input name="{prefix}_order_title___INDEX__" placeholder="z. B. Tortenbestellung Müller"></label>
                    <label>Hinweis<textarea name="{prefix}_order_note___INDEX__" rows="2" placeholder="Optional"></textarea></label>
                    <label>Bild<input type="file" name="{prefix}_order_image___INDEX__" accept="image/*"><span class="muted">Optional</span></label>
                    <label class="check"><input type="checkbox" name="{prefix}_order_active___INDEX__" value="1" checked> Im Cockpit anzeigen</label>
                    <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                </div>
            </template>
            """
            return "".join(rows_html), len(row_items), template

        def info_rows(prefix, infos):
            row_items = normalize_cockpit_infos(infos) or [{"id": "", "title": "", "text": "", "active": True, "image_filename": ""}]
            rows_html = []
            for row_index, item in enumerate(row_items):
                image_filename = item.get("image_filename", "")
                rows_html.append(
                    f"""
                    <div class="cockpit-admin-row cockpit-admin-info-row" data-cockpit-info-row>
                        <input type="hidden" name="{prefix}_info_id_{row_index}" value="{esc(item.get('id', ''))}">
                        <input type="hidden" name="{prefix}_info_image_current_{row_index}" value="{esc(image_filename)}">
                        <label>Überschrift<input name="{prefix}_info_title_{row_index}" value="{esc(item.get('title', ''))}" placeholder="z. B. Wichtig für heute"></label>
                        <label>Info<textarea name="{prefix}_info_text_{row_index}" rows="3" placeholder="Hinweis für das Cockpit">{esc(item.get('text', ''))}</textarea></label>
                        <label>Bild<input type="file" name="{prefix}_info_image_{row_index}" accept="image/*">{image_preview(image_filename)}</label>
                        <label class="check"><input type="checkbox" name="{prefix}_info_active_{row_index}" value="1" {"checked" if item.get('active', True) else ""}> Im Cockpit anzeigen</label>
                        <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                    </div>
                    """
                )
            template = f"""
            <template id="{prefix}_info_template">
                <div class="cockpit-admin-row cockpit-admin-info-row" data-cockpit-info-row>
                    <input type="hidden" name="{prefix}_info_id___INDEX__" value="">
                    <input type="hidden" name="{prefix}_info_image_current___INDEX__" value="">
                    <label>Überschrift<input name="{prefix}_info_title___INDEX__" placeholder="z. B. Wichtig für heute"></label>
                    <label>Info<textarea name="{prefix}_info_text___INDEX__" rows="3" placeholder="Hinweis für das Cockpit"></textarea></label>
                    <label>Bild<input type="file" name="{prefix}_info_image___INDEX__" accept="image/*"><span class="muted">Optional</span></label>
                    <label class="check"><input type="checkbox" name="{prefix}_info_active___INDEX__" value="1" checked> Im Cockpit anzeigen</label>
                    <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                </div>
            </template>
            """
            return "".join(rows_html), len(row_items), template

        def production_rows(prefix, jobs):
            section_options = lambda selected="": "".join(
                f'<option value="{esc(key)}" {"selected" if key == selected else ""}>{esc(label)}</option>'
                for key, label in PRODUCTION_SECTIONS.items()
            )
            row_items = normalize_production_jobs(jobs) or [{"id": "", "section": "backen", "customer_name": "", "order_text": "", "due_date": "", "note": "", "active": True, "image_filename": ""}]
            rows_html = []
            for row_index, item in enumerate(row_items):
                image_filename = item.get("image_filename", "")
                rows_html.append(
                    f"""
                    <div class="cockpit-admin-row cockpit-admin-production-row" data-cockpit-production-row>
                        <input type="hidden" name="{prefix}_production_id_{row_index}" value="{esc(item.get('id', ''))}">
                        <input type="hidden" name="{prefix}_production_image_current_{row_index}" value="{esc(image_filename)}">
                        <label>Bereich<select name="{prefix}_production_section_{row_index}">{section_options(item.get('section', 'backen'))}</select></label>
                        <label>Name<input name="{prefix}_production_customer_name_{row_index}" value="{esc(item.get('customer_name') or item.get('title', ''))}" placeholder="z. B. Müller"></label>
                        <label>Abholdatum<input type="date" name="{prefix}_production_due_date_{row_index}" value="{esc(item.get('due_date', ''))}"></label>
                        <label>Was wurde bestellt<textarea name="{prefix}_production_order_text_{row_index}" rows="2" placeholder="z. B. Eistorte Erdbeere">{esc(item.get('order_text', ''))}</textarea></label>
                        <label>Hinweis<textarea name="{prefix}_production_note_{row_index}" rows="2" placeholder="Optional">{esc(item.get('note', ''))}</textarea></label>
                        <label>Bild<input type="file" name="{prefix}_production_image_{row_index}" accept="image/*">{image_preview(image_filename)}</label>
                        <label class="check"><input type="checkbox" name="{prefix}_production_active_{row_index}" value="1" {"checked" if item.get('active', True) else ""}> Im Cockpit anzeigen</label>
                        <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                    </div>
                    """
                )
            template = f"""
            <template id="{prefix}_production_template">
                <div class="cockpit-admin-row cockpit-admin-production-row" data-cockpit-production-row>
                    <input type="hidden" name="{prefix}_production_id___INDEX__" value="">
                    <input type="hidden" name="{prefix}_production_image_current___INDEX__" value="">
                    <label>Bereich<select name="{prefix}_production_section___INDEX__">{section_options('backen')}</select></label>
                    <label>Name<input name="{prefix}_production_customer_name___INDEX__" placeholder="z. B. Müller"></label>
                    <label>Abholdatum<input type="date" name="{prefix}_production_due_date___INDEX__"></label>
                    <label>Was wurde bestellt<textarea name="{prefix}_production_order_text___INDEX__" rows="2" placeholder="z. B. Eistorte Erdbeere"></textarea></label>
                    <label>Hinweis<textarea name="{prefix}_production_note___INDEX__" rows="2" placeholder="Optional"></textarea></label>
                    <label>Bild<input type="file" name="{prefix}_production_image___INDEX__" accept="image/*"><span class="muted">Optional</span></label>
                    <label class="check"><input type="checkbox" name="{prefix}_production_active___INDEX__" value="1" checked> Im Cockpit anzeigen</label>
                    <button class="button cockpit-row-remove" type="button">Zeile entfernen</button>
                </div>
            </template>
            """
            return "".join(rows_html), len(row_items), template

        location_panels = []
        for index, location in enumerate(locations):
            prefix = f"cockpit_content_{index}"
            task_html, task_count, task_template, task_templates = task_rows(prefix, location.get("cockpit_tasks", []))
            order_html, order_count, order_template = order_rows(prefix, location.get("cockpit_orders", []))
            info_html, info_count, info_template = info_rows(prefix, location.get("cockpit_infos", []))
            production_html, production_count, production_template = production_rows(prefix, location.get("production_jobs", []))
            order_section = f"""
                    <details class="category-panel cockpit-admin-panel">
                        <summary>Bestellungen <span>{order_count}</span></summary>
                        <input type="hidden" id="{prefix}_order_count" name="{prefix}_order_count" value="{order_count}">
                        <div id="{prefix}_order_rows" class="cockpit-admin-rows" data-template-id="{prefix}_order_template" data-count-id="{prefix}_order_count">{order_html}</div>
                        {order_template}
                        <button class="button add-cockpit-row" type="button" data-target="{prefix}_order_rows">Bestellung hinzufügen</button>
                    </details>
            """
            production_section = ""
            if is_production_location(location):
                production_section = f"""
                    <details class="category-panel cockpit-admin-panel">
                        <summary>Produktionsaufgaben <span>{production_count}</span></summary>
                        <input type="hidden" id="{prefix}_production_count" name="{prefix}_production_count" value="{production_count}">
                        <div id="{prefix}_production_rows" class="cockpit-admin-rows" data-template-id="{prefix}_production_template" data-count-id="{prefix}_production_count">{production_html}</div>
                        {production_template}
                        <button class="button add-cockpit-row" type="button" data-target="{prefix}_production_rows">Produktionsaufgabe hinzufügen</button>
                    </details>
                """
            location_is_open = selected_location == location["id"]
            location_panels.append(
                f"""
                <details id="cockpit-content-{esc(location['id'])}" class="category-panel cockpit-content-location" data-location-id="{esc(location['id'])}" {"open" if location_is_open else ""}>
                    <summary>{esc(location['name'])} <span>{esc(role_label(location.get('role', 'standard')))}</span></summary>
                    <input type="hidden" name="location_id_{index}" value="{esc(location['id'])}">
                    <details class="category-panel cockpit-admin-panel">
                        <summary>Tagesaufgaben <span>{task_count}</span></summary>
                        <input type="hidden" id="{prefix}_task_count" name="{prefix}_task_count" value="{task_count}">
                        <div id="{prefix}_task_rows" class="cockpit-admin-rows" data-template-id="{prefix}_task_template" data-count-id="{prefix}_task_count">{task_html}</div>
                        {task_template}
                        <button class="button add-cockpit-row" type="button" data-target="{prefix}_task_rows">Tagesaufgabe hinzufügen</button>
                        {task_templates}
                    </details>
                    {order_section}
                    <details class="category-panel cockpit-admin-panel">
                        <summary>Info <span>{info_count}</span></summary>
                        <input type="hidden" id="{prefix}_info_count" name="{prefix}_info_count" value="{info_count}">
                        <div id="{prefix}_info_rows" class="cockpit-admin-rows" data-template-id="{prefix}_info_template" data-count-id="{prefix}_info_count">{info_html}</div>
                        {info_template}
                        <button class="button add-cockpit-row" type="button" data-target="{prefix}_info_rows">Info hinzufügen</button>
                    </details>
                    {production_section}
                </details>
                """
            )
        invoice_rows = []
        for row in cockpit_invoice_orders():
            item = row["item"]
            note = f"<br><span class='muted'>{esc(item.get('note', ''))}</span>" if item.get("note") else ""
            invoice_rows.append(
                f"""
                <tr>
                    <td>{esc(row['location']['name'])}</td>
                    <td><strong>{esc(item['title'])}</strong>{note}</td>
                    <td>{esc(row.get('updated_at') or '-')}</td>
                </tr>
                """
            )
        invoice_section = f"""
        <section class="box invoice-panel">
            <div class="section-head">
                <div><h2>Rechnungsstellung</h2><p class="muted">Cockpit-Bestellungen, die als abgeholt mit Rechnungsstellung markiert wurden.</p></div>
                <span class="pill">{len(invoice_rows)}</span>
            </div>
            <div class="table-wrap"><table><tr><th>Standort</th><th>Bestellung</th><th>Markiert am</th></tr>{''.join(invoice_rows) if invoice_rows else '<tr><td colspan="3">Aktuell keine Einträge für Rechnungsstellung.</td></tr>'}</table></div>
        </section>
        """

        body = f"""
        {admin_menu()}
        {f'<div class="success box narrow">{esc(msg)}</div>' if msg else ''}
        {f'<div class="error box narrow">{esc(error)}</div>' if error else ''}
        {invoice_section}
        <section class="box">
            <div class="section-head">
                <div>
                    <h2>Aufträge & Aufgaben</h2>
                    <p class="muted">Lege Tagesaufgaben, Bestellhinweise oder Produktionsaufgaben an und ordne sie dem passenden Standort zu.</p>
                </div>
                <a class="button" href="/admin/locations">Standorte verwalten</a>
            </div>
            <form method="post" action="/admin/cockpit-content" enctype="multipart/form-data">
                <input type="hidden" name="location_count" value="{len(locations)}">
                <input type="hidden" id="cockpitContentOpenLocation" name="open_location" value="{esc(selected_location)}">
                <div class="location-list">{''.join(location_panels) if location_panels else '<p>Noch keine Standorte angelegt.</p>'}</div>
                <button class="primary" type="submit">Aufträge & Aufgaben speichern</button>
            </form>
        </section>
        """
        self.send_html(page("Aufträge & Aufgaben", body, admin=True, buyer_key=self.current_buyer_key()))


    def show_visibility(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        msg = (query.get("msg", [""])[0] or "").strip()
        error = (query.get("error", [""])[0] or "").strip()
        locations = get_locations()
        selected_location_id = (query.get("location", [""])[0] or "").strip()
        if not selected_location_id and locations:
            selected_location_id = locations[0]["id"]
        selected_location = find_location(selected_location_id)
        products = get_products(False, sort="category")
        categories = sorted({category for p in products for category in product_categories(p)}, key=lambda item: item.lower())
        location_options = "".join(
            f'<option value="{esc(location["id"])}" {"selected" if location["id"] == selected_location_id else ""}>{esc(location["name"])}</option>'
            for location in locations
        )
        visible_product_ids = {
            str(product["id"]) for product in products
            if selected_location_id in product_visible_location_keys(product["visible_to"])
        }
        category_checks = []
        for category in categories:
            category_products = [p for p in products if product_has_category(p, category)]
            all_visible = bool(category_products) and all(str(p["id"]) in visible_product_ids for p in category_products)
            category_checks.append(
                f'<label class="visibility-option"><input class="visibility-category-toggle" type="checkbox" name="category_{esc(category)}" value="1" data-category="{esc(category)}" {"checked" if all_visible else ""}><span>{esc(category)} ({len(category_products)})</span></label>'
            )
        product_panels = []
        for category in categories:
            category_products = [p for p in products if product_has_category(p, category)]
            product_checks = "".join(
                f'<label class="visibility-option product-visibility-option"><input class="visibility-product-check" type="checkbox" name="product_{product["id"]}" value="1" data-category="{esc(category)}" {"checked" if str(product["id"]) in visible_product_ids else ""}><span>{esc(product["name"])}<small>{esc(product["package_size"])}</small></span></label>'
                for product in category_products
            )
            product_panels.append(
                f"<details class='category-panel admin-product-panel'><summary>{esc(category)} <span>{len(category_products)}</span></summary><div class='visibility-grid product-visibility-grid'>{product_checks}</div></details>"
            )
        body = f"""
        {admin_menu()}
        {f'<div class="success box narrow">{esc(msg)}</div>' if msg else ''}
        {f'<div class="error box narrow">{esc(error)}</div>' if error else ''}
        <section class="box">
            <div class="section-head">
                <div>
                    <h2>Sichtbarkeit je Standort</h2>
                    <p class="muted">Lege fest, welche Produktkategorien oder Einzelprodukte ein Standort im Bestellbereich sehen darf.</p>
                </div>
                <a class="button" href="/admin/locations">Standorte bearbeiten</a>
            </div>
            <form method="get" action="/admin/visibility" class="filters compact-form">
                <label>Standort auswählen<select name="location">{location_options}</select></label>
                <button class="primary" type="submit">Anzeigen</button>
            </form>
        </section>
        <section class="box">
            <h2>{esc(selected_location["name"] if selected_location else "Kein Standort")}</h2>
            <form method="post" action="/admin/visibility" data-confirm="Sichtbarkeit für diesen Standort speichern?">
                <input type="hidden" name="location_id" value="{esc(selected_location_id)}">
                <fieldset class="visibility-box">
                    <legend>Kategorien schnell auswählen</legend>
                    <div class="visibility-grid">{''.join(category_checks) if category_checks else '<p>Keine Kategorien vorhanden.</p>'}</div>
                    <p class="muted">Eine Kategorie setzt die darunterliegenden Produkte. Einzelprodukte kannst du danach zusätzlich an- oder abwählen.</p>
                </fieldset>
                <div class="category-sections">{''.join(product_panels) if product_panels else '<p>Keine Produkte vorhanden.</p>'}</div>
                <button class="primary visibility-save" type="submit">Sichtbarkeit speichern</button>
            </form>
        </section>
        """
        self.send_html(page("Sichtbarkeit", body, admin=True, buyer_key=self.current_buyer_key()))


    # =================================================================
    #   VERWALTUNG NEU – Übersicht, Mehr, Sortiment
    # =================================================================

    def query_text(self, query, key):
        return ((query or {}).get(key, [""])[0] or "").strip()

    def admin_seite(self, titel, body, status=200):
        self.send_html(page(titel, body, admin=True, buyer_key=self.current_buyer_key()), status)

    def show_admin_overview(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        # Alte Links mit Produktsuche (/admin?q=…) landen auf der Produktseite
        if self.query_text(query, "q"):
            return self.redirect("/admin/products?q=" + quote_plus(self.query_text(query, "q")))
        heute = berlin_now()
        offene = offene_bestellungen()
        bestell_zeilen = []
        for order in offene[:6]:
            anzahl = len(get_order_items(order["id"]))
            notiz = f'<small class="vw-notiz">„{esc(order["note"])}“</small>' if order["note"] else ""
            bestell_zeilen.append(
                f'<a class="vw-zeile" href="/admin/orders#bestellung-{order["id"]}">'
                f'<span><b>{esc(order["location"])}</b><small>{esc(freundliches_datum(order["created_at"]))} · {anzahl} Positionen · {esc(order["ordered_by"] or "")}</small>{notiz}</span>'
                f'<span class="vw-pfeil" aria-hidden="true">›</span></a>'
            )
        mehr_bestellungen = f'<p class="vw-klein">… und {len(offene) - 6} weitere</p>' if len(offene) > 6 else ""
        bestell_karte = f"""
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>Neue Bestellungen</h3><span class="vw-zahl {'rot' if offene else ''}">{len(offene)}</span></div>
            <div class="vw-liste">{''.join(bestell_zeilen) if bestell_zeilen else '<p class="vw-leer">Keine offenen Bestellungen. 👍</p>'}</div>
            {mehr_bestellungen}
            <div class="vw-karte-fuss">
                <a class="button primary" href="/admin/einkauf">Einkaufsliste nach Händler</a>
                <a class="button" href="/admin/orders">Alle offenen</a>
            </div>
        </section>"""

        threads = cockpit_message_threads()
        ungelesen = [t for t in threads if t["unread_admin"]]
        nachricht_zeilen = []
        for thread in (ungelesen or threads)[:5]:
            vorschau = (thread.get("last_message") or "")[:80]
            punkt = f'<i class="vw-punkt">{thread["unread_admin"]}</i>' if thread["unread_admin"] else ""
            link = f'/admin/messages?location={quote_plus(thread["location_id"])}&recipient={quote_plus(thread["recipient_name"])}'
            nachricht_zeilen.append(
                f'<a class="vw-zeile" href="{link}">'
                f'<span><b>{esc(thread["recipient_name"])} · {esc(thread["location_name"])}</b><small>{esc(vorschau)}</small></span>'
                f'<span class="vw-zeit">{esc(freundliches_datum(thread["last_created_at"]))}{punkt}</span></a>'
            )
        nachrichten_karte = f"""
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>{'Ungelesene Nachrichten' if ungelesen else 'Nachrichten'}</h3><span class="vw-zahl {'rot' if ungelesen else ''}">{sum(t['unread_admin'] for t in threads)}</span></div>
            <div class="vw-liste">{''.join(nachricht_zeilen) if nachricht_zeilen else '<p class="vw-leer">Noch keine Nachrichten.</p>'}</div>
            <div class="vw-karte-fuss"><a class="button" href="/admin/messages">Alle Nachrichten</a></div>
        </section>"""

        heute_iso = today_iso()
        heute_eintraege = [e for e in get_time_entries(current_month()) if e["work_date"] == heute_iso]
        dienst_zeilen = "".join(
            f'<div class="vw-zeile"><span><b>{esc(e["employee_name"])}</b><small>{esc(e["work_location"])}</small></span>'
            f'<span class="vw-zeit">{esc(e["start_time"])}–{esc(e["end_time"])}</span></div>'
            for e in sorted(heute_eintraege, key=lambda e: e["start_time"] or "")
        )
        aufgaben_zeilen = []
        for location in get_locations():
            tasks = [t for t in normalize_cockpit_tasks(location.get("cockpit_tasks", [])) if t.get("active")]
            if not tasks:
                continue
            states = get_cockpit_state_details(location["id"])
            erledigt = sum(
                1 for t in tasks
                if states.get(("task", t["id"]), {}).get("state") == "done" and cockpit_state_is_from_today(states.get(("task", t["id"])))
            )
            prozent = round(100 * erledigt / len(tasks))
            aufgaben_zeilen.append(
                f'<a class="vw-zeile" href="/admin/aufgaben?standort={quote_plus(location["id"])}"><span><b>{esc(location["name"])}</b>'
                f'<small>Tagesaufgaben: {erledigt} von {len(tasks)} erledigt</small><span class="vw-balken"><span style="width:{prozent}%"></span></span></span></a>'
            )
        dienst_karte = f"""
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>Heute im Dienst</h3><span class="vw-zahl">{len(heute_eintraege)}</span></div>
            <div class="vw-liste">{dienst_zeilen or '<p class="vw-leer">Heute hat noch niemand Zeiten eingetragen.</p>'}</div>
            {f'<h4 class="vw-zwischentitel">Aufgaben heute</h4><div class="vw-liste">{"".join(aufgaben_zeilen)}</div>' if aufgaben_zeilen else ''}
            <div class="vw-karte-fuss"><a class="button primary" href="/admin/tagesabschluss">🧾 Tagesabschluss</a><a class="button" href="/admin/time">Zeiten</a><a class="button" href="/admin/aufgaben">Aufgaben</a></div>
        </section>"""

        hinweise = admin_hinweise()
        hinweis_html = ""
        if hinweise:
            hinweis_html = '<section class="vw-hinweise"><h3>Zu erledigen</h3>' + "".join(
                f'<div class="vw-hinweis"><span>{esc(text)}</span><a class="button" href="{esc(link)}">{esc(label)}</a></div>'
                for text, link, label in hinweise
            ) + "</section>"
        produkte = get_products(False)
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Übersicht', f'{WOCHENTAGE_KURZ[heute.weekday()]}, {heute.strftime("%d.%m.%Y")} · {len(produkte)} Produkte · {len(get_locations())} Standorte')}
        <div class="vw-raster-3">{bestell_karte}{nachrichten_karte}{dienst_karte}</div>
        {hinweis_html}
        """
        self.admin_seite("Verwaltung", body)

    def show_admin_more(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        punkte = [
            ("/admin/products", "📦", "Sortiment", "Produkte, Warengruppen, Import"),
            ("/admin/time", "👥", "Team", "Zeiten, Personen, Aufgaben"),
            ("/admin/tagesabschluss", "🧾", "Tagesabschluss", "Kasse, Team und Auffälligkeiten des Tages"),
            ("/admin/buchhaltung", "📒", "Buchhaltung", "Belege, Kontoauszüge, Monatsexport"),
            ("/admin/locations", "📍", "Standorte", "Zugänge, Sortiment je Standort, Mindestbestand"),
            ("/admin/settings", "⚙️", "Einstellungen", "E-Mail, PIN, Push, Sicherung"),
            ("/admin/logout", "↩", "Abmelden", "Verwaltung verlassen"),
        ]
        body = admin_kopf("Mehr") + '<div class="vw-liste vw-mehr">' + "".join(
            f'<a class="vw-zeile" href="{href}"><span class="vw-symbol">{icon}</span><span><b>{esc(t)}</b><small>{esc(u)}</small></span><span class="vw-pfeil">›</span></a>'
            for href, icon, t, u in punkte
        ) + "</div>"
        self.admin_seite("Mehr", body)

    # ---------------------------------------------------------- Produkte
    def show_products(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        produkte = get_products(False, sort="category")
        gruppen = visible_category_names()
        haendler = get_source_names()
        standorte = get_locations()
        alle_standorte = set(location_ids())
        zaehler = {}
        zeilen = []
        for p in produkte:
            kategorien = product_categories(p)
            for k in kategorien:
                zaehler[k] = zaehler.get(k, 0) + 1
            sichtbar = set(product_visible_location_keys(p["visible_to"]))
            if sichtbar >= alle_standorte:
                sichtbar_text = "alle Standorte"
            elif not sichtbar:
                sichtbar_text = "kein Standort"
            else:
                sichtbar_text = ", ".join(l["name"] for l in standorte if l["id"] in sichtbar)
            bild = (
                f'<span>{esc((p["name"] or "?")[:1].upper())}</span><img src="/uploads/{esc(p["image_filename"])}" alt="" loading="lazy" onerror="this.remove()">'
                if p["image_filename"] else f'<span>{esc((p["name"] or "?")[:1].upper())}</span>'
            )
            suche = normalize_text_key(" ".join([p["name"] or "", p["package_size"] or "", p["source"] or "", " ".join(kategorien)]))
            status = "" if p["active"] else '<span class="vw-marke grau">ausgeblendet</span>'
            zeilen.append(f"""
            <div class="vw-produkt {'ist-aus' if not p['active'] else ''}" id="p-{p['id']}" data-id="{p['id']}" data-suche="{esc(suche)}"
                 data-gruppen="{esc(' '.join(normalize_text_key(k) for k in kategorien))}" data-haendler="{esc(normalize_text_key(p['source']))}" data-status="{'aktiv' if p['active'] else 'aus'}">
                <label class="vw-auswahl"><input class="vw-produkt-check" type="checkbox" form="bulkProductForm" name="selected_{p['id']}" value="1" aria-label="{esc(p['name'])} auswählen"></label>
                <button type="button" class="vw-produkt-knopf" data-produkt="{p['id']}">
                    <span class="vw-bild">{bild}</span>
                    <span class="vw-produkt-text"><b>{esc(p['name'])}</b><small>{esc(p['package_size'])} · {esc(category_text(kategorien))}</small></span>
                    <span class="vw-produkt-haendler">{esc(p['source'])}</span>
                    <span class="vw-produkt-sicht">{esc(sichtbar_text)}</span>
                    <span class="vw-produkt-status">{status}</span>
                </button>
            </div>""")
        chips = '<button type="button" class="vw-chip" data-gruppe="" aria-pressed="true">Alle <span>' + str(len(produkte)) + "</span></button>" + "".join(
            f'<button type="button" class="vw-chip" data-gruppe="{esc(normalize_text_key(g))}" aria-pressed="false">{esc(g)} <span>{zaehler.get(g, 0)}</span></button>'
            for g in gruppen if zaehler.get(g)
        )
        haendler_optionen = '<option value="">Alle Händler</option>' + "".join(
            f'<option value="{esc(normalize_text_key(h))}">{esc(h)}</option>' for h in haendler
        )
        quelle_liste = "".join(f'<option value="{esc(h)}"></option>' for h in haendler)
        sichtbar_hidden = "".join(f'<input type="hidden" name="visible_{esc(l["id"])}" value="1">' for l in standorte)
        bulk_sicht = "".join(
            f'<label class="vw-schalter"><input type="checkbox" name="bulk_visible_{esc(l["id"])}" value="1"><span>{esc(l["name"])}</span></label>'
            for l in standorte
        )
        aus = sum(1 for p in produkte if not p["active"])
        aktionen = (
            '<button type="button" class="primary" data-dialog="neuesProdukt">+ Neues Produkt</button>'
            '<a class="button" href="/admin/import">Aus Datei importieren</a>'
        )
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Sortiment', f'{len(produkte)} Produkte' + (f' · {aus} ausgeblendet' if aus else ''), aktionen, SORTIMENT_REITER, 'produkte')}
        <datalist id="sourceSuggestions">{quelle_liste}</datalist>
        <datalist id="vwGruppenListe">{''.join(f'<option value="{esc(g)}"></option>' for g in gruppen)}</datalist>
        <section class="vw-werkzeug">
            <label class="vw-suche"><span class="sr-only">Suchen</span><input id="vwProduktSuche" type="search" value="{esc(self.query_text(query, 'q'))}" placeholder="Produkt, Gebinde oder Händler suchen …" autocomplete="off"></label>
            <select id="vwHaendlerFilter" aria-label="Händler">{haendler_optionen}</select>
            <select id="vwStatusFilter" aria-label="Status"><option value="">Alle</option><option value="aktiv">nur sichtbare</option><option value="aus">nur ausgeblendete</option></select>
        </section>
        <div class="vw-chips" id="vwGruppenChips">{chips}</div>
        <div class="vw-produkt-kopf"><label class="vw-auswahl"><input type="checkbox" id="vwAlleWaehlen" aria-label="Alle angezeigten auswählen"></label><span>Produkt</span><span>Händler</span><span>Sichtbar für</span><span></span></div>
        <div class="vw-produktliste" id="vwProduktliste">{''.join(zeilen) or '<p class="vw-leer">Noch keine Produkte angelegt.</p>'}</div>
        <p class="vw-leer" id="vwKeinTreffer" hidden>Kein Produkt passt zur Suche.</p>

        <form id="bulkProductForm" method="post" action="/admin/bulk-products">
            <div class="vw-massenleiste" id="vwMassenleiste" hidden>
                <span id="vwAuswahlZahl">0 ausgewählt</span>
                <button type="button" class="vw-hell" data-dialog="massenBearbeiten">Bearbeiten …</button>
                <button type="submit" class="vw-gefahr-hell" name="bulk_action" value="delete" data-vw-confirm="Ausgewählte Produkte wirklich löschen? Bereits gespeicherte Bestellungen behalten ihre Daten.">Löschen</button>
                <button type="button" class="vw-leise" id="vwAuswahlAufheben">Auswahl aufheben</button>
            </div>
            <dialog class="vw-dialog" id="massenBearbeiten">
                <div class="vw-dialog-kopf"><h3>Ausgewählte Produkte ändern</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
                <p class="vw-klein">Nur was du hier ausfüllst, wird geändert. Leere Felder bleiben wie sie sind.</p>
                <fieldset class="vw-feldgruppe"><legend>Warengruppe setzen</legend><div class="vw-schalter-raster">{category_checkboxes('bulk_category')}</div></fieldset>
                <label>Händler<input name="bulk_source" list="sourceSuggestions" placeholder="leer lassen = behalten"></label>
                <label>Sichtbarkeit im Bestellbereich<select name="bulk_active"><option value="keep">so lassen</option><option value="1">sichtbar</option><option value="0">ausblenden</option></select></label>
                <label>Für welche Standorte<select name="bulk_visibility_mode" id="vwMassenSicht"><option value="keep">so lassen</option><option value="all">für alle Standorte</option><option value="custom">nur für diese:</option></select></label>
                <div class="vw-schalter-raster" id="vwMassenStandorte" hidden>{bulk_sicht}</div>
                <div class="vw-dialog-fuss"><button type="submit" class="primary" name="bulk_action" value="update">Änderungen anwenden</button></div>
            </dialog>
        </form>

        <dialog class="vw-dialog vw-seitenfenster" id="vwProduktPanel" aria-label="Produkt bearbeiten">
            <div id="vwProduktPanelInhalt"><p class="vw-leer">Wird geladen …</p></div>
        </dialog>

        <dialog class="vw-dialog vw-seitenfenster" id="neuesProdukt">
            <div class="vw-dialog-kopf"><h3>Neues Produkt</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/add-product" enctype="multipart/form-data" class="vw-formular">
                {sichtbar_hidden}
                <label>Produktname *<input name="name" required></label>
                <div class="vw-zwei">
                    <label>Gebinde *<input name="package_size" required placeholder="z. B. Karton à 12 Stück"></label>
                    <label>Händler *<input name="source" list="sourceSuggestions" required placeholder="aus der Liste wählen"></label>
                </div>
                <fieldset class="vw-feldgruppe"><legend>Warengruppe *</legend><div class="vw-schalter-raster">{category_checkboxes('category')}</div></fieldset>
                <label>Bild (optional)<input type="file" name="image" accept="image/*"></label>
                <p class="vw-klein">Neue Produkte sind für alle Standorte sichtbar. Einschränken kannst du das beim jeweiligen Standort unter „Sortiment“.</p>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Produkt speichern</button></div>
            </form>
            <details class="vw-aufklapp">
                <summary>Mehrere Produkte auf einmal anlegen</summary>
                <form method="post" action="/admin/add-product" class="vw-formular">
                    <input type="hidden" id="bulkAddProductCount" name="bulk_add_count" value="3">
                    <p class="vw-klein">Eine Zeile pro Produkt. Leere Zeilen werden übersprungen.</p>
                    <div id="bulkAddProductRows" class="vw-mehrfach">
                        {''.join(self._bulk_add_row(i) for i in range(3))}
                    </div>
                    <template id="bulkAddProductTemplate">{self._bulk_add_row('__INDEX__', entfernen=True)}</template>
                    <div class="vw-dialog-fuss"><button class="button" id="addBulkProductRow" type="button">+ Zeile</button><button class="primary" type="submit">Alle speichern</button></div>
                </form>
            </details>
        </dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Sortiment", body)

    def _bulk_add_row(self, index, entfernen=False):
        knopf = '<button class="vw-leise bulk-add-remove" type="button" aria-label="Zeile entfernen">×</button>' if entfernen else ""
        return (
            f'<div class="vw-mehrfach-zeile" data-bulk-add-row>'
            f'<input name="bulk_name_{index}" placeholder="Produktname" aria-label="Produktname">'
            f'<input name="bulk_package_size_{index}" placeholder="Gebinde" aria-label="Gebinde">'
            f'<input name="bulk_source_{index}" list="sourceSuggestions" placeholder="Händler" aria-label="Händler">'
            f'<input name="bulk_category_{index}" placeholder="Warengruppe" list="vwGruppenListe" aria-label="Warengruppe">'
            f'{knopf}</div>'
        )

    def product_edit_form(self, product):
        """Bearbeiten-Formular – im Seitenfenster und als eigene Seite."""
        sichtbar = set(product_visible_location_keys(product["visible_to"]))
        sichtbar_namen = [l["name"] for l in get_locations() if l["id"] in sichtbar]
        sichtbar_hidden = "".join(
            f'<input type="hidden" name="visible_{esc(l["id"])}" value="1">' for l in get_locations() if l["id"] in sichtbar
        )
        if not sichtbar_namen:
            sichtbar_text = "für keinen Standort"
        elif len(sichtbar_namen) == len(get_locations()):
            sichtbar_text = "für alle Standorte"
        else:
            sichtbar_text = "nur für " + ", ".join(sichtbar_namen)
        bild = (
            f'<img class="vw-vorschau" src="/uploads/{esc(product["image_filename"])}" alt="">'
            if product["image_filename"] else '<p class="vw-klein">Noch kein Bild hinterlegt.</p>'
        )
        quelle_liste = "".join(f'<option value="{esc(h)}"></option>' for h in get_source_names())
        return f"""
        <div class="vw-dialog-kopf"><h3>{esc(product['name'])}</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
        <form method="post" action="/admin/update-product" enctype="multipart/form-data" class="vw-formular">
            <datalist id="sourceSuggestionsPanel">{quelle_liste}</datalist>
            <input type="hidden" name="id" value="{esc(product['id'])}">
            {sichtbar_hidden}
            <label>Produktname *<input name="name" required value="{esc(product['name'])}"></label>
            <div class="vw-zwei">
                <label>Gebinde *<input name="package_size" required value="{esc(product['package_size'])}"></label>
                <label>Händler *<input name="source" list="sourceSuggestionsPanel" required value="{esc(product['source'])}"></label>
            </div>
            <fieldset class="vw-feldgruppe"><legend>Warengruppe *</legend><div class="vw-schalter-raster">{category_checkboxes('category', product_categories(product))}</div></fieldset>
            <label>Im Bestellbereich<select name="active"><option value="1" {"selected" if product["active"] else ""}>sichtbar</option><option value="0" {"selected" if not product["active"] else ""}>ausgeblendet</option></select></label>
            <div class="vw-zwei vw-bildfeld"><div>{bild}</div><label>Neues Bild<input type="file" name="image" accept="image/*"></label></div>
            <p class="vw-klein">Sichtbar {esc(sichtbar_text)}. Ändern kannst du das beim jeweiligen <a href="/admin/locations">Standort</a> unter „Sortiment“.</p>
            <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
        </form>
        <form method="post" action="/admin/delete-product" class="vw-gefahrzone" data-vw-confirm="„{esc(product['name'])}“ wirklich löschen? Es verschwindet aus dem Bestellbereich. Alte Bestellungen behalten ihre Daten.">
            <input type="hidden" name="id" value="{esc(product['id'])}">
            <span>Produkt endgültig entfernen</span>
            <button class="danger" type="submit">Löschen</button>
        </form>
        """

    def show_edit_product(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        product = get_product(self.query_text(query, "id"))
        if not product:
            if self.query_text(query, "embed"):
                return self.send_html(b'<p class="vw-leer">Produkt wurde nicht gefunden.</p>', 404)
            return self.redirect("/admin/products?error=" + quote_plus("Produkt wurde nicht gefunden."))
        formular = self.product_edit_form(product)
        if self.query_text(query, "embed"):
            return self.send_html(formular.encode("utf-8"))
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Sortiment', '', '<a class="button" href="/admin/products">‹ Zurück zur Liste</a>', SORTIMENT_REITER, 'produkte')}
        <section class="vw-karte vw-schmal">{formular}</section>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Produkt bearbeiten", body)

    # ---------------------------------------------------------- Warengruppen
    def show_categories(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        produkte = get_products(False)
        zeilen = []
        for name in get_category_names(True):
            anzahl = sum(1 for p in produkte if product_has_category(p, name))
            if name == "Allgemein" and not anzahl:
                continue
            if anzahl:
                loeschen = f'<span class="vw-klein">Löschen geht erst, wenn keine Produkte mehr drin sind.</span>'
            else:
                loeschen = (
                    f'<form method="post" action="/admin/delete-category" data-vw-confirm="Warengruppe „{esc(name)}“ löschen?">'
                    f'<input type="hidden" name="name" value="{esc(name)}"><button class="danger" type="submit">Löschen</button></form>'
                )
            zeilen.append(f"""
            <tr>
                <td><b>{esc(name)}</b></td>
                <td class="zahl"><a href="/admin/products?gruppe={esc(normalize_text_key(name))}">{anzahl}</a></td>
                <td>
                    <form method="post" action="/admin/rename-category" class="vw-inline-form">
                        <input type="hidden" name="old_name" value="{esc(name)}">
                        <input name="new_name" value="{esc(name)}" aria-label="Neuer Name für {esc(name)}" required>
                        <button class="button" type="submit">Umbenennen</button>
                    </form>
                </td>
                <td>{loeschen}</td>
            </tr>""")
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Sortiment', 'Warengruppen ordnen die Produkte im Bestellbereich.', '', SORTIMENT_REITER, 'kategorien')}
        <section class="vw-karte vw-schmal">
            <form method="post" action="/admin/add-category" class="vw-inline-form">
                <label class="vw-wachsen">Neue Warengruppe<input name="name" required placeholder="z. B. Saisonware"></label>
                <button class="primary" type="submit">Anlegen</button>
            </form>
            <p class="vw-klein">Bitte kein Komma im Namen – „&amp;“ geht (z. B. „Milchprodukte &amp; Eier“).</p>
        </section>
        <div class="vw-tabelle-wrap">
            <table class="vw-tabelle">
                <tr><th>Warengruppe</th><th class="zahl">Produkte</th><th>Umbenennen</th><th></th></tr>
                {''.join(zeilen) or '<tr><td colspan="4">Noch keine Warengruppen.</td></tr>'}
            </table>
        </div>
        <p class="vw-klein">Beim Umbenennen ändert sich der Name automatisch bei allen Produkten dieser Gruppe. Gibt es den neuen Namen schon, werden beide Gruppen zusammengelegt.</p>
        """
        self.admin_seite("Warengruppen", body)

    def handle_add_category(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        name = self.form_value(form, "name").strip()
        fehler = gruppenname_pruefen(name)
        if fehler:
            return self.redirect("/admin/categories?error=" + quote_plus(fehler))
        ensure_category(name)
        self.redirect("/admin/categories?msg=" + quote_plus(f"Warengruppe „{name}“ angelegt."))

    def handle_rename_category(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        old_name = self.form_value(form, "old_name").strip()
        new_name = self.form_value(form, "new_name").strip()
        fehler = gruppenname_pruefen(new_name)
        if fehler:
            return self.redirect("/admin/categories?error=" + quote_plus(fehler))
        if not old_name or old_name == new_name:
            return self.redirect("/admin/categories")
        geaendert = rename_category(old_name, new_name)
        self.redirect("/admin/categories?msg=" + quote_plus(f"„{old_name}“ heißt jetzt „{new_name}“. {geaendert} Produkt(e) angepasst."))

    # ---------------------------------------------------------- Import mit Vorschau
    def import_vorschau_pfad(self, token):
        token = re.sub(r"[^a-f0-9]", "", str(token or ""))[:32]
        if not token:
            return ""
        ordner = os.path.join(DATA_DIR, "import_vorschau")
        os.makedirs(ordner, exist_ok=True)
        return os.path.join(ordner, token + ".json")

    def show_import(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        pfad = self.import_vorschau_pfad(self.query_text(query, "vorschau"))
        vorschau_html = ""
        if pfad and os.path.exists(pfad):
            with open(pfad, "r", encoding="utf-8") as f:
                daten = json.load(f)
            zeilen = daten.get("zeilen", [])
            neu = sum(1 for z in zeilen if z["aktion"] == "neu")
            aendern = sum(1 for z in zeilen if z["aktion"] == "aendern")
            gleich = sum(1 for z in zeilen if z["aktion"] == "gleich")
            fehler = sum(1 for z in zeilen if z["aktion"] == "fehler")
            marken = {
                "neu": '<span class="vw-marke gruen">neu</span>',
                "aendern": '<span class="vw-marke honig">wird geändert</span>',
                "gleich": '<span class="vw-marke grau">schon vorhanden</span>',
                "fehler": '<span class="vw-marke rot">fehlt etwas</span>',
            }
            tabelle = "".join(
                f'<tr><td>{marken[z["aktion"]]}</td><td>{esc(z["name"])}</td><td>{esc(z["package_size"])}</td><td>{esc(z["source"])}</td><td>{esc(z["category"])}</td><td class="vw-klein">{esc(z.get("hinweis", ""))}</td></tr>'
                for z in sorted(zeilen, key=lambda z: ["fehler", "neu", "aendern", "gleich"].index(z["aktion"]))
            )
            vorschau_html = f"""
            <section class="vw-karte">
                <div class="vw-karte-kopf"><h3>Vorschau: {esc(daten.get('dateiname', ''))}</h3></div>
                <p><b>{neu}</b> neu · <b>{aendern}</b> werden geändert · <b>{gleich}</b> schon vorhanden (bleiben, wie sie sind) · <b>{fehler}</b> mit fehlenden Angaben (werden übersprungen)</p>
                <p class="vw-klein">Ein Produkt gilt als „schon vorhanden“, wenn Name und Gebinde übereinstimmen. Dann werden nur Warengruppe und Händler aktualisiert – es entsteht kein doppeltes Produkt.</p>
                <div class="vw-karte-fuss">
                    <form method="post" action="/admin/import-confirm"><input type="hidden" name="token" value="{esc(self.query_text(query, 'vorschau'))}"><button class="primary" type="submit" {'disabled' if not (neu or aendern) else ''}>Jetzt übernehmen</button></form>
                    <a class="button" href="/admin/import">Abbrechen</a>
                </div>
                <div class="vw-tabelle-wrap"><table class="vw-tabelle"><tr><th></th><th>Produkt</th><th>Gebinde</th><th>Händler</th><th>Warengruppe</th><th></th></tr>{tabelle}</table></div>
            </section>"""
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Sortiment', 'Viele Produkte auf einmal aus einer Excel- oder CSV-Datei übernehmen.', '', SORTIMENT_REITER, 'import')}
        {vorschau_html}
        <div class="vw-raster-2">
            <section class="vw-karte">
                <h3>Datei hochladen</h3>
                <form method="post" action="/admin/import-products" enctype="multipart/form-data" class="vw-formular">
                    <label>CSV- oder Excel-Datei<input type="file" name="import_file" accept=".csv,.xlsx,.xlsm,text/csv" required></label>
                    <p class="vw-klein">Du siehst zuerst eine Vorschau. Gespeichert wird erst, wenn du „Jetzt übernehmen“ klickst.</p>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">Vorschau anzeigen</button></div>
                </form>
            </section>
            <section class="vw-karte">
                <h3>So muss die Datei aussehen</h3>
                <div class="vw-tabelle-wrap"><table class="vw-tabelle">
                    <tr><th>Spalte</th><th>Pflicht</th><th>Beispiel</th></tr>
                    <tr><td>Produktname</td><td>ja</td><td>Kaffeebecher 200 ml</td></tr>
                    <tr><td>Gebindegröße</td><td>ja</td><td>Karton à 100 Stück</td></tr>
                    <tr><td>Bezugsquelle</td><td>ja</td><td>Hefe van Haag</td></tr>
                    <tr><td>Kategorie</td><td>nein</td><td>Becher &amp; Verpackung</td></tr>
                </table></div>
                <p class="vw-klein">Neue Produkte sind für alle Standorte sichtbar. Bilder lassen sich danach einzeln ergänzen.</p>
                <a class="button" href="/admin/import-template.csv">Vorlage herunterladen</a>
            </section>
        </div>
        """
        self.admin_seite("Import", body)

    def handle_import_products(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        uploaded = form.get("import_file")
        if not isinstance(uploaded, UploadedFile) or not uploaded.content:
            return self.redirect("/admin/import?error=" + quote_plus("Bitte eine CSV- oder Excel-Datei auswählen."))
        if len(uploaded.content) > MAX_IMPORT_BYTES:
            return self.redirect("/admin/import?error=" + quote_plus("Die Importdatei ist zu groß. Bitte maximal 5 MB hochladen."))
        try:
            rows = parse_import_file(uploaded)
        except Exception as exc:
            return self.redirect("/admin/import?error=" + quote_plus(str(exc)))
        vorhandene = {}
        for p in get_products(False):
            vorhandene[(normalize_text_key(p["name"]), normalize_text_key(p["package_size"]))] = p
        zeilen = []
        gesehen = set()
        for row in rows:
            name = (row.get("name") or "").strip()
            package_size = (row.get("package_size") or "").strip()
            source = (row.get("source") or "").strip()
            category = category_text(split_categories(row.get("category") or "Allgemein"))
            zeile = {"name": name, "package_size": package_size, "source": source, "category": category, "aktion": "neu", "id": None, "hinweis": ""}
            schluessel = (normalize_text_key(name), normalize_text_key(package_size))
            if not (name and package_size and source):
                zeile["aktion"] = "fehler"
                fehlt = [t for t, v in (("Name", name), ("Gebinde", package_size), ("Händler", source)) if not v]
                zeile["hinweis"] = "Es fehlt: " + ", ".join(fehlt)
            elif schluessel in gesehen:
                zeile["aktion"] = "fehler"
                zeile["hinweis"] = "Steht doppelt in der Datei"
            elif schluessel in vorhandene:
                alt = vorhandene[schluessel]
                zeile["id"] = alt["id"]
                unterschiede = []
                if normalize_text_key(alt["source"]) != normalize_text_key(source):
                    unterschiede.append(f'Händler: {alt["source"]} → {source}')
                if {normalize_text_key(c) for c in product_categories(alt)} != {normalize_text_key(c) for c in split_categories(category)}:
                    unterschiede.append(f'Gruppe: {category_text(product_categories(alt))} → {category}')
                zeile["aktion"] = "aendern" if unterschiede else "gleich"
                zeile["hinweis"] = "; ".join(unterschiede)
            gesehen.add(schluessel)
            zeilen.append(zeile)
        if not zeilen:
            return self.redirect("/admin/import?error=" + quote_plus("In der Datei wurden keine Zeilen gefunden."))
        token = uuid.uuid4().hex
        with open(self.import_vorschau_pfad(token), "w", encoding="utf-8") as f:
            json.dump({"dateiname": uploaded.filename or "", "zeilen": zeilen}, f, ensure_ascii=False)
        self.redirect("/admin/import?vorschau=" + token)

    def handle_import_confirm(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        pfad = self.import_vorschau_pfad(self.form_value(form, "token"))
        if not pfad or not os.path.exists(pfad):
            return self.redirect("/admin/import?error=" + quote_plus("Die Vorschau ist abgelaufen. Bitte die Datei noch einmal hochladen."))
        with open(pfad, "r", encoding="utf-8") as f:
            zeilen = json.load(f).get("zeilen", [])
        con = db()
        category_map = {}
        for zeile in zeilen:
            if zeile["aktion"] in ("neu", "aendern"):
                for category in split_categories(zeile["category"]):
                    if category not in category_map:
                        category_map[category] = ensure_category_in_connection(con, category, active=1)
        sync_category_metadata(con)
        neu = geaendert = 0
        jetzt = datetime.now().isoformat(timespec="seconds")
        for zeile in zeilen:
            kategorien = category_text([category_map.get(c, c) for c in split_categories(zeile["category"])])
            if zeile["aktion"] == "neu":
                con.execute(
                    "INSERT INTO products (name, package_size, category, source, visible_to, image_filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (zeile["name"], zeile["package_size"], kategorien, zeile["source"], ALL_LOCATIONS_KEY, None, jetzt),
                )
                neu += 1
            elif zeile["aktion"] == "aendern" and zeile.get("id"):
                con.execute("UPDATE products SET category=?, source=? WHERE id=? AND deleted_at IS NULL", (kategorien, zeile["source"], zeile["id"]))
                geaendert += 1
        con.commit()
        con.close()
        os.remove(pfad)
        self.redirect("/admin/products?msg=" + quote_plus(f"Import übernommen: {neu} neu, {geaendert} geändert."))

    # =================================================================
    #   Bestellungen: Offen · Einkaufsliste · Erledigt · Rechnungsstellung
    # =================================================================
    def bestell_karte(self, order, ansicht):
        items = get_order_items(order["id"])
        offen_items = [i for i in items if not i["completed_at"]]
        erledigt = bool(order["completed_at"])
        zeilen = "".join(
            f'<tr class="{"ist-erledigt" if i["completed_at"] else ""}">'
            f'<td><label class="vw-auswahl"><input type="checkbox" name="item_{i["id"]}" value="1" form="positionen-{order["id"]}" aria-label="{esc(i["product_name"])} auswählen"></label></td>'
            f'<td><b>{esc(i["product_name"])}</b><small class="vw-klein-block">{esc(i["category"] or "")}</small></td>'
            f'<td>{esc(i["package_size"])}</td><td>{esc(i["source"])}</td>'
            f'<td class="zahl"><b>{esc(i["quantity"])}</b></td>'
            f'<td>{ERLEDIGT_MARKE if i["completed_at"] else ""}</td></tr>'
            for i in sorted(items, key=lambda i: ((i["source"] or "").lower(), (i["product_name"] or "").lower()))
        )
        bild = f'<a class="button" href="/order-images/{esc(order["order_image_filename"])}" target="_blank" rel="noopener">Foto</a>' if order["order_image_filename"] else ""
        if erledigt:
            status_knopf = f"""<form method="post" action="/admin/orders/status"><input type="hidden" name="order_{order['id']}" value="1"><input type="hidden" name="completed" value="0"><input type="hidden" name="return_location" value="{ansicht}"><button class="button" type="submit">Wieder öffnen</button></form>"""
            loeschen = f"""<form method="post" action="/admin/orders/delete" data-vw-confirm="Bestellung {esc(order['order_number'])} endgültig löschen?"><input type="hidden" name="order_{order['id']}" value="1"><input type="hidden" name="return_location" value="{ansicht}"><button class="danger" type="submit">Löschen</button></form>"""
        else:
            status_knopf = f"""<form method="post" action="/admin/orders/status"><input type="hidden" name="order_{order['id']}" value="1"><input type="hidden" name="completed" value="1"><input type="hidden" name="return_location" value="{ansicht}"><button class="primary" type="submit">✓ Erledigt</button></form>"""
            loeschen = ""
        notiz = f'<p class="vw-bemerkung">„{esc(order["note"])}“</p>' if order["note"] else ""
        unter = f'{esc(freundliches_datum(order["created_at"]))} · {esc(order["order_number"])} · {esc(order["ordered_by"] or "")}'
        if erledigt:
            unter += f' · erledigt {esc(order["completed_at"])}'
        teil = f'{len(offen_items)} von {len(items)} offen' if (not erledigt and 0 < len(offen_items) < len(items)) else (f'{len(items)} Positionen' if len(items) != 1 else '1 Position')
        return f"""
        <div class="vw-bestellung-wrap {'ist-erledigt' if erledigt else ''}" id="order-{order['id']}">
        <label class="vw-auswahl"><input class="vw-bestell-check" type="checkbox" name="order_{order['id']}" value="1" form="sammelForm" aria-label="Bestellung {esc(order['order_number'])} auswählen"></label>
        <details class="vw-bestellung">
            <summary>
                <span class="vw-bestellung-text"><b>{esc(order['location'])}</b><small>{unter}</small>{notiz}</span>
                <span class="vw-marke {'grau' if erledigt else 'blau'}">{teil}</span>
            </summary>
            <div class="vw-bestellung-inhalt">
                <div class="vw-tabelle-wrap"><table class="vw-tabelle"><tr><th></th><th>Produkt</th><th>Gebinde</th><th>Händler</th><th class="zahl">Menge</th><th></th></tr>{zeilen or '<tr><td colspan="6">Keine Positionen gespeichert.</td></tr>'}</table></div>
                <form id="positionen-{order['id']}" method="post" action="/admin/order-items/status" class="vw-knopfreihe vw-klein-knopfe">
                    <input type="hidden" name="return_order" value="{order['id']}">
                    <input type="hidden" name="return_location" value="{ansicht}">
                    <span class="vw-klein">Markierte Positionen:</span>
                    <button class="button" type="submit" name="completed" value="1">abhaken</button>
                    <button class="button" type="submit" name="completed" value="0">wieder öffnen</button>
                    <button class="danger" type="submit" formaction="/admin/order-items/delete" data-vw-confirm="Markierte Positionen aus der Bestellung löschen?">löschen</button>
                </form>
                <div class="vw-knopfreihe">
                    <a class="button" href="{esc(pdf_viewer_href(order['pdf_filename']))}">PDF</a>{bild}{status_knopf}{loeschen}
                </div>
            </div>
        </details>
        </div>"""

    def pdf_meldung(self, pdf):
        if not pdf:
            return ""
        return (
            f'<div class="vw-meldung ok">PDF ist fertig. '
            f'<a class="button primary" href="{esc(pdf_download_href(pdf))}">Herunterladen</a>'
            f'<a class="button" href="{esc(pdf_direct_href(pdf))}" target="_blank" rel="noopener">Öffnen / Drucken</a></div>'
        )

    def show_admin_orders(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        ansicht = self.query_text(query, "ansicht") or "offen"
        if ansicht not in ("offen", "erledigt", "rechnung"):
            ansicht = "offen"
        standort = self.query_text(query, "standort")
        alle = get_orders()
        offene = [o for o in alle if not o["completed_at"]]
        kopf = admin_kopf(
            "Bestellungen",
            f"{len(offene)} offen · {len(alle)} insgesamt",
            "",
            [(k, (f"{l} ({len(offene)})" if k == "offen" else l), h) for k, l, h in BESTELL_REITER],
            ansicht,
        )
        meldungen = admin_meldungen(self.query_text(query, "msg"), self.query_text(query, "error")) + self.pdf_meldung(self.query_text(query, "pdf"))

        if ansicht == "rechnung":
            zeilen = "".join(
                f'<tr><td>{esc(r["location"]["name"])}</td><td><b>{esc(r["item"]["title"])}</b>'
                f'{"<small class=vw-klein-block>" + esc(r["item"].get("note", "")) + "</small>" if r["item"].get("note") else ""}</td>'
                f'<td>{esc(r["item"].get("pickup_date") and format_iso_date(r["item"]["pickup_date"]) or "–")}</td><td>{esc(r.get("updated_at") or "–")}</td></tr>'
                for r in cockpit_invoice_orders()
            )
            body = f"""{meldungen}{kopf}
            <p class="vw-klein">Kundenaufträge, die ein Standort als „abgeholt – Rechnung schreiben“ markiert hat.</p>
            <div class="vw-tabelle-wrap"><table class="vw-tabelle"><tr><th>Standort</th><th>Auftrag</th><th>Abholung</th><th>Markiert am</th></tr>{zeilen or '<tr><td colspan="4">Gerade wartet nichts auf eine Rechnung.</td></tr>'}</table></div>
            <p class="vw-klein">Kundenaufträge legst du unter <a href="/admin/aufgaben?art=auftraege">Team › Aufgaben &amp; Aufträge</a> an.</p>"""
            return self.admin_seite("Rechnungsstellung", body)

        if ansicht == "erledigt":
            monat = self.query_text(query, "monat") or current_month()
            liste = [o for o in alle if o["completed_at"]]
            monate = sorted({parse_order_created_at(o["created_at"]).strftime("%Y-%m") for o in liste if parse_order_created_at(o["created_at"]) != datetime.min}, reverse=True)
            if monat != "alle":
                liste = [o for o in liste if parse_order_created_at(o["created_at"]).strftime("%Y-%m") == monat]
        else:
            liste = offene
            monate = []
            monat = ""
        standorte_in_liste = sorted({o["location"] or "" for o in liste}, key=str.lower)
        if standort:
            liste = [o for o in liste if (o["location"] or "") == standort]
        chips = "".join(
            f'<a class="vw-chip" aria-pressed="{"true" if s == standort else "false"}" href="/admin/orders?ansicht={ansicht}&monat={quote_plus(monat)}&standort={quote_plus(s)}">{esc(s)}</a>'
            for s in standorte_in_liste
        )
        chips = (f'<a class="vw-chip" aria-pressed="{"false" if standort else "true"}" href="/admin/orders?ansicht={ansicht}&monat={quote_plus(monat)}">Alle Standorte</a>' + chips) if len(standorte_in_liste) > 1 else ""
        monat_auswahl = ""
        if ansicht == "erledigt":
            optionen = '<option value="alle">Alle Monate</option>' + "".join(
                f'<option value="{m}" {"selected" if m == monat else ""}>{esc(datetime.strptime(m, "%Y-%m").strftime("%m/%Y"))}</option>' for m in monate
            )
            if monat not in monate and monat != "alle":
                optionen = f'<option value="{esc(monat)}" selected>{esc(monat)}</option>' + optionen
            monat_auswahl = f'<form method="get" action="/admin/orders" class="vw-werkzeug"><input type="hidden" name="ansicht" value="erledigt"><select name="monat" data-auto-submit aria-label="Monat">{optionen}</select></form>'

        karten = []
        letzter_tag = None
        for order in liste:
            tag = order_day_key(order)
            if ansicht == "erledigt" and tag != letzter_tag:
                karten.append(f'<h3 class="vw-tag">{esc(freundliches_datum(order["created_at"], mit_zeit=False))}</h3>')
                letzter_tag = tag
            karten.append(self.bestell_karte(order, ansicht))
        if ansicht == "offen":
            leer = '<p class="vw-leer">Keine offenen Bestellungen. 👍</p>'
            sammel_knoepfe = (
                '<button type="submit" class="vw-hell">Als ein PDF</button>'
                '<button type="submit" class="vw-hell" formaction="/admin/orders/status" name="completed" value="1">✓ Erledigt</button>'
            )
            alle_pdf = ""
            if offene:
                alle_pdf = '<form method="post" action="/admin/orders/combined-pdf" class="vw-knopfreihe">' + "".join(
                    f'<input type="hidden" name="order_{o["id"]}" value="1">' for o in liste
                ) + f'<input type="hidden" name="return_location" value="offen"><button class="button" type="submit">{"Alle angezeigten" if standort else "Alle offenen"} als ein PDF</button><a class="button primary" href="/admin/einkauf">Einkaufsliste nach Händler</a></form>'
        else:
            leer = '<p class="vw-leer">In diesem Zeitraum gibt es keine erledigten Bestellungen.</p>'
            sammel_knoepfe = (
                '<button type="submit" class="vw-hell">Als ein PDF</button>'
                '<button type="submit" class="vw-hell" formaction="/admin/orders/status" name="completed" value="0">Wieder öffnen</button>'
                '<button type="submit" class="vw-gefahr-hell" formaction="/admin/orders/delete" data-vw-confirm="Ausgewählte Bestellungen endgültig löschen?">Löschen</button>'
            )
            alle_pdf = ""
        body = f"""{meldungen}{kopf}
        {monat_auswahl}
        {f'<div class="vw-chips">{chips}</div>' if chips else ''}
        {alle_pdf}
        <div class="vw-bestellliste">{''.join(karten) or leer}</div>
        <form id="sammelForm" method="post" action="/admin/orders/combined-pdf">
            <input type="hidden" name="return_location" value="{ansicht}">
            <div class="vw-massenleiste" id="vwSammelLeiste" hidden>
                <span id="vwSammelZahl">0 ausgewählt</span>{sammel_knoepfe}
            </div>
        </form>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Bestellungen", body)

    # ---------------------------------------------------------- Einkaufsliste
    def show_einkauf(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        haendler = einkaufsliste_offen()
        offene = offene_bestellungen()
        kopf = admin_kopf(
            "Bestellungen",
            "Alles, was offen ist – zusammengezählt und nach Händler sortiert.",
            "",
            [(k, (f"{l} ({len(offene)})" if k == "offen" else l), h) for k, l, h in BESTELL_REITER],
            "einkauf",
        )
        bloecke = []
        for quelle in sorted(haendler, key=lambda q: (-len(haendler[q]), q.lower())):
            eintraege = sorted(haendler[quelle].values(), key=lambda e: (e["category"].lower(), e["product_name"].lower()))
            gebinde = sum(e["total"] for e in eintraege)
            zeilen = "".join(
                f'<tr><td><b>{esc(e["product_name"])}</b><small class="vw-klein-block">{esc(e["category"])}</small></td><td>{esc(e["package_size"])}</td>'
                f'<td class="zahl vw-gross">{e["total"]}</td>'
                f'<td>{"".join(f"<span class=vw-standort-menge>{esc(loc)} <b>{qty}</b></span>" for loc, qty in sorted(e["by_location"].items()))}</td>'
                f'<td><form method="post" action="/admin/einkauf/erledigt">{"".join(f"<input type=hidden name=item_{i} value=1>" for i in e["item_ids"])}'
                f'<button class="button vw-haken" type="submit" title="Abhaken" aria-label="{esc(e["product_name"])} abhaken">✓</button></form></td></tr>'
                for e in eintraege
            )
            schluessel = normalize_text_key(quelle) or "ohne"
            bloecke.append(f"""
            <details class="vw-haendler" open data-merken="einkauf-{esc(schluessel)}">
                <summary><span><b>{esc(quelle)}</b><small>{len(eintraege)} Produkte · {gebinde} Gebinde</small></span></summary>
                <div class="vw-tabelle-wrap"><table class="vw-tabelle"><tr><th>Produkt</th><th>Gebinde</th><th class="zahl">Menge</th><th>Für</th><th></th></tr>{zeilen}</table></div>
                <div class="vw-knopfreihe">
                    <form method="post" action="/admin/einkauf/pdf"><input type="hidden" name="source" value="{esc(quelle)}"><button class="button" type="submit">PDF für {esc(quelle)}</button></form>
                    <form method="post" action="/admin/einkauf/erledigt" data-vw-confirm="Alle {len(eintraege)} Produkte von {esc(quelle)} als bestellt abhaken?"><input type="hidden" name="source" value="{esc(quelle)}"><button class="primary" type="submit">✓ Alles von {esc(quelle)} abhaken</button></form>
                </div>
            </details>""")
        body = f"""{admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}{self.pdf_meldung(self.query_text(query, 'pdf'))}{kopf}
        <p class="vw-klein">Abgehakte Produkte verschwinden aus der Liste. Sind alle Produkte einer Bestellung abgehakt, wandert sie automatisch zu „Erledigt“.</p>
        {''.join(bloecke) or '<p class="vw-leer">Nichts offen – alles bestellt. 👍</p>'}
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Einkaufsliste", body)

    def handle_einkauf_pdf(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        quelle = self.form_value(form, "source").strip()
        eintraege = list(einkaufsliste_offen().get(quelle, {}).values())
        if not eintraege:
            return self.redirect("/admin/einkauf?error=" + quote_plus("Für diesen Händler ist nichts offen."))
        try:
            pdf = create_supplier_pdf(quelle, eintraege)
        except Exception as exc:
            print(f"Einkaufs-PDF fehlgeschlagen: {exc}")
            return self.redirect("/admin/einkauf?error=" + quote_plus("Das PDF konnte nicht erstellt werden."))
        self.redirect("/admin/einkauf?pdf=" + quote_plus(pdf))

    def handle_einkauf_erledigt(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        quelle = self.form_value(form, "source").strip()
        if quelle:
            item_ids = [i for e in einkaufsliste_offen().get(quelle, {}).values() for i in e["item_ids"]]
        else:
            item_ids = [key[5:] for key, value in form.items() if key.startswith("item_") and value]
        if not item_ids:
            return self.redirect("/admin/einkauf?error=" + quote_plus("Nichts zum Abhaken gefunden."))
        set_order_items_completed(item_ids, completed=True)
        fertig = bestellungen_automatisch_erledigen()
        text = f"{len(item_ids)} Position(en) abgehakt."
        if fertig:
            text += f" {fertig} Bestellung(en) sind damit komplett erledigt."
        self.redirect("/admin/einkauf?msg=" + quote_plus(text))

    # =================================================================
    #   Team: Zeiten · Personen · Aufgaben & Aufträge · Auswertung
    # =================================================================
    def team_kopf(self, aktiv, unter="", aktionen=""):
        return admin_kopf("Team", unter, aktionen, TEAM_REITER, aktiv)

    def zeit_eintrag_karte(self, entry, month, mitarbeiter_namen, zeige_person=False):
        namen = list(mitarbeiter_namen)
        if entry["employee_name"] and entry["employee_name"] not in namen:
            namen.append(entry["employee_name"])
        status = "nachträglich geändert" if entry["edited"] else "so eingetragen"
        notiz = f'<small class="vw-notiz">„{esc(entry["note"])}“</small>' if entry["note"] else ""
        person = f'<b>{esc(entry["employee_name"])}</b>' if zeige_person else f'<span class="vw-schicht-datum">{esc(freundliches_datum(entry["work_date"], mit_zeit=False))}</span>'
        return f"""
        <details class="vw-schicht">
            <summary>
                <span class="vw-schicht-erst">{person}</span>
                <span class="vw-schicht-zeit">{esc(entry['start_time'])}–{esc(entry['end_time'])}</span>
                <span class="vw-schicht-ort">{esc(entry['work_location'])}{notiz}</span>
                <b class="vw-schicht-dauer">{esc(stunden_text(entry['duration_minutes']))}</b>
                <span class="vw-schicht-stift" aria-hidden="true">✎</span>
            </summary>
            <form method="post" action="/admin/time/update" class="vw-formular vw-schicht-form">
                <input type="hidden" name="id" value="{entry['id']}">
                <div class="vw-drei">
                    <label>Datum<input name="work_date" type="date" value="{esc(entry['work_date'])}" required></label>
                    <label>Anfang<input name="start_time" type="time" step="900" value="{esc(entry['start_time'])}" required></label>
                    <label>Ende<select name="end_time">{option_html(time_options(), entry['end_time'])}</select></label>
                </div>
                <div class="vw-zwei">
                    <label>Person<select name="employee_name" required>{option_html(namen, entry['employee_name'])}</select></label>
                    <label>Einsatzort<input name="work_location" value="{esc(entry['work_location'])}" required></label>
                </div>
                <label>Vorkommnisse<textarea name="note" rows="2">{esc(entry['note'])}</textarea></label>
                <div class="vw-knopfreihe"><span class="vw-klein">{esc(status)}</span><span class="vw-wachsen"></span>
                    <button class="primary" type="submit">Speichern</button>
                    <button class="danger" type="submit" form="loeschen-{entry['id']}">Löschen</button>
                </div>
            </form>
            <form id="loeschen-{entry['id']}" method="post" action="/admin/time/delete" data-vw-confirm="Diese Schicht wirklich löschen?"><input type="hidden" name="id" value="{entry['id']}"><input type="hidden" name="month" value="{esc(month)}"></form>
        </details>"""

    def show_admin_time(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        month = self.query_text(query, "month") or current_month()
        if not re.match(r"^\d{4}-\d{2}$", month):
            month = current_month()
        ansicht = self.query_text(query, "ansicht") or "uebersicht"
        person_filter = self.query_text(query, "person")
        ort_filter = self.query_text(query, "work_location")
        entries = get_time_entries(month, "", ort_filter)
        by_employee, by_location, matrix = summarize_time_entries(entries)
        settings_by_key = employee_settings_map()
        aktive_namen = get_time_employee_names(active_only=True)
        alle_namen = get_time_employee_names(active_only=False)
        locations = get_locations()
        jahr, mon = int(month[:4]), int(month[5:])
        vorher = f"{jahr - (mon == 1)}-{12 if mon == 1 else mon - 1:02d}"
        nachher = f"{jahr + (mon == 12)}-{1 if mon == 12 else mon + 1:02d}"
        monatsnamen = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
        monat_text = f"{monatsnamen[mon - 1]} {jahr}"
        def link(**extra):
            werte = {"month": month, "ansicht": ansicht, "work_location": ort_filter, "person": person_filter}
            werte.update(extra)
            return "/admin/time?" + urlencode({k: v for k, v in werte.items() if v})

        # ---- Kennzahlen
        summe_ist = sum(by_employee.values())
        summe_lohn = 0
        zeilen = []
        namen = sorted(set(by_employee) | set(aktive_namen), key=str.lower)
        max_ist = max([by_employee.get(n, 0) for n in namen] + [1])
        for name in namen:
            ist = by_employee.get(name, 0)
            einstellungen = settings_by_key.get(normalize_text_key(name), {})
            soll = int(round(parse_decimal(einstellungen.get("monthly_hours", 0)) * 60))
            _h, _u, lohn = employee_payroll_values(name, ist, settings_by_key)
            summe_lohn += lohn
            anzahl = sum(1 for e in entries if (e["employee_name"] or "Ohne Namen") == name)
            if soll:
                diff = ist - soll
                diff_html = f'<span class="{"plus" if diff > 0 else ("minus" if diff < 0 else "")}">{esc(stunden_kurz(diff, mit_vorzeichen=True))}</span>'
                prozent = min(100, round(100 * ist / soll)) if soll else 0
                balken = f'<span class="vw-balken" title="{prozent} % vom Soll"><span style="width:{prozent}%"></span></span>'
            else:
                diff_html = '<span class="vw-klein">–</span>'
                balken = f'<span class="vw-balken grau"><span style="width:{round(100 * ist / max_ist)}%"></span></span>'
            weg = "" if name in aktive_namen or name not in alle_namen else ' <span class="vw-marke grau">ausgeschieden</span>'
            zeilen.append(f"""
            <tr class="vw-klickzeile" data-href="{esc(link(ansicht='schichten', person=name))}">
                <td><a href="{esc(link(ansicht='schichten', person=name))}"><b>{esc(name)}</b></a>{weg}</td>
                <td class="zahl">{esc(stunden_kurz(soll)) if soll else '–'}</td>
                <td class="zahl"><b>{esc(stunden_kurz(ist))}</b>{balken}</td>
                <td class="zahl">{diff_html}</td>
                <td class="zahl">{esc(format_money(lohn)) if lohn else '–'}</td>
            </tr>""")
        personen_tabelle = f"""
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>Stunden je Person</h3><span class="vw-klein">Name anklicken = Schichten ansehen</span></div>
            <div class="vw-tabelle-wrap vw-ohne-rand"><table class="vw-tabelle">
                <tr><th>Person</th><th class="zahl">Soll (Std.)</th><th class="zahl">Ist (Std.)</th><th class="zahl">+/−</th><th class="zahl">Lohn</th></tr>
                {''.join(zeilen) or '<tr><td colspan="5">Noch keine Personen angelegt.</td></tr>'}
                <tr class="summe"><td>Gesamt</td><td></td><td class="zahl">{esc(stunden_kurz(summe_ist))}</td><td></td><td class="zahl">{esc(format_money(summe_lohn))}</td></tr>
            </table></div>
        </section>"""

        max_ort = max(list(by_location.values()) + [1])
        ort_karten = []
        for ort, minuten in sorted(by_location.items(), key=lambda i: -i[1]):
            leute = sorted(((p, orte.get(ort, 0)) for p, orte in matrix.items() if orte.get(ort)), key=lambda i: -i[1])
            leute_html = "".join(f'<li><span>{esc(p)}</span><b>{esc(stunden_text(m))}</b></li>' for p, m in leute)
            anteil = round(100 * minuten / summe_ist) if summe_ist else 0
            ort_karten.append(f"""
            <div class="vw-ort">
                <div class="vw-ort-kopf"><b>{esc(ort)}</b><span>{esc(stunden_text(minuten))} · {anteil} %</span></div>
                <span class="vw-balken dick"><span style="width:{round(100 * minuten / max_ort)}%"></span></span>
                <ul class="vw-ort-leute">{leute_html}</ul>
            </div>""")
        orte_karte = f"""
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>Stunden je Standort</h3></div>
            {''.join(ort_karten) or '<p class="vw-leer">In diesem Monat noch keine Schichten.</p>'}
        </section>"""

        heute_iso = today_iso()
        heute = [e for e in get_time_entries(current_month()) if e["work_date"] == heute_iso]
        kennzahlen = f"""
        <div class="vw-kennzahlen">
            <div><small>Stunden {esc(monatsnamen[mon - 1])}</small><b>{esc(stunden_text(summe_ist))}</b></div>
            <div><small>Lohnsumme</small><b>{esc(format_money(summe_lohn))}</b></div>
            <div><small>Schichten</small><b>{len(entries)}</b></div>
            <div><small>Heute im Dienst</small><b>{len(heute)}</b><span class="vw-klein">{esc(", ".join(e["employee_name"] for e in heute)) or "noch niemand"}</span></div>
        </div>"""

        # ---- Ansicht "Schichten": nach Tagen
        if ansicht == "schichten":
            liste = [e for e in entries if not person_filter or e["employee_name"] == person_filter]
            tage = {}
            for e in liste:
                tage.setdefault(e["work_date"], []).append(e)
            bloecke = []
            for tag in sorted(tage, reverse=True):
                schichten = sorted(tage[tag], key=lambda e: (e["start_time"] or "", e["employee_name"] or ""))
                summe = sum(e["duration_minutes"] or 0 for e in schichten)
                bloecke.append(
                    f'<div class="vw-tag-kopf"><h3 class="vw-tag">{esc(tag_titel(tag))}</h3><span class="vw-klein">{esc(stunden_text(summe))}</span></div>'
                    f'<div class="vw-schichtliste">{"".join(self.zeit_eintrag_karte(e, month, alle_namen, zeige_person=True) for e in schichten)}</div>'
                )
            personen_chips = f'<a class="vw-chip" aria-pressed="{"false" if person_filter else "true"}" href="{esc(link(person=""))}">Alle</a>' + "".join(
                f'<a class="vw-chip" aria-pressed="{"true" if n == person_filter else "false"}" href="{esc(link(person=n))}">{esc(n)}</a>'
                for n in sorted(by_employee, key=str.lower)
            )
            person_info = ""
            if person_filter:
                ist = by_employee.get(person_filter, 0)
                soll = int(round(parse_decimal(settings_by_key.get(normalize_text_key(person_filter), {}).get("monthly_hours", 0)) * 60))
                diff = f" · {stunden_text(ist - soll, mit_vorzeichen=True)}" if soll else ""
                person_info = f'<p class="vw-person-info"><b>{esc(person_filter)}</b>: {esc(stunden_text(ist))}' + (f" von {esc(stunden_text(soll))} Soll{esc(diff)}" if soll else "") + f' · {len(liste)} Schichten</p>'
            inhalt = f"""
            <div class="vw-chips">{personen_chips}</div>
            {person_info}
            {''.join(bloecke) or '<p class="vw-leer">Keine Schichten in diesem Monat.</p>'}"""
        else:
            inhalt = f'{kennzahlen}<div class="vw-raster-zeiten">{personen_tabelle}{orte_karte}</div>'

        settings = load_settings()
        log = latest_time_export_log()
        export_status = settings.get("last_auto_time_export_status") or (f"{log['created_at']}: {log['status']}" if log else "noch nicht gelaufen")
        ort_optionen = '<option value="">Alle Standorte</option>' + "".join(
            f'<option value="{esc(l["name"])}" {"selected" if l["name"] == ort_filter else ""}>{esc(l["name"])}</option>' for l in locations
        )
        zeit_optionen = '<option value="">–</option>' + option_html(time_options())
        unterreiter = (
            f'<div class="vw-umschalter"><a href="{esc(link(ansicht="uebersicht", person=""))}" {AKTIV_ATTR if ansicht != "schichten" else ""}>Übersicht</a>'
            f'<a href="{esc(link(ansicht="schichten"))}" {AKTIV_ATTR if ansicht == "schichten" else ""}>Alle Schichten</a></div>'
        )
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.team_kopf('zeiten', '', '<button type="button" class="primary" data-dialog="schichtNachtragen">+ Schicht nachtragen</button>' + f'<a class="button" href="/admin/time/export?month={esc(month)}">Monat exportieren</a>')}
        <div class="vw-werkzeug vw-monatsleiste">
            <div class="vw-monat">
                <a class="button vw-rund" href="{esc(link(month=vorher))}" aria-label="Monat davor">‹</a>
                <b>{esc(monat_text)}</b>
                <a class="button vw-rund" href="{esc(link(month=nachher))}" aria-label="Monat danach">›</a>
            </div>
            {unterreiter}
            <form method="get" action="/admin/time"><input type="hidden" name="month" value="{esc(month)}"><input type="hidden" name="ansicht" value="{esc(ansicht)}"><input type="hidden" name="person" value="{esc(person_filter)}">
                <select name="work_location" data-auto-submit aria-label="Standort">{ort_optionen}</select></form>
        </div>
        {inhalt}
        <p class="vw-klein vw-fusszeile">Monats-Export geht automatisch an <b>{esc(settings.get('time_export_email') or 'niemanden')}</b> ({esc(export_status)}) · Späteste Uhrzeit zum Eintragen: beim jeweiligen <a href="/admin/locations">Standort</a></p>

        <dialog class="vw-dialog" id="schichtNachtragen">
            <div class="vw-dialog-kopf"><h3>Schicht nachtragen</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/time/create" class="vw-formular">
                <input type="hidden" name="shift_count" value="1">
                <div class="vw-zwei">
                    <label>Person<select name="employee_name" required><option value="">bitte wählen</option>{option_html(aktive_namen, person_filter)}</select></label>
                    <label>Standort<select name="location_id" required>{''.join(f'<option value="{esc(l["id"])}">{esc(l["name"])}</option>' for l in locations)}</select></label>
                </div>
                <div class="vw-drei">
                    <label>Datum<input type="date" name="shift_date_0" value="{esc(heute_iso)}" required></label>
                    <label>Anfang<select name="shift_start_0" required>{zeit_optionen}</select></label>
                    <label>Ende<select name="shift_end_0" required>{zeit_optionen}</select></label>
                </div>
                <label>Hinweis (optional)<input name="shift_note_0"></label>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Eintragen</button></div>
            </form>
        </dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Zeiten", body)

    def show_employees(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        personen = get_time_employees(active_only=False)
        zeilen = "".join(f"""
            <tr class="{'' if p.get('active', True) else 'ist-aus'}">
                <td><input form="personenForm" name="employee_name_{i}" value="{esc(p['name'])}" aria-label="Name"></td>
                <td><input form="personenForm" name="employee_hourly_wage_{i}" value="{esc(format_decimal(p.get('hourly_wage', 0)))}" inputmode="decimal" aria-label="Stundenlohn"></td>
                <td><input form="personenForm" name="employee_monthly_hours_{i}" value="{esc(format_decimal(p.get('monthly_hours', 0)))}" inputmode="decimal" aria-label="Soll-Stunden im Monat"></td>
                <td><select form="personenForm" name="employee_status_{i}" aria-label="Status">
                    <option value="aktiv" {'selected' if p.get('active', True) else ''}>arbeitet bei uns</option>
                    <option value="ausgeschieden" {'' if p.get('active', True) else 'selected'}>ausgeschieden</option>
                    <option value="loeschen">aus der Liste löschen</option>
                </select></td>
            </tr>""" for i, p in enumerate(personen))
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.team_kopf('personen', 'Wer hier steht, kann im Standort Zeiten eintragen und Aufgaben abhaken.')}
        <form id="personenForm" method="post" action="/admin/employees">
            <input type="hidden" name="employee_count" value="{len(personen)}">
            <div class="vw-tabelle-wrap"><table class="vw-tabelle">
                <tr><th>Name</th><th>Stundenlohn (€)</th><th>Soll-Stunden / Monat</th><th>Status</th></tr>
                {zeilen or '<tr><td colspan="4">Noch keine Personen.</td></tr>'}
                <tr class="summe"><td><input name="new_employee_name" placeholder="+ Neue Person: Name"></td><td><input name="new_employee_hourly_wage" inputmode="decimal" placeholder="z. B. 14,00"></td><td><input name="new_employee_monthly_hours" inputmode="decimal" placeholder="z. B. 80"></td><td></td></tr>
            </table></div>
            <div class="vw-knopfreihe"><button class="primary" type="submit" data-vw-confirm="Personenliste speichern?">Speichern</button></div>
        </form>
        <p class="vw-klein">„Ausgeschieden“ blendet die Person bei der Zeiterfassung aus – ihre Stunden bleiben erhalten. Leer gelassene Soll-Stunden bedeuten: kein Plus/Minus.</p>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Personen", body)

    def show_task_report(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        month = self.query_text(query, "month") or current_month()
        if not re.match(r"^\d{4}-\d{2}$", month):
            month = current_month()
        con = db()
        rows = con.execute(
            "SELECT location_id, item_id, completed_by, updated_at FROM cockpit_item_states WHERE item_type='task' AND state='done' AND completed_by IS NOT NULL AND TRIM(completed_by) != '' ORDER BY updated_at DESC"
        ).fetchall()
        con.close()
        lookup = {}
        for location in get_locations():
            for task in normalize_cockpit_tasks(location.get("cockpit_tasks", [])):
                lookup[(location["id"], task["id"])] = (location["name"], task["title"])
        zeilen, zaehler = [], {}
        for row in rows:
            try:
                wann = datetime.strptime(row["updated_at"], "%d.%m.%Y %H:%M")
            except ValueError:
                continue
            if wann.strftime("%Y-%m") != month:
                continue
            ort, titel = lookup.get((row["location_id"], row["item_id"]), (row["location_id"], "Aufgabe"))
            zaehler[row["completed_by"]] = zaehler.get(row["completed_by"], 0) + 1
            zeilen.append(f'<tr><td>{esc(row["updated_at"])}</td><td>{esc(row["completed_by"])}</td><td>{esc(ort)}</td><td>{esc(titel)}</td></tr>')
        summe = "".join(f'<tr><td>{esc(p)}</td><td class="zahl">{n}</td></tr>' for p, n in sorted(zaehler.items(), key=lambda i: (-i[1], i[0].lower())))
        body = f"""
        {self.team_kopf('auswertung', 'Wer hat welche Tagesaufgaben abgehakt?')}
        <form method="get" action="/admin/task-report" class="vw-werkzeug"><input type="month" name="month" value="{esc(month)}" data-auto-submit aria-label="Monat"></form>
        <div class="vw-raster-2">
            <div class="vw-tabelle-wrap"><table class="vw-tabelle"><tr><th>Person</th><th class="zahl">Erledigt</th></tr>{summe or '<tr><td colspan="2">In diesem Monat nichts erledigt.</td></tr>'}</table></div>
            <div class="vw-tabelle-wrap"><table class="vw-tabelle"><tr><th>Wann</th><th>Person</th><th>Standort</th><th>Aufgabe</th></tr>{''.join(zeilen) or '<tr><td colspan="4">Keine Einträge.</td></tr>'}</table></div>
        </div>
        <p class="vw-klein">Hinweis: Es zählt jeweils das letzte Abhaken einer Aufgabe – wird sie am nächsten Tag wieder abgehakt, ersetzt das den alten Eintrag.</p>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Auswertung", body)

    # ---------------------------------------------------------- Aufgaben & Aufträge
    AUFGABEN_ARTEN = [
        ("tagesaufgaben", "Tagesaufgaben", "cockpit_tasks"),
        ("auftraege", "Kundenaufträge", "cockpit_orders"),
        ("infos", "Infos", "cockpit_infos"),
        ("produktion", "Produktion", "production_jobs"),
    ]

    def show_aufgaben(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        locations = get_locations()
        produktion = [l for l in locations if is_production_location(l)]
        arten = [a for a in self.AUFGABEN_ARTEN if a[0] != "produktion" or produktion]
        art = self.query_text(query, "art") or "tagesaufgaben"
        if art not in [a[0] for a in arten]:
            art = "tagesaufgaben"
        schluessel = dict((a[0], a[2]) for a in arten)[art]
        standort = self.query_text(query, "standort")
        ziel_orte = produktion if art == "produktion" else locations
        zaehler = {a[0]: sum(len(self.aufgaben_liste(l, a[2])) for l in (produktion if a[0] == "produktion" else locations)) for a in arten}
        art_umschalter = '<div class="vw-umschalter">' + "".join(
            f'<a href="/admin/aufgaben?art={a[0]}"{AKTIV_ATTR if a[0] == art else ""}>{esc(a[1])} <small>{zaehler[a[0]]}</small></a>' for a in arten
        ) + "</div>"

        # Formular "Neu anlegen" im Fenster
        if len(ziel_orte) > 1:
            ort_haken = '<fieldset class="vw-feldgruppe"><legend>Für welche Standorte?</legend><div class="vw-schalter-raster">' + \
                '<label class="vw-schalter"><input type="checkbox" data-alle-standorte><span>Alle</span></label>' + "".join(
                    f'<label class="vw-schalter"><input type="checkbox" data-standort name="standort_{esc(l["id"])}" value="1" {"checked" if (l["id"] == standort or art == "tagesaufgaben") else ""}><span>{esc(l["name"])}</span></label>'
                    for l in ziel_orte) + "</div></fieldset>"
        else:
            ort_haken = "".join(f'<input type="hidden" name="standort_{esc(l["id"])}" value="1">' for l in ziel_orte)
        neu_titel = {"tagesaufgaben": "Neue Tagesaufgabe", "auftraege": "Neuer Kundenauftrag", "infos": "Neue Info", "produktion": "Neuer Produktionsauftrag"}[art]
        neu_dialog = f"""
        <dialog class="vw-dialog" id="neuEintrag" {'data-auto-oeffnen' if self.query_text(query, 'neu') else ''}>
            <div class="vw-dialog-kopf"><h3>{neu_titel}</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/aufgaben/neu" enctype="multipart/form-data" class="vw-formular">
                <input type="hidden" name="art" value="{art}">
                {self.aufgaben_felder(art, {})}
                {ort_haken}
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Anlegen</button></div>
            </form>
        </dialog>"""
        erklaerung = {
            "tagesaufgaben": "Diese Aufgaben erscheinen jeden Tag im Cockpit der Standorte und werden dort abgehakt. Über Nacht sind sie wieder offen.",
            "auftraege": "Kundenaufträge erscheinen im Cockpit. Wird ein Auftrag „abgeholt – Rechnung schreiben“ markiert, landet er unter Bestellungen › Rechnungsstellung.",
            "infos": "Infos sind Hinweise, die das Team im Cockpit liest – z. B. neue Preise oder Termine.",
            "produktion": "Produktionsaufträge erscheinen im Wochenplan der Produktion.",
        }[art]

        if art == "tagesaufgaben":
            inhalt = self.aufgaben_tabelle(locations)
            filter_chips = ""
        else:
            ort_chips = ""
            if len(ziel_orte) > 1:
                ort_chips = f'<a class="vw-chip" aria-pressed="{"false" if standort else "true"}" href="/admin/aufgaben?art={art}">Alle Standorte</a>' + "".join(
                    f'<a class="vw-chip" aria-pressed="{"true" if l["id"] == standort else "false"}" href="/admin/aufgaben?art={art}&standort={quote_plus(l["id"])}">{esc(l["name"])}</a>' for l in ziel_orte
                )
            filter_chips = f'<div class="vw-chips">{ort_chips}</div>' if ort_chips else ""
            bloecke = []
            for location in (l for l in ziel_orte if not standort or l["id"] == standort):
                eintraege = self.aufgaben_liste(location, schluessel)
                states = get_cockpit_state_details(location["id"])
                zeilen = [self.aufgaben_zeile(art, location, e, states) for e in eintraege]
                bloecke.append(f"""
                <section class="vw-aufgaben-ort">
                    <h3>{esc(location['name'])} <span class="vw-zahl">{len(eintraege)}</span></h3>
                    {''.join(zeilen) or '<p class="vw-leer">Hier ist noch nichts angelegt.</p>'}
                </section>""")
            inhalt = "".join(bloecke)
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.team_kopf('aufgaben', '', f'<button type="button" class="primary" data-dialog="neuEintrag">+ {neu_titel}</button>')}
        {art_umschalter}
        <p class="vw-klein">{esc(erklaerung)}</p>
        {filter_chips}
        {inhalt}
        {neu_dialog}
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Aufgaben & Aufträge", body)

    def aufgaben_gruppen(self, locations):
        """Tagesaufgaben gleichen Namens über alle Standorte zusammenfassen."""
        gruppen = {}
        for location in locations:
            for task in normalize_cockpit_tasks(location.get("cockpit_tasks", [])):
                key = normalize_text_key(task["title"])
                g = gruppen.setdefault(key, {"title": task["title"], "bild": "", "orte": {}})
                g["orte"][location["id"]] = task
                if task.get("image_filename") and not g["bild"]:
                    g["bild"] = task["image_filename"]
        return gruppen

    def aufgaben_tabelle(self, locations):
        gruppen = self.aufgaben_gruppen(locations)
        if not gruppen:
            return '<p class="vw-leer">Noch keine Tagesaufgaben. Lege oben rechts die erste an.</p>'
        states = {l["id"]: get_cockpit_state_details(l["id"]) for l in locations}
        kopf = "".join(f'<th class="mitte">{esc(l["name"])}</th>' for l in locations)
        zeilen, dialoge = [], []
        erledigt_heute = {l["id"]: [0, 0] for l in locations}
        for nr, (key, g) in enumerate(sorted(gruppen.items(), key=lambda i: i[1]["title"].lower())):
            zellen = []
            for l in locations:
                task = g["orte"].get(l["id"])
                if not task:
                    zellen.append('<td class="mitte"><span class="vw-strich">–</span></td>')
                    continue
                if not task.get("active", True):
                    zellen.append('<td class="mitte"><span class="vw-marke grau">pausiert</span></td>')
                    continue
                erledigt_heute[l["id"]][1] += 1
                info = states[l["id"]].get(("task", task["id"]), {})
                if info.get("state") == "done" and cockpit_state_is_from_today(info):
                    erledigt_heute[l["id"]][0] += 1
                    wer = esc(info.get("completed_by") or "")
                    zellen.append(f'<td class="mitte"><span class="vw-marke gruen">✓ {wer} {esc(info["updated_at"][-5:])}</span></td>')
                else:
                    zellen.append('<td class="mitte"><span class="vw-marke honig">offen</span></td>')
            bild = f'<img class="vw-mini-bild" src="/uploads/{esc(g["bild"])}" alt="" onerror="this.remove()">' if g["bild"] else ""
            zeilen.append(
                f'<tr><td><button type="button" class="vw-textknopf" data-dialog="gruppe-{nr}">{bild}<b>{esc(g["title"])}</b><span class="vw-stift">✎</span></button></td>{"".join(zellen)}</tr>'
            )
            haken = "".join(
                f'<label class="vw-schalter"><input type="checkbox" name="aktiv_{esc(l["id"])}" value="1" {"checked" if (g["orte"].get(l["id"]) or {}).get("active") else ""}><span>{esc(l["name"])}</span></label>'
                for l in locations
            )
            dialoge.append(f"""
            <dialog class="vw-dialog" id="gruppe-{nr}">
                <div class="vw-dialog-kopf"><h3>Tagesaufgabe</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
                <form method="post" action="/admin/aufgaben/gruppe" enctype="multipart/form-data" class="vw-formular">
                    <input type="hidden" name="schluessel" value="{esc(key)}">
                    <label>Aufgabe<input name="title" required value="{esc(g['title'])}"></label>
                    <fieldset class="vw-feldgruppe"><legend>Jeden Tag im Cockpit von</legend><div class="vw-schalter-raster">{haken}</div>
                    <p class="vw-klein">Abwählen pausiert die Aufgabe dort – sie bleibt gespeichert.</p></fieldset>
                    <label>Bild (optional){f'<img class="vw-vorschau" src="/uploads/{esc(g["bild"])}" alt="" onerror="this.remove()">' if g["bild"] else ''}<input type="file" name="image" accept="image/*"></label>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
                </form>
                <form method="post" action="/admin/aufgaben/gruppe-loeschen" class="vw-gefahrzone" data-vw-confirm="„{esc(g['title'])}“ bei allen Standorten löschen?">
                    <input type="hidden" name="schluessel" value="{esc(key)}"><span>Überall löschen</span><button class="danger" type="submit">Löschen</button>
                </form>
            </dialog>""")
        fuss = "".join(
            f'<td class="mitte"><b>{e} / {t}</b><span class="vw-balken"><span style="width:{round(100 * e / t) if t else 0}%"></span></span></td>'
            for e, t in (erledigt_heute[l["id"]] for l in locations)
        )
        return f"""
        <div class="vw-tabelle-wrap"><table class="vw-tabelle vw-matrix">
            <tr><th>Aufgabe <span class="vw-klein">– zum Ändern anklicken</span></th>{kopf}</tr>
            {''.join(zeilen)}
            <tr class="summe"><td>Heute erledigt</td>{fuss}</tr>
        </table></div>
        {''.join(dialoge)}"""

    def handle_aufgaben_gruppe(self, loeschen=False):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        key = self.form_value(form, "schluessel").strip()
        locations = get_locations()
        if loeschen:
            for l in locations:
                l["cockpit_tasks"] = [t for t in normalize_cockpit_tasks(l.get("cockpit_tasks", [])) if normalize_text_key(t["title"]) != key]
            save_locations(normalize_locations(locations))
            return self.redirect("/admin/aufgaben?msg=" + quote_plus("Aufgabe bei allen Standorten gelöscht."))
        titel = self.form_value(form, "title").strip()
        if not titel:
            return self.redirect("/admin/aufgaben?error=" + quote_plus("Bitte einen Namen für die Aufgabe eingeben."))
        bild = self.save_cockpit_image_upload(form, "image", "")
        for l in locations:
            tasks = normalize_cockpit_tasks(l.get("cockpit_tasks", []))
            an = bool(self.form_value(form, f"aktiv_{l['id']}"))
            treffer = [t for t in tasks if normalize_text_key(t["title"]) == key]
            if treffer:
                for t in treffer:
                    t["title"] = titel
                    t["active"] = an
                    if bild:
                        t["image_filename"] = bild
            elif an:
                tasks.append({"id": make_cockpit_item_id("task"), "title": titel, "template": False, "active": True, "image_filename": bild})
            l["cockpit_tasks"] = tasks
        save_locations(normalize_locations(locations))
        self.redirect("/admin/aufgaben?msg=" + quote_plus(f"„{titel}“ gespeichert."))

    def aufgaben_liste(self, location, schluessel):
        normalisierer = {
            "cockpit_tasks": normalize_cockpit_tasks,
            "cockpit_orders": normalize_cockpit_orders,
            "cockpit_infos": normalize_cockpit_infos,
            "production_jobs": normalize_production_jobs,
        }[schluessel]
        return normalisierer(location.get(schluessel, []))

    def aufgaben_felder(self, art, e):
        bild_feld = '<label>Bild (optional)<input type="file" name="image" accept="image/*"></label>'
        if art == "tagesaufgaben":
            return f'<label>Aufgabe *<input name="title" required value="{esc(e.get("title", ""))}" placeholder="z. B. Vitrine reinigen"></label>{bild_feld}'
        if art == "auftraege":
            return f"""<div class="vw-zwei"><label>Kunde / Auftrag *<input name="title" required value="{esc(e.get('title', ''))}" placeholder="z. B. Geburtstagstorte Müller"></label>
                <label>Abholung am<input type="date" name="pickup_date" value="{esc(e.get('pickup_date', ''))}"></label></div>
                <label>Was genau?<textarea name="note" rows="2" placeholder="z. B. Erdbeere, Schrift „Alles Gute“">{esc(e.get('note', ''))}</textarea></label>{bild_feld}"""
        if art == "infos":
            return f"""<label>Überschrift *<input name="title" required value="{esc(e.get('title', ''))}"></label>
                <label>Text<textarea name="text" rows="3">{esc(e.get('text', ''))}</textarea></label>{bild_feld}"""
        bereiche = "".join(f'<option value="{esc(k)}" {"selected" if k == e.get("section") else ""}>{esc(v)}</option>' for k, v in PRODUCTION_SECTIONS.items())
        return f"""<div class="vw-drei"><label>Bereich<select name="section">{bereiche}</select></label>
            <label>Kunde *<input name="customer_name" required value="{esc(e.get('customer_name') or e.get('title', ''))}"></label>
            <label>Fertig bis *<input type="date" name="due_date" required value="{esc(e.get('due_date', ''))}"></label></div>
            <label>Was wurde bestellt?<textarea name="order_text" rows="2">{esc(e.get('order_text', ''))}</textarea></label>
            <label>Hinweis<input name="note" value="{esc(e.get('note', ''))}"></label>{bild_feld}"""

    def aufgaben_zeile(self, art, location, e, states):
        typ = {"tagesaufgaben": "task", "auftraege": "order", "produktion": "production_job"}.get(art)
        info = states.get((typ, e["id"]), {}) if typ else {}
        zustand = info.get("state", "open")
        if not e.get("active", True):
            marke = '<span class="vw-marke grau">pausiert</span>'
        elif art == "tagesaufgaben":
            if zustand == "done" and cockpit_state_is_from_today(info):
                wer = f' von {esc(info["completed_by"])}' if info.get("completed_by") else ""
                marke = f'<span class="vw-marke gruen">✓ heute erledigt{wer} · {esc(info["updated_at"][-5:])}</span>'
            else:
                marke = '<span class="vw-marke honig">heute offen</span>'
        elif art == "auftraege":
            marke = {
                "paid_picked": '<span class="vw-marke gruen">bezahlt &amp; abgeholt</span>',
                "picked_invoice": '<span class="vw-marke rot">abgeholt – Rechnung offen</span>',
            }.get(zustand, '<span class="vw-marke blau">offen</span>')
        elif art == "produktion":
            marke = {"started": '<span class="vw-marke honig">in Arbeit</span>', "done": '<span class="vw-marke gruen">fertig</span>'}.get(zustand, '<span class="vw-marke blau">offen</span>')
        else:
            marke = '<span class="vw-marke gruen">sichtbar</span>'
        titel = e.get("title") or e.get("customer_name") or ""
        unter = []
        if e.get("pickup_date"):
            unter.append("Abholung " + format_iso_date(e["pickup_date"]))
        if e.get("due_date"):
            unter.append(PRODUCTION_SECTIONS.get(e.get("section"), "") + " · fertig bis " + format_iso_date(e["due_date"]))
        if e.get("note"):
            unter.append(e["note"])
        if e.get("text"):
            unter.append(e["text"][:120])
        bild = f'<img class="vw-mini-bild" src="/uploads/{esc(e["image_filename"])}" alt="" onerror="this.remove()">' if e.get("image_filename") else ""
        aktiv_auswahl = f"""<label>Im Cockpit<select name="active"><option value="1" {'selected' if e.get('active', True) else ''}>{'aktiv' if art == 'tagesaufgaben' else 'anzeigen'}</option><option value="0" {'' if e.get('active', True) else 'selected'}>pausiert</option></select></label>"""
        return f"""
        <details class="vw-aufgabe" id="a-{esc(e['id'])}">
            <summary>{bild}<span class="vw-aufgabe-text"><b>{esc(titel)}</b>{f'<small>{esc(" · ".join(unter))}</small>' if unter else ''}</span>{marke}</summary>
            <form method="post" action="/admin/aufgaben/speichern" enctype="multipart/form-data" class="vw-formular">
                <input type="hidden" name="art" value="{art}"><input type="hidden" name="location_id" value="{esc(location['id'])}"><input type="hidden" name="item_id" value="{esc(e['id'])}">
                {self.aufgaben_felder(art, e)}
                {aktiv_auswahl}
                <div class="vw-knopfreihe"><span class="vw-wachsen"></span>
                    <button class="primary" type="submit">Speichern</button>
                    <button class="danger" type="submit" formaction="/admin/aufgaben/loeschen" data-vw-confirm="„{esc(titel)}“ bei {esc(location['name'])} löschen?">Löschen</button>
                </div>
            </form>
        </details>"""

    def aufgaben_eintrag_aus_formular(self, form, art, alt=None):
        alt = alt or {}
        bild = self.save_cockpit_image_upload(form, "image", alt.get("image_filename", ""))
        aktiv = self.form_value(form, "active", "1") != "0"
        if art == "tagesaufgaben":
            return {"id": alt.get("id", ""), "title": self.form_value(form, "title").strip(), "template": bool(alt.get("template")), "active": aktiv, "image_filename": bild}
        if art == "auftraege":
            return {"id": alt.get("id", ""), "title": self.form_value(form, "title").strip(), "note": self.form_value(form, "note").strip(),
                    "pickup_date": self.form_value(form, "pickup_date").strip(), "active": aktiv, "image_filename": bild}
        if art == "infos":
            return {"id": alt.get("id", ""), "title": self.form_value(form, "title").strip(), "text": self.form_value(form, "text").strip(), "active": aktiv, "image_filename": bild}
        kunde = self.form_value(form, "customer_name").strip()
        return {"id": alt.get("id", ""), "section": self.form_value(form, "section").strip(), "title": kunde, "customer_name": kunde,
                "order_text": self.form_value(form, "order_text").strip(), "due_date": self.form_value(form, "due_date").strip(),
                "note": self.form_value(form, "note").strip(), "active": aktiv, "source": alt.get("source", "admin"), "image_filename": bild}

    def handle_aufgaben(self, aktion):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        art = self.form_value(form, "art").strip()
        arten = dict((a[0], a[2]) for a in self.AUFGABEN_ARTEN)
        if art not in arten:
            return self.redirect("/admin/aufgaben?error=" + quote_plus("Unbekannte Art."))
        schluessel = arten[art]
        locations = get_locations()
        zurueck = f"/admin/aufgaben?art={art}"
        if aktion == "neu":
            ziele = [l["id"] for l in locations if self.form_value(form, f"standort_{l['id']}")]
            if not ziele:
                return self.redirect(zurueck + "&neu=1&error=" + quote_plus("Bitte mindestens einen Standort ankreuzen."))
            eintrag = self.aufgaben_eintrag_aus_formular(form, art)
            if not (eintrag.get("title") or eintrag.get("note")):
                return self.redirect(zurueck + "&neu=1&error=" + quote_plus("Bitte einen Titel eingeben."))
            anzahl = 0
            for location in locations:
                if location["id"] in ziele:
                    neu = dict(eintrag)
                    neu["id"] = make_cockpit_item_id(art[:5])
                    location[schluessel] = list(self.aufgaben_liste(location, schluessel)) + [neu]
                    anzahl += 1
            save_locations(normalize_locations(locations))
            namen = ", ".join(l["name"] for l in locations if l["id"] in ziele)
            return self.redirect(zurueck + "&msg=" + quote_plus(f"Angelegt für {namen}."))
        location_id = self.form_value(form, "location_id").strip()
        item_id = self.form_value(form, "item_id").strip()
        for location in locations:
            if location["id"] != location_id:
                continue
            eintraege = list(self.aufgaben_liste(location, schluessel))
            for index, alt in enumerate(eintraege):
                if alt["id"] != item_id:
                    continue
                if aktion == "loeschen":
                    eintraege.pop(index)
                    text = "Gelöscht."
                else:
                    neu = self.aufgaben_eintrag_aus_formular(form, art, alt)
                    neu["id"] = alt["id"]
                    eintraege[index] = neu
                    text = "Gespeichert."
                location[schluessel] = eintraege
                save_locations(normalize_locations(locations))
                anker = "" if aktion == "loeschen" else f"#a-{item_id}"
                return self.redirect(zurueck + "&msg=" + quote_plus(text) + anker)
        self.redirect(zurueck + "&error=" + quote_plus("Eintrag wurde nicht gefunden – vielleicht schon gelöscht?"))

    # =================================================================
    #   Standorte – Liste und je Standort eine Seite mit Reitern
    # =================================================================
    STANDORT_REITER = [
        ("allgemein", "Allgemein"),
        ("sortiment", "Sortiment"),
        ("mindestbestand", "Mindestbestand"),
        ("zeiterfassung", "Zeiterfassung"),
    ]

    def show_locations(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        if self.query_text(query, "open_location"):
            return self.redirect("/admin/location?id=" + quote_plus(self.query_text(query, "open_location")))
        produkte = get_products(False)
        karten = []
        for l in get_locations():
            sichtbar = sum(1 for p in produkte if l["id"] in product_visible_location_keys(p["visible_to"]))
            merkmale = [
                f'<span class="vw-marke blau">{esc(role_label(l.get("role", "standard")))}</span>',
                f'<span class="vw-marke {"gruen" if l.get("time_tracking_enabled") else "grau"}">Zeiterfassung {"an" if l.get("time_tracking_enabled") else "aus"}</span>',
            ]
            if l.get("min_stock_enabled"):
                merkmale.append('<span class="vw-marke honig">Mindestbestand</span>')
            if not l.get("password"):
                merkmale.append('<span class="vw-marke rot">ohne PIN</span>')
            karten.append(f"""
            <a class="vw-karte vw-standort-karte" href="/admin/location?id={quote_plus(l['id'])}">
                <span class="vw-karte-kopf"><h3>{esc(l['name'])}</h3><span class="vw-pfeil">›</span></span>
                <span class="vw-klein">{sichtbar} von {len(produkte)} Produkten sichtbar{(' · bestellt: ' + esc(l.get('contact_name'))) if l.get('contact_name') else ''}</span>
                <span class="vw-merkmale">{''.join(merkmale)}</span>
            </a>""")
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Standorte', 'Zugang, Sortiment, Mindestbestand und Zeiterfassung – je Standort an einem Platz.', '<button type="button" class="primary" data-dialog="neuerStandort">+ Neuer Standort</button>')}
        <div class="vw-raster-3">{''.join(karten) or '<p class="vw-leer">Noch keine Standorte.</p>'}</div>
        <dialog class="vw-dialog" id="neuerStandort">
            <div class="vw-dialog-kopf"><h3>Neuer Standort</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/location/neu" class="vw-formular">
                <label>Name *<input name="name" required placeholder="z. B. Filiale Mölln"></label>
                <div class="vw-zwei"><label>Art<select name="role">{role_options('standard')}</select></label>
                <label>PIN für den Zugang<input name="password" autocomplete="new-password" placeholder="leer = ohne PIN"></label></div>
                <p class="vw-klein">Der neue Standort sieht zunächst alle Produkte. Das kannst du danach unter „Sortiment“ einschränken.</p>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Anlegen</button></div>
            </form>
        </dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Standorte", body)

    def show_location_detail(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        location = find_location(self.query_text(query, "id"))
        if not location:
            return self.redirect("/admin/locations?error=" + quote_plus("Standort wurde nicht gefunden."))
        tab = self.query_text(query, "tab") or "allgemein"
        if tab not in [t[0] for t in self.STANDORT_REITER]:
            tab = "allgemein"
        lid = quote_plus(location["id"])
        reiter = [(k, l, f"/admin/location?id={lid}&tab={k}") for k, l in self.STANDORT_REITER]
        kopf = admin_kopf(location["name"], esc(role_label(location.get("role", "standard"))), '<a class="button" href="/admin/locations">‹ Alle Standorte</a>', reiter, tab)
        versteckt = f'<input type="hidden" name="location_id" value="{esc(location["id"])}"><input type="hidden" name="tab" value="{tab}">'
        if tab == "allgemein":
            inhalt = f"""
            <section class="vw-karte vw-schmal">
                <form method="post" action="/admin/location/speichern" class="vw-formular">{versteckt}
                    <label>Name des Standorts *<input name="name" required value="{esc(location['name'])}"></label>
                    <div class="vw-zwei">
                        <label>Art<select name="role">{role_options(location.get('role', 'standard'))}</select></label>
                        <label>Wer bestellt hier? (optional)<input name="contact_name" value="{esc(location.get('contact_name', ''))}" placeholder="z. B. Lisa"></label>
                    </div>
                    <label>PIN für den Zugang<span class="password-wrap"><input class="password-field" type="password" name="password" value="{esc(location.get('password', ''))}" autocomplete="new-password" placeholder="leer = ohne PIN"><button type="button" class="password-toggle">Anzeigen</button></span></label>
                    <p class="vw-klein">Mit dieser PIN meldet sich der Standort im Bestellbereich an.</p>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
                </form>
                <form method="post" action="/admin/location/loeschen" class="vw-gefahrzone" data-vw-confirm="Standort „{esc(location['name'])}“ wirklich löschen? Bestellungen und Zeiten bleiben gespeichert, aber der Standort kann sich nicht mehr anmelden.">
                    <input type="hidden" name="location_id" value="{esc(location['id'])}">
                    <span>Standort löschen</span><button class="danger" type="submit">Löschen</button>
                </form>
            </section>"""
        elif tab == "sortiment":
            inhalt = self.standort_sortiment_html(location)
        elif tab == "mindestbestand":
            produkte = get_products(False, sort="category")
            optionen = '<option value="">Produkt wählen</option>' + "".join(
                f'<option value="{p["id"]}">{esc(p["name"])} · {esc(p["package_size"])}</option>' for p in produkte
            )
            eintraege = normalize_min_stock_items(location.get("min_stock_items", [])) or [{"product_id": "", "quantity": ""}]
            prefix = "min"
            def zeile(i, item):
                auswahl = optionen.replace(f'value="{item.get("product_id")}"', f'value="{item.get("product_id")}" selected', 1) if item.get("product_id") else optionen
                return (f'<div class="min-stock-row vw-mindest-zeile" data-min-stock-row><label>Produkt<select name="{prefix}_product_{i}">{auswahl}</select></label>'
                        f'<label>Mindestmenge<input type="number" name="{prefix}_qty_{i}" min="1" max="{MAX_ORDER_QUANTITY}" value="{esc(item.get("quantity", ""))}"></label>'
                        f'<button class="vw-leise min-stock-remove" type="button" aria-label="Zeile entfernen">×</button></div>')
            zeilen = "".join(zeile(i, it) for i, it in enumerate(eintraege))
            vorlage = zeile("__INDEX__", {}).replace(' selected', '')
            inhalt = f"""
            <section class="vw-karte">
                <form method="post" action="/admin/location/speichern" class="vw-formular">{versteckt}
                    <label class="vw-schalter"><input class="min-stock-toggle" type="checkbox" name="min_stock_enabled" value="1" data-target="vwMindestInhalt" {'checked' if location.get('min_stock_enabled') else ''}><span>Mindestbestand für {esc(location['name'])} benutzen</span></label>
                    <p class="vw-klein">Beim Bestellen erscheint dann ein Knopf „Mindestbestand in den Korb“ mit diesen Mengen als Vorschlag.</p>
                    <div id="vwMindestInhalt" {'hidden' if not location.get('min_stock_enabled') else ''}>
                        <input type="hidden" id="{prefix}_count" name="{prefix}_count" value="{len(eintraege)}">
                        <div id="{prefix}_rows" class="min-stock-rows" data-template-id="{prefix}_template" data-count-id="{prefix}_count">{zeilen}</div>
                        <template id="{prefix}_template">{vorlage}</template>
                        <button class="button add-min-stock-row" type="button" data-target="{prefix}_rows">+ Produkt</button>
                    </div>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
                </form>
            </section>"""
        else:
            ende = '<option value="">keine feste Grenze</option>' + option_html(time_options(), location.get("time_tracking_max_end", ""))
            inhalt = f"""
            <section class="vw-karte vw-schmal">
                <form method="post" action="/admin/location/speichern" class="vw-formular">{versteckt}
                    <label class="vw-schalter"><input type="checkbox" name="time_tracking_enabled" value="1" {'checked' if location.get('time_tracking_enabled') else ''}><span>Zeiterfassung an diesem Standort</span></label>
                    <label>Späteste Uhrzeit beim Eintragen<select name="time_tracking_max_end">{ende}</select></label>
                    <p class="vw-klein">Bis zu dieser Uhrzeit kann das Team ein Schichtende eintragen. Ohne Grenze gilt die aktuelle Uhrzeit.</p>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
                </form>
            </section>"""
        body = f"""{admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}{kopf}{inhalt}
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite(location["name"], body)

    def standort_sortiment_html(self, location):
        produkte = get_products(False, sort="category")
        lid = location["id"]
        sichtbar_ids = {str(p["id"]) for p in produkte if lid in product_visible_location_keys(p["visible_to"])}
        gruppen = sorted({c for p in produkte for c in product_categories(p)}, key=str.lower)
        gruppen_haken, bloecke = [], []
        for g in gruppen:
            in_gruppe = [p for p in produkte if product_has_category(p, g)]
            alle = bool(in_gruppe) and all(str(p["id"]) in sichtbar_ids for p in in_gruppe)
            an = sum(1 for p in in_gruppe if str(p["id"]) in sichtbar_ids)
            gruppen_haken.append(
                f'<label class="vw-schalter"><input class="visibility-category-toggle" type="checkbox" name="category_{esc(g)}" value="1" data-category="{esc(g)}" {"checked" if alle else ""}><span>{esc(g)} <small>{an}/{len(in_gruppe)}</small></span></label>'
            )
            haken = "".join(
                f'<label class="vw-haken-zeile"><input class="visibility-product-check" type="checkbox" name="product_{p["id"]}" value="1" data-category="{esc(g)}" {"checked" if str(p["id"]) in sichtbar_ids else ""}> {esc(p["name"])} <small>{esc(p["package_size"])}</small></label>'
                for p in in_gruppe
            )
            bloecke.append(f'<details class="vw-aufklapp vw-sicht-gruppe"><summary>{esc(g)} <span class="vw-klein">{an} von {len(in_gruppe)} sichtbar</span></summary><div class="vw-haken-liste">{haken}</div></details>')
        return f"""
        <section class="vw-karte">
            <p>Welche Produkte sieht <b>{esc(location['name'])}</b> beim Bestellen? Aktuell: <b>{len(sichtbar_ids)} von {len(produkte)}</b>.</p>
            <form method="post" action="/admin/visibility" class="vw-formular">
                <input type="hidden" name="location_id" value="{esc(lid)}">
                <input type="hidden" name="zurueck" value="standort">
                <fieldset class="vw-feldgruppe"><legend>Ganze Warengruppen an/aus</legend><div class="vw-schalter-raster">{''.join(gruppen_haken)}</div></fieldset>
                <p class="vw-klein">Für einzelne Produkte die Gruppe aufklappen:</p>
                {''.join(bloecke)}
                <div class="vw-dialog-fuss vw-klebend"><button class="primary" type="submit">Sortiment speichern</button></div>
            </form>
        </section>"""

    def handle_location_speichern(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        lid = self.form_value(form, "location_id").strip()
        tab = self.form_value(form, "tab").strip()
        locations = get_locations()
        location = next((l for l in locations if l["id"] == lid), None)
        if not location:
            return self.redirect("/admin/locations?error=" + quote_plus("Standort wurde nicht gefunden."))
        if tab == "allgemein":
            name = self.form_value(form, "name").strip()
            if not name:
                return self.redirect(f"/admin/location?id={quote_plus(lid)}&error=" + quote_plus("Bitte einen Namen eingeben."))
            location["name"] = name
            location["role"] = self.form_value(form, "role", "standard").strip()
            location["contact_name"] = self.form_value(form, "contact_name").strip()
            location["password"] = self.form_value(form, "password").strip()
        elif tab == "mindestbestand":
            location["min_stock_enabled"] = bool(self.form_value(form, "min_stock_enabled"))
            location["min_stock_items"] = self.collect_min_stock_items(form, "min")
        elif tab == "zeiterfassung":
            location["time_tracking_enabled"] = bool(self.form_value(form, "time_tracking_enabled"))
            location["time_tracking_max_end"] = self.form_value(form, "time_tracking_max_end").strip()
        save_locations(normalize_locations(locations))
        self.redirect(f"/admin/location?id={quote_plus(lid)}&tab={quote_plus(tab)}&msg=" + quote_plus("Gespeichert."))

    def handle_location_neu(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        name = self.form_value(form, "name").strip()
        if not name:
            return self.redirect("/admin/locations?error=" + quote_plus("Bitte einen Namen eingeben."))
        locations = get_locations()
        locations.append({
            "id": make_location_id(name), "name": name, "role": self.form_value(form, "role", "standard").strip(),
            "contact_name": "", "password": self.form_value(form, "password").strip(),
            "time_tracking_enabled": False, "time_tracking_max_end": "", "min_stock_enabled": False, "min_stock_items": [],
            "cockpit_tasks": [], "cockpit_orders": [], "cockpit_infos": [], "production_jobs": [],
        })
        normalized = normalize_locations(locations)
        save_locations(normalized)
        new_id = normalized[-1]["id"]
        apply_category_visibility_for_location(new_id, get_category_names(True))
        self.redirect(f"/admin/location?id={quote_plus(new_id)}&tab=sortiment&msg=" + quote_plus(f"„{name}“ angelegt. Wähle jetzt, welche Produkte der Standort sehen soll."))

    def handle_location_loeschen(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        lid = self.form_value(form, "location_id").strip()
        locations = get_locations()
        rest = [l for l in locations if l["id"] != lid]
        if not rest:
            return self.redirect(f"/admin/location?id={quote_plus(lid)}&error=" + quote_plus("Der letzte Standort kann nicht gelöscht werden."))
        name = next((l["name"] for l in locations if l["id"] == lid), "")
        save_locations(normalize_locations(rest))
        self.redirect("/admin/locations?msg=" + quote_plus(f"Standort „{name}“ gelöscht."))

    # =================================================================
    #   Nachrichten im Chat-Stil
    # =================================================================
    def show_admin_messages(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        ort = self.query_text(query, "location")
        person = self.query_text(query, "recipient")
        explizit = bool(ort and person)
        threads = cockpit_message_threads()
        if not explizit and threads:
            ort, person = threads[0]["location_id"], threads[0]["recipient_name"]
        nachrichten = get_cockpit_messages(ort, person, mark_read_for="admin") if ort and person else []
        threads = cockpit_message_threads()
        liste = []
        for t in threads:
            aktiv = t["location_id"] == ort and normalize_text_key(t["recipient_name"]) == normalize_text_key(person)
            punkt = f'<i class="vw-punkt">{t["unread_admin"]}</i>' if t["unread_admin"] else ""
            liste.append(
                f'<a class="vw-faden {"ist-aktiv" if aktiv else ""} {"ist-neu" if t["unread_admin"] else ""}" href="/admin/messages?location={quote_plus(t["location_id"])}&recipient={quote_plus(t["recipient_name"])}">'
                f'<span class="vw-avatar">{esc((t["recipient_name"] or "?")[:1].upper())}</span>'
                f'<span class="vw-faden-text"><b>{esc(t["recipient_name"])} <small>· {esc(t["location_name"])}</small></b><small>{esc((t.get("last_message") or "")[:70])}</small></span>'
                f'<span class="vw-zeit">{esc(freundliches_datum(t["last_created_at"]))}{punkt}</span></a>'
            )
        verlauf = "".join(
            f'<div class="vw-blase {"eigen" if m["sender_role"] == "admin" else ""}"><p>{esc(m["message"])}</p><small>{"Du" if m["sender_role"] == "admin" else esc(m["sender_name"])} · {esc(freundliches_datum(m["created_at"]))}</small></div>'
            for m in nachrichten
        )
        ort_name = next((l["name"] for l in get_locations() if l["id"] == ort), ort)
        chat = '<p class="vw-leer">Wähle links ein Gespräch aus.</p>'
        if ort and person:
            chat = f"""
            <div class="vw-chat-kopf"><a class="vw-zurueck" href="/admin/messages" aria-label="Zur Liste">‹</a><span class="vw-avatar">{esc(person[:1].upper())}</span><span><b>{esc(person)}</b><small>{esc(ort_name)}</small></span></div>
            <div class="vw-chat-verlauf">{verlauf or '<p class="vw-leer">Noch keine Nachrichten.</p>'}</div>
            <form method="post" action="/admin/messages/reply" class="vw-chat-eingabe">
                <input type="hidden" name="location_id" value="{esc(ort)}"><input type="hidden" name="recipient_name" value="{esc(person)}">
                <textarea name="message" rows="2" required placeholder="Nachricht an {esc(person)} …" aria-label="Nachricht"></textarea>
                <button class="primary" type="submit">Senden</button>
            </form>"""
        orte = "".join(f'<option value="{esc(l["id"])}">{esc(l["name"])}</option>' for l in get_locations())
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Nachrichten', 'Mit dem Team an den Standorten schreiben.', '<button type="button" class="primary" data-dialog="neueNachricht">+ Neue Nachricht</button>')}
        <div class="vw-chat {'hat-auswahl' if explizit else ''}">
            <div class="vw-faeden">{''.join(liste) or '<p class="vw-leer">Noch keine Nachrichten.</p>'}</div>
            <div class="vw-chat-fenster">{chat}</div>
        </div>
        <dialog class="vw-dialog" id="neueNachricht">
            <div class="vw-dialog-kopf"><h3>Neue Nachricht</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/messages/reply" class="vw-formular">
                <div class="vw-zwei"><label>Standort<select name="location_id" required>{orte}</select></label>
                <label>An<select name="recipient_name" required>{option_html(get_time_employee_names(True))}</select></label></div>
                <label>Nachricht<textarea name="message" rows="4" required></textarea></label>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Senden</button></div>
            </form>
        </dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Nachrichten", body)

    # =================================================================
    #   Einstellungen
    # =================================================================
    def show_settings(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        s = load_settings()
        smtp = smtp_is_configured()
        push_an = push_is_configured()
        geraete = push_subscription_count(enabled_only=True)
        def adresse(key, label, hilfe):
            wert = s.get(key, "")
            warnung = '' if not wert or email_sieht_gueltig_aus(wert) else '<small class="vw-notiz">Diese Adresse sieht unvollständig aus.</small>'
            return f'<label>{label}<input type="email" name="{key}" value="{esc(wert)}">{warnung}<small class="vw-klein-block">{hilfe}</small></label>'
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Einstellungen')}
        <div class="vw-raster-2">
            <section class="vw-karte">
                <h3>E-Mail &amp; WhatsApp</h3>
                <form method="post" action="/admin/settings" class="vw-formular">
                    {adresse('order_email_to', 'Bestellungen gehen an', 'Jeden Morgen um 6 Uhr kommt eine Mail mit allen Bestellungen vom Vortag (mit den PDFs im Anhang). Gab es keine Bestellungen, kommt keine Mail.')}
                    {adresse('time_export_email', 'Zeiten-Export geht an', 'Am Monatsanfang kommt hier die Zeiterfassung des Vormonats an.')}
                    <label>WhatsApp-Nummer<input name="whatsapp_number" value="{esc(s.get('whatsapp_number', ''))}"><small class="vw-klein-block">Für den WhatsApp-Knopf nach dem Bestellen.</small></label>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
                </form>
                <div class="vw-status {'ok' if smtp else 'warn'}">{'E-Mail-Versand ist eingerichtet.' if smtp else 'E-Mail-Versand ist auf dem Server nicht eingerichtet – es gehen keine Mails raus. Die Zugangsdaten deines Mail-Postfachs müssen einmalig auf dem Server hinterlegt werden.'}</div>
                <div class="vw-knopfreihe"><form method="post" action="/admin/settings/testmail"><button class="button" type="submit" {'disabled' if not smtp else ''}>Test-Mail an „Bestellungen“ senden</button></form>
                <form method="post" action="/admin/settings/tagesmail"><button class="button" type="submit" {'disabled' if not smtp else ''}>Zusammenfassung von gestern jetzt senden</button></form></div>
                <p class="vw-klein">Letzte Tages-Zusammenfassung: {esc(format_iso_date(s.get('tagesmail_zuletzt', '')) or 'noch keine')}</p>
            </section>
            <section class="vw-karte">
                <h3>Admin-PIN ändern</h3>
                <form method="post" action="/admin/settings" class="vw-formular">
                    <input type="hidden" name="change_admin_pin" value="1">
                    <label>Aktuelle PIN<span class="password-wrap"><input class="password-field" type="password" name="current_admin_pin" autocomplete="current-password" required><button type="button" class="password-toggle">Anzeigen</button></span></label>
                    <label>Neue PIN (mind. 4 Zeichen)<span class="password-wrap"><input class="password-field" type="password" name="new_admin_pin" autocomplete="new-password" minlength="4" required><button type="button" class="password-toggle">Anzeigen</button></span></label>
                    <label>Neue PIN wiederholen<span class="password-wrap"><input class="password-field" type="password" name="new_admin_pin_confirm" autocomplete="new-password" minlength="4" required><button type="button" class="password-toggle">Anzeigen</button></span></label>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">PIN ändern</button></div>
                </form>
            </section>
            <section class="vw-karte">
                <h3>Push-Nachrichten</h3>
                <p class="vw-klein">Damit dein Handy oder Mac kurz meldet, wenn eine Bestellung oder Nachricht reinkommt.</p>
                <div class="vw-status {'ok' if push_an else 'warn'}">{'Auf dem Server eingerichtet' if push_an else 'Auf dem Server noch nicht eingerichtet'} · {geraete} Gerät(e) angemeldet</div>
                <div id="pushControl" class="push-control vw-knopfreihe" hidden><span id="pushStatus">Push nicht aktiv</span><button type="button" id="pushToggle" class="button">Auf diesem Gerät aktivieren</button></div>
                <form method="post" action="/admin/push/test" class="vw-knopfreihe"><button class="button" type="submit" {'disabled' if not push_an else ''}>Test-Push senden</button></form>
            </section>
            <section class="vw-karte">
                <h3>Sicherung</h3>
                <p class="vw-klein">Lädt alle Daten (Bestellungen, Zeiten, Produkte, Bilder, Einstellungen) als eine Zip-Datei herunter. Am Mac macht das auch dein Sicherungsprogramm auf dem Schreibtisch.</p>
                <div class="vw-knopfreihe"><a class="button primary" href="/admin/backup">Sicherung herunterladen</a></div>
                <p class="vw-klein">Datenordner auf dem Server: {esc(os.path.abspath(DATA_DIR))}</p>
            </section>
        </div>
        <div class="vw-knopfreihe"><a class="button" href="/admin/logout">Abmelden</a></div>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>
        """
        self.admin_seite("Einstellungen", body)

    def handle_tagesmail_jetzt(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        self.read_form()
        ok, text = tageszusammenfassung_senden((berlin_now() - timedelta(days=1)).date())
        self.redirect("/admin/settings?" + ("msg=" if ok else "error=") + quote_plus(text))

    def handle_test_mail(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        self.read_form()
        empfaenger = (load_settings().get("order_email_to") or "").strip()
        if not smtp_is_configured():
            return self.redirect("/admin/settings?error=" + quote_plus("E-Mail-Versand ist auf dem Server nicht eingerichtet."))
        if not email_sieht_gueltig_aus(empfaenger):
            return self.redirect("/admin/settings?error=" + quote_plus("Bitte zuerst eine gültige Adresse für Bestellungen eintragen."))
        try:
            msg = EmailMessage()
            msg["Subject"] = "Test aus dem Bestellsystem Opa Peters"
            msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
            msg["To"] = empfaenger
            msg.set_content("Wenn du diese Mail liest, funktioniert der E-Mail-Versand des Bestellsystems.")
            with smtp_oeffnen(os.environ.get("SMTP_HOST"), os.environ.get("SMTP_PORT", "587")) as smtp:
                smtp.login(os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"))
                smtp.send_message(msg)
        except Exception as exc:
            return self.redirect("/admin/settings?error=" + quote_plus(f"Test-Mail ging nicht raus: {exc}"))
        self.redirect("/admin/settings?msg=" + quote_plus(f"Test-Mail an {empfaenger} verschickt."))

    # =================================================================
    #   BUCHHALTUNG
    # =================================================================
    def bh_lauf_html(self):
        if bh.LAUF.get("laeuft"):
            return '<div class="vw-meldung ok bh-lauf" data-bh-lauf>Belege werden gerade abgeholt … Die Seite aktualisiert sich von selbst.</div>'
        return ""

    def bh_abholen_knopf(self, quelle_id=None, text="Jetzt Belege abholen", klasse="primary"):
        versteckt = f'<input type="hidden" name="quelle_id" value="{quelle_id}">' if quelle_id else ""
        return f'<form method="post" action="/admin/buchhaltung/abholen" class="vw-inline-form">{versteckt}<button class="{klasse}" type="submit" {"disabled" if bh.LAUF.get("laeuft") else ""}>⟳ {esc(text)}</button></form>'

    def bh_monat(self, query):
        monat = self.query_text(query, "monat")
        return monat if re.match(r"^\d{4}-\d{2}$", monat or "") else bh.jetzt().strftime("%Y-%m")

    def bh_monat_leiste(self, pfad, monat, extra=""):
        jahr, mon = int(monat[:4]), int(monat[5:])
        vorher = f"{jahr - (mon == 1)}-{12 if mon == 1 else mon - 1:02d}"
        nachher = f"{jahr + (mon == 12)}-{1 if mon == 12 else mon + 1:02d}"
        namen = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
        trenner = "&" if "?" in pfad else "?"
        return (f'<div class="vw-monat"><a class="button vw-rund" href="{pfad}{trenner}monat={vorher}{extra}" aria-label="Monat davor">‹</a>'
                f'<b>{namen[mon - 1]} {jahr}</b><a class="button vw-rund" href="{pfad}{trenner}monat={nachher}{extra}" aria-label="Monat danach">›</a></div>')

    def send_bytes(self, daten, content_type, dateiname="", inline=True):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(daten)))
        self.send_header("Cache-Control", "no-store")
        if dateiname:
            art = "inline" if inline else "attachment"
            self.send_header("Content-Disposition", f"{art}; filename=\"{dateiname.encode('ascii', 'replace').decode()}\"; filename*=UTF-8''{quote(dateiname)}")
        self.end_headers()
        self.wfile.write(daten)

    # ---------------------------------------------------------- Übersicht
    def show_bh_status(self, query=None):
        if not self.is_admin():
            return self.send_json({"ok": False}, 403)
        return self.send_json({"laeuft": bool(bh.LAUF.get("laeuft")), "meldung": bh.LAUF.get("meldung", "")})

    def handle_bh_abholen(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        qid = self.form_value(form, "quelle_id").strip()
        zurueck = "/admin/buchhaltung/einstellungen" if qid else "/admin/buchhaltung"
        if bh.alle_abholen(int(qid) if qid.isdigit() else None):
            return self.redirect(zurueck + "?msg=" + quote_plus("Abholen gestartet. Das dauert je nach Postfach ein paar Minuten."))
        self.redirect(zurueck + "?error=" + quote_plus("Es läuft gerade schon ein Abholvorgang."))

    # ---------------------------------------------------------- Quellen
    def bh_quelle_formular(self, q=None):
        q = q or {}
        anbieter = "".join(
            f'<option value="{k}" data-host="{esc(v[1])}" data-port="{v[2]}" {"selected" if k == (q.get("anbieter") or "strato") else ""}>{esc(v[0])}</option>'
            for k, v in bh.ANBIETER.items()
        )
        richtung = "".join(
            f'<option value="{k}" {"selected" if k == (q.get("richtung") or "auto") else ""}>{v}</option>'
            for k, v in (("auto", "automatisch erkennen"), ("eingang", "nur Eingangsrechnungen"), ("ausgang", "nur Ausgangsrechnungen"))
        )
        start = q.get("start_datum") or (bh.jetzt().replace(day=1).date().isoformat())
        return f"""
            <input type="hidden" name="quelle_id" value="{q.get('id', '')}">
            <label>Name *<input name="name" required value="{esc(q.get('name', ''))}" placeholder="z. B. belege@opapeters.de oder Postfach info@"></label>
            <div class="vw-zwei">
                <label>Anbieter<select name="anbieter" data-bh-anbieter>{anbieter}</select></label>
                <label>Abholen ab<input type="date" name="start_datum" value="{esc(start)}"></label>
            </div>
            <div class="vw-zwei bh-host" {'hidden' if (q.get('anbieter') or 'strato') != 'andere' else ''}>
                <label>Server (IMAP)<input name="host" value="{esc(q.get('host', ''))}" placeholder="imap.beispiel.de"></label>
                <label>Port<input name="port" inputmode="numeric" value="{esc(q.get('port') or 993)}"></label>
            </div>
            <div class="vw-zwei">
                <label>E-Mail-Adresse / Benutzer *<input name="benutzer" required autocomplete="off" value="{esc(q.get('benutzer', ''))}"></label>
                <label>Passwort {'(leer lassen = unverändert)' if q.get('id') else '*'}<span class="password-wrap"><input class="password-field" type="password" name="passwort" autocomplete="new-password" {'required' if not q.get('id') else ''}><button type="button" class="password-toggle">Anzeigen</button></span></label>
            </div>
            <label>Ordner<input name="ordner" value="{esc(q.get('ordner') or 'INBOX')}"><small class="vw-klein-block">INBOX = Posteingang. Mehrere mit Komma, z. B. „INBOX, Gesendet“ (Gesendet = für Ausgangsrechnungen). Welche Ordner es gibt, zeigt „Verbindung testen“.</small></label>
            <div class="vw-zwei">
                <label>Was ist es?<select name="richtung">{richtung}</select></label>
                <label class="vw-schalter bh-schalter"><input type="checkbox" name="alles_ist_beleg" value="1" {'checked' if q.get('alles_ist_beleg') else ''}><span>Eigenes Belege-Postfach: jeder Anhang ist ein Beleg</span></label>
            </div>"""

    def handle_bh_quelle(self, aktion):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        qid = self.form_value(form, "quelle_id").strip()
        zurueck = "/admin/buchhaltung/einstellungen"
        con = bh.db()
        try:
            if aktion == "speichern":
                anbieter = self.form_value(form, "anbieter", "strato").strip()
                host, port = bh.ANBIETER.get(anbieter, ("", "", 993))[1:]
                if anbieter == "andere":
                    host = self.form_value(form, "host").strip()
                    port = int(self.form_value(form, "port", "993").strip() or 993)
                werte = {
                    "name": self.form_value(form, "name").strip()[:80], "anbieter": anbieter, "host": host, "port": port,
                    "benutzer": self.form_value(form, "benutzer").strip(), "ordner": self.form_value(form, "ordner", "INBOX").strip() or "INBOX",
                    "alles_ist_beleg": 1 if self.form_value(form, "alles_ist_beleg") else 0,
                    "richtung": self.form_value(form, "richtung", "auto"), "start_datum": bh.datum_iso(self.form_value(form, "start_datum")),
                }
                if not werte["name"] or not werte["benutzer"] or not werte["host"]:
                    return self.redirect(zurueck + "?error=" + quote_plus("Bitte Name, Benutzer und Server ausfüllen."))
                passwort = self.form_value(form, "passwort")
                if qid.isdigit():
                    setze = ", ".join(f"{k}=?" for k in werte)
                    con.execute(f"UPDATE quellen SET {setze}, letzter_lauf='' WHERE id=?", (*werte.values(), int(qid)))
                    if passwort:
                        con.execute("UPDATE quellen SET geheim=?, letzter_fehler=0 WHERE id=?", (bh.verschluesseln(passwort), int(qid)))
                    neue_id = int(qid)
                else:
                    if not passwort:
                        return self.redirect(zurueck + "?error=" + quote_plus("Bitte das Passwort eingeben."))
                    neue_id = con.execute(
                        f"INSERT INTO quellen ({', '.join(werte)}, art, geheim, erstellt) VALUES ({', '.join('?' for _ in werte)}, 'imap', ?, ?)",
                        (*werte.values(), bh.verschluesseln(passwort), bh.jetzt_text())).lastrowid
                bh.protokolliere(con, "quelle", f"Quelle {neue_id} gespeichert: {werte['name']}")
                con.commit()
                con.close()
                con = None
                ok, text = bh.quelle_testen(neue_id)
                return self.redirect(zurueck + ("?msg=" if ok else "?error=") + quote_plus(("Gespeichert. " if ok else "Gespeichert, aber: ") + text) + f"#q-{neue_id}")
            if not qid.isdigit():
                return self.redirect(zurueck)
            if aktion == "testen":
                con.close()
                con = None
                ok, text = bh.quelle_testen(int(qid))
                return self.redirect(zurueck + ("?msg=" if ok else "?error=") + quote_plus(text) + f"#q-{qid}")
            if aktion == "aktiv":
                con.execute("UPDATE quellen SET aktiv = 1 - aktiv WHERE id=?", (int(qid),))
                con.commit()
                return self.redirect(zurueck + f"#q-{qid}")
            if aktion == "loeschen":
                con.execute("DELETE FROM quellen WHERE id=?", (int(qid),))
                bh.protokolliere(con, "quelle", f"Quelle {qid} entfernt")
                con.commit()
                return self.redirect(zurueck + "?msg=" + quote_plus("Quelle entfernt. Die gefundenen Belege bleiben erhalten."))
        finally:
            if con:
                con.close()
        self.redirect(zurueck)

    def handle_bh_einstellungen(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        con = bh.db()
        bh.setze_einstellung(con, "eigene_namen", self.form_value(form, "eigene_namen").strip())
        bh.setze_einstellung(con, "eigene_domains", self.form_value(form, "eigene_domains").strip().replace("@", ""))
        for feld in ("belege_adresse", "erinnerung_an"):
            wert = self.form_value(form, feld).strip()
            bh.setze_einstellung(con, feld, wert if email_sieht_gueltig_aus(wert) else "")
        con.commit()
        con.close()
        self.redirect("/admin/buchhaltung/einstellungen?msg=" + quote_plus("Gespeichert."))

    # ---------------------------------------------------------- Belege
    def bh_upload_dialog(self, buchung_id=""):
        return f"""
        <dialog class="vw-dialog" id="belegHochladen">
            <div class="vw-dialog-kopf"><h3>Beleg hochladen</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/buchhaltung/beleg/upload" enctype="multipart/form-data" class="vw-formular" data-bh-upload>
                <input type="hidden" name="buchung_id" value="{buchung_id}">
                <label>PDF, XML oder Foto (mehrere möglich)<input type="file" name="datei" accept=".pdf,.xml,image/*" multiple></label>
                <label>Oder mit dem Handy fotografieren<input type="file" name="foto" accept="image/*" capture="environment"></label>
                <div class="vw-zwei"><label>Art<select name="art"><option value="">automatisch</option><option value="ER">Eingangsrechnung</option><option value="AR">Ausgangsrechnung</option><option value="GS">Gutschrift</option></select></label>
                <label class="vw-schalter bh-schalter"><input type="checkbox" name="bar" value="1"><span>Bar bezahlt (steht auf keinem Kontoauszug)</span></label></div>
                <p class="vw-klein" data-bh-upload-status></p>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Hochladen</button></div>
            </form>
        </dialog>"""

    def show_bh_beleg(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        bid = self.query_text(query, "id")
        con = bh.db()
        b = con.execute("SELECT * FROM belege WHERE id=?", (bid,)).fetchone() if bid.isdigit() else None
        if not b:
            con.close()
            return self.send_html(b'<p class="vw-leer">Beleg nicht gefunden.</p>', 404)
        buchungen = con.execute("SELECT bu.* FROM zuordnung z JOIN buchungen bu ON bu.id=z.buchung_id WHERE z.beleg_id=?", (b["id"],)).fetchall()
        con.close()
        if b["datei"]:
            ist_bild = (b["mime"] or "").startswith("image/") or b["datei"].lower().endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))
            if ist_bild:
                vorschau = f'<img class="bh-vorschau-bild" src="/admin/buchhaltung/datei?id={b["id"]}" alt="Beleg">'
            elif b["datei"].lower().endswith(".xml"):
                vorschau = '<p class="vw-klein">Reine E-Rechnung (XML) – die Angaben unten wurden daraus gelesen.</p>'
            else:
                vorschau = f'<iframe class="bh-vorschau" src="/admin/buchhaltung/datei?id={b["id"]}" title="Beleg"></iframe>'
            vorschau += f'<p class="vw-klein"><a href="/admin/buchhaltung/datei?id={b["id"]}" target="_blank" rel="noopener">In neuem Fenster öffnen</a>{" · <a href=/admin/buchhaltung/datei?id=" + str(b["id"]) + "&xml=1>E-Rechnung (XML)</a>" if b["xml_datei"] else ""} · Dateiname im Export: <code>{esc(bh.beleg_dateiname(b))}</code></p>'
        else:
            vorschau = f'<div class="vw-status warn">Diese Rechnung liegt nur im Kundenportal. <a href="{esc(b["portal_link"])}" target="_blank" rel="noopener">Portal öffnen</a>, PDF herunterladen und hier hochladen – dann ersetzt es diesen Hinweis.</div>'
        absender = parseaddr(b["mail_von"] or "")[1]
        frage = ""
        if b["status"] == "unsicher":
            frage = f"""
        <form method="post" action="/admin/buchhaltung/beleg/urteil" class="bh-frage">
            <input type="hidden" name="id" value="{b['id']}">
            <b>Ist das eine Rechnung?</b><small>{esc(b['notiz'] or '')}</small>
            {f'<label class="vw-schalter bh-schalter"><input type="checkbox" name="ganzer_absender" value="1"><span>Gilt für alles von {esc(absender)}</span></label>' if absender else ''}
            <div class="vw-knopfreihe"><button class="primary" type="submit" name="urteil" value="ja">Ja, Rechnung</button>
                <button class="button" type="submit" name="urteil" value="nein">Keine Rechnung</button></div>
        </form>"""
        herkunft = f'{esc(b["quelle"])}' + (f' · Mail von {esc(b["mail_von"])} „{esc(b["mail_betreff"])}“ ({esc(b["mail_datum"])})' if b["mail_von"] else "")
        arten = "".join(f'<option value="{k}" {"selected" if b["art"] == k else ""}>{v}</option>' for k, v in bh.ARTEN.items())
        zuord = "".join(f'<li>{esc(bh.datum_de(bu["datum"]))} · {esc(bh.betrag_text(bu["betrag"]))} · {esc(bu["gegenpartei"])}</li>' for bu in buchungen)
        betrag = "" if b["betrag"] is None else bh.betrag_text(b["betrag"], "EUR").replace(" €", "")
        portal_upload = ""
        if b["status"] == "portal":
            portal_upload = f"""<form method="post" action="/admin/buchhaltung/beleg/upload" enctype="multipart/form-data" class="vw-knopfreihe">
                <input type="hidden" name="ersetzt" value="{b['id']}"><input type="file" name="datei" accept=".pdf,.xml,image/*" required><button class="primary" type="submit">PDF hochladen</button></form>"""
        html_text = f"""
        <div class="vw-dialog-kopf"><h3>{esc(b['lieferant'] or 'Beleg')}</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
        {frage}<div class="bh-vorschau-wrap">{vorschau}{portal_upload}</div>
        <form method="post" action="/admin/buchhaltung/beleg/speichern" class="vw-formular">
            <input type="hidden" name="id" value="{b['id']}">
            <div class="vw-zwei"><label>Art<select name="art">{arten}</select></label><label>Lieferant / Kunde<input name="lieferant" value="{esc(b['lieferant'])}"></label></div>
            <div class="vw-drei"><label>Rechnungsdatum<input type="date" name="datum" value="{esc(b['datum'])}"></label>
                <label>Rechnungsnummer<input name="rechnungsnr" value="{esc(b['rechnungsnr'])}"></label>
                <label>Betrag brutto (€)<input name="betrag" inputmode="decimal" value="{esc(betrag)}" placeholder="z. B. 412,35"></label></div>
            <div class="vw-zwei"><label class="vw-schalter bh-schalter"><input type="checkbox" name="bar" value="1" {'checked' if b['bar'] else ''}><span>Bar bezahlt</span></label>
                <label class="vw-schalter bh-schalter"><input type="checkbox" name="dauer" value="1" {'checked' if b['dauer'] else ''} data-bh-dauer><span>Dauerbeleg (Vertrag, jeden Monat)</span></label></div>
            <label class="bh-dauer-muster" {'hidden' if not b['dauer'] else ''}>Erkennen an diesem Text auf dem Kontoauszug<input name="dauer_muster" value="{esc(b['dauer_muster'])}" placeholder="z. B. Miete Lager Schwarzenbek"></label>
            <label>Notiz<input name="notiz" value="{esc(b['notiz'])}"></label>
            <p class="vw-klein">Herkunft: {herkunft}{'<br>Zugeordnet zu: <ul>' + zuord + '</ul>' if zuord else ''}</p>
            <div class="vw-dialog-fuss"><button class="primary" type="submit" name="status" value="geprueft">Speichern &amp; geprüft</button></div>
        </form>
        <form method="post" action="/admin/buchhaltung/beleg/speichern" class="vw-gefahrzone">
            <input type="hidden" name="id" value="{b['id']}">
            <span>{'Keine Rechnung (z. B. BWA, Werbung, AGB)' if b['status'] not in ('ignoriert', 'doppelt') else 'Aussortiert – wieder aufnehmen?'}
                {f'<label class="vw-schalter bh-schalter"><input type="checkbox" name="ganzer_absender" value="1"><span>Alles von {esc(absender)} künftig überspringen</span></label>' if absender and b['status'] not in ('ignoriert', 'doppelt') else ''}</span>
            <button class="{'danger' if b['status'] not in ('ignoriert', 'doppelt') else 'button'}" type="submit" name="status" value="{'ignoriert' if b['status'] not in ('ignoriert', 'doppelt') else 'neu'}">{'Aussortieren' if b['status'] not in ('ignoriert', 'doppelt') else 'Wieder aufnehmen'}</button>
        </form>"""
        if self.query_text(query, "embed"):
            return self.send_html(html_text.encode("utf-8"))
        body = f'{self.bh_kopf("belege")}<section class="vw-karte vw-schmal">{html_text}</section><script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>'
        self.admin_seite("Beleg", body)

    def show_bh_datei(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        bid = self.query_text(query, "id")
        con = bh.db()
        b = con.execute("SELECT * FROM belege WHERE id=?", (bid,)).fetchone() if bid.isdigit() else None
        con.close()
        if not b:
            return self.send_error(404)
        xml = bool(self.query_text(query, "xml"))
        pfad = bh.beleg_pfad(b, xml=xml)
        if not pfad or not os.path.exists(pfad):
            return self.send_error(404)
        endung = os.path.splitext(pfad)[1].lower()
        typ = {".pdf": "application/pdf", ".xml": "application/xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic"}.get(endung, "application/octet-stream")
        with open(pfad, "rb") as f:
            daten = f.read()
        self.send_bytes(daten, typ, bh.beleg_dateiname(b, endung), inline=True)

    def bh_upload_verarbeiten(self, form, quelle="Upload Verwaltung", vorgaben=None):
        dateien = [v for k, v in form.items() if k in ("datei", "foto") and isinstance(v, UploadedFile) and v.content]
        ids = []
        for datei in dateien:
            if len(datei.content) > 11 * 1024 * 1024:
                continue
            name = datei.filename or "beleg.pdf"
            endung = os.path.splitext(name)[1].lower()
            mime = {".pdf": "application/pdf", ".xml": "application/xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic", ".webp": "image/webp"}.get(endung, "")
            if not mime:
                continue
            beleg_id, neu, _ = bh.beleg_anlegen(datei.content, name, mime, quelle, None, None, "auto", True, vorgaben={**(vorgaben or {}), "ohne_pruefung": True})
            if beleg_id:
                ids.append((beleg_id, neu))
        return ids

    def handle_bh_beleg_upload(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        vorgaben = {"bar": bool(self.form_value(form, "bar"))}
        if self.form_value(form, "art") in bh.ARTEN:
            vorgaben["art"] = self.form_value(form, "art")
        ids = self.bh_upload_verarbeiten(form, vorgaben=vorgaben)
        ist_fetch = self.headers.get("X-Requested-With") == "fetch"
        if not ids:
            if ist_fetch:
                return self.send_json({"ok": False, "fehler": "Datei nicht erkannt (erlaubt: PDF, XML, Foto)."}, 400)
            return self.redirect("/admin/buchhaltung/belege?error=" + quote_plus("Keine passende Datei gefunden (PDF, XML oder Foto)."))
        ersetzt = self.form_value(form, "ersetzt").strip()
        if ersetzt.isdigit():
            alt = bh.db().execute("SELECT * FROM belege WHERE id=?", (int(ersetzt),)).fetchone()
            if alt and alt["status"] == "portal":
                bh.beleg_aendern(int(ersetzt), {"status": "ignoriert", "notiz": f"ersetzt durch Beleg {ids[0][0]}"})
                bh.beleg_aendern(ids[0][0], {"lieferant": alt["lieferant"]} if alt["lieferant"] else {})
        buchung_id = self.form_value(form, "buchung_id").strip()
        if buchung_id.isdigit():
            bh.zuordnen(int(buchung_id), [i for i, _ in ids])
        else:
            bh.abgleichen()
        if ist_fetch:
            return self.send_json({"ok": True, "ids": [i for i, _ in ids], "neu": sum(1 for _, n in ids if n)})
        neu = sum(1 for _, n in ids if n)
        text = f"{neu} Beleg(e) hochgeladen." + ("" if neu == len(ids) else f" {len(ids) - neu} gab es schon.")
        if buchung_id.isdigit():
            return self.redirect("/admin/buchhaltung/bank?msg=" + quote_plus(text + " Und zugeordnet."))
        self.redirect("/admin/buchhaltung/belege?filter=neu&msg=" + quote_plus(text) + f"#b-{ids[0][0]}")

    def handle_bh_beleg_speichern(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        bid = self.form_value(form, "id").strip()
        if not bid.isdigit():
            return self.redirect("/admin/buchhaltung/belege")
        felder = {"status": self.form_value(form, "status") or "geprueft"}
        if "art" in form:
            betrag = self.form_value(form, "betrag").strip()
            felder.update({
                "art": self.form_value(form, "art") if self.form_value(form, "art") in bh.ARTEN else "ER",
                "lieferant": self.form_value(form, "lieferant").strip()[:80],
                "rechnungsnr": self.form_value(form, "rechnungsnr").strip()[:60],
                "datum": bh.datum_iso(self.form_value(form, "datum")),
                "betrag": bh.cent(betrag) if betrag else None,
                "bar": 1 if self.form_value(form, "bar") else 0,
                "dauer": 1 if self.form_value(form, "dauer") else 0,
                "dauer_muster": self.form_value(form, "dauer_muster").strip()[:120],
                "notiz": self.form_value(form, "notiz").strip()[:300],
            })
        con = bh.db()
        vorher = con.execute("SELECT status FROM belege WHERE id=?", (int(bid),)).fetchone()
        con.close()
        bh.beleg_aendern(int(bid), felder)
        ganz = bool(self.form_value(form, "ganzer_absender"))
        if felder["status"] == "ignoriert":
            bh.lernen(int(bid), "nein", ganz)
        elif vorher and vorher["status"] in ("ignoriert", "unsicher"):
            bh.lernen(int(bid), "ja")
        bh.abgleichen()
        text = ("Aussortiert. Ähnliche Dokumente werden künftig übersprungen." if felder["status"] == "ignoriert" else "Gespeichert.")
        self.redirect("/admin/buchhaltung/belege?filter=neu&msg=" + quote_plus(text))

    def handle_bh_beleg_urteil(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        bid = self.form_value(form, "id").strip()
        urteil = self.form_value(form, "urteil")
        if not bid.isdigit() or urteil not in ("ja", "nein"):
            return self.redirect("/admin/buchhaltung/belege")
        bh.beleg_aendern(int(bid), {"status": "neu" if urteil == "ja" else "ignoriert", "notiz": "" if urteil == "ja" else "Keine Rechnung (von dir aussortiert)"})
        bh.lernen(int(bid), urteil, bool(self.form_value(form, "ganzer_absender")))
        bh.abgleichen()
        text = "Als Rechnung übernommen." if urteil == "ja" else "Aussortiert."
        self.redirect("/admin/buchhaltung/belege?msg=" + quote_plus(text + " Gemerkt für ähnliche Dokumente."))

    def handle_bh_erkennung(self, aktion):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        if aktion == "neu-pruefen":
            e = bh.belege_neu_pruefen()
            return self.redirect("/admin/buchhaltung/belege?msg=" + quote_plus(
                f"{e['geprueft']} Belege neu geprüft: {e['aussortiert']} aussortiert, {e['unsicher']} zum Ansehen markiert."))
        if aktion == "regel-loeschen":
            con = bh.db()
            con.execute("DELETE FROM gelernt WHERE absender=? AND titel=?", (self.form_value(form, "absender"), self.form_value(form, "titel")))
            con.commit()
            con.close()
            return self.redirect("/admin/buchhaltung/einstellungen?msg=" + quote_plus("Gelernte Regel entfernt.") + "#gelernt")
        self.redirect("/admin/buchhaltung/einstellungen")

    # ---------------------------------------------------------- Kontoauszüge & Abgleich
    def show_bh_buchung(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        bid = self.query_text(query, "id")
        suche = self.query_text(query, "suche")
        con = bh.db()
        bu = con.execute("SELECT * FROM buchungen WHERE id=?", (bid,)).fetchone() if bid.isdigit() else None
        if not bu:
            con.close()
            return self.send_html(b'<p class="vw-leer">Buchung nicht gefunden.</p>', 404)
        belege = con.execute("SELECT b.* FROM zuordnung z JOIN belege b ON b.id=z.beleg_id WHERE z.buchung_id=?", (bu["id"],)).fetchall()
        kand = bh.kandidaten(con, bu, limit=10, suche=suche)
        konto = con.execute("SELECT name FROM konten WHERE id=?", (bu["konto_id"],)).fetchone()
        con.close()
        monat = (bu["datum"] or "")[:7]
        zugeordnet = "".join(
            f'<li><a href="/admin/buchhaltung/datei?id={b["id"]}" target="_blank" rel="noopener">{esc(bh.beleg_dateiname(b))}</a>'
            f'<form method="post" action="/admin/buchhaltung/buchung/loesen" class="vw-inline-form"><input type="hidden" name="buchung_id" value="{bu["id"]}"><input type="hidden" name="beleg_id" value="{b["id"]}"><input type="hidden" name="monat" value="{monat}"><button class="vw-leise" type="submit">lösen</button></form></li>'
            for b in belege
        )
        kand_html = "".join(
            f'<label class="bh-kandidat"><input type="checkbox" name="beleg_{b["id"]}" value="1"><span><b>{esc(b["lieferant"] or "Unbekannt")}</b> · {esc(bh.betrag_text(b["betrag"], b["waehrung"]))}<small>{esc(bh.datum_de(b["datum"]))} · {esc(b["rechnungsnr"] or "ohne Nr.")} · {esc(bh.ARTEN.get(b["art"], ""))}</small></span>'
            f'<span class="vw-marke {"gruen" if p >= 75 else ("honig" if p >= 40 else "grau")}">{"passt gut" if p >= 75 else ("möglich" if p >= 40 else "")}</span></label>'
            for p, b in kand
        )
        vorschlag_muster = (bu["gegenpartei"] or "").strip()[:60]
        html_text = f"""
        <div class="vw-dialog-kopf"><h3>{esc(bh.betrag_text(bu['betrag']))}</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
        <div class="bh-buchung-info">
            <p><b>{esc(bu['gegenpartei'] or '–')}</b><br><span class="vw-klein">{esc(bh.datum_de(bu['datum']))} · {esc(konto['name'] if konto else '')}{(' · IBAN ' + esc(bu['iban_gegen'])) if bu['iban_gegen'] else ''}</span></p>
            <p class="vw-klein">{esc(bu['zweck'])}</p>
        </div>
        {f'<h4 class="vw-zwischentitel">Zugeordnete Belege</h4><ul class="bh-zugeordnet">{zugeordnet}</ul>' if zugeordnet else ''}
        <h4 class="vw-zwischentitel">Passenden Beleg wählen</h4>
        <form method="get" action="/admin/buchhaltung/buchung" class="vw-inline-form bh-suche" data-bh-suche><input type="hidden" name="id" value="{bu['id']}"><input type="hidden" name="embed" value="1">
            <input name="suche" value="{esc(suche)}" placeholder="Lieferant, Nummer oder Betrag suchen …" class="vw-wachsen"><button class="button" type="submit">Suchen</button></form>
        <form method="post" action="/admin/buchhaltung/buchung/zuordnen" class="vw-formular">
            <input type="hidden" name="buchung_id" value="{bu['id']}"><input type="hidden" name="monat" value="{monat}">
            <div class="bh-kandidaten">{kand_html or '<p class="vw-leer">Kein passender Beleg gefunden. Suche oben nach einem anderen oder lade ihn hoch.</p>'}</div>
            <p class="vw-klein">Mehrere ankreuzen, wenn eine Überweisung mehrere Rechnungen bezahlt.</p>
            <div class="vw-knopfreihe"><button class="primary" type="submit">Ausgewählte zuordnen</button>
                <button type="button" class="button" data-dialog="belegHochladen" data-bh-fuer-buchung="{bu['id']}">Beleg hochladen</button></div>
        </form>
        <h4 class="vw-zwischentitel">Oder: kein Beleg nötig</h4>
        <form method="post" action="/admin/buchhaltung/buchung/kein-beleg" class="vw-formular">
            <input type="hidden" name="buchung_id" value="{bu['id']}"><input type="hidden" name="monat" value="{monat}">
            <label class="vw-schalter bh-schalter"><input type="checkbox" name="immer" value="1" data-bh-immer><span>Immer bei diesem Empfänger (Regel anlegen)</span></label>
            <label class="bh-regel-muster" hidden>Erkennen an<input name="muster" value="{esc(vorschlag_muster)}"></label>
            <div class="vw-knopfreihe"><button class="button" type="submit">Kein Beleg nötig</button></div>
        </form>{self.bh_anfordern_html(bu)}"""
        if self.query_text(query, "embed"):
            return self.send_html(html_text.encode("utf-8"))
        body = f'{self.bh_kopf("bank")}<section class="vw-karte vw-schmal">{html_text}</section>{self.bh_upload_dialog(bu["id"])}<script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>'
        self.admin_seite("Buchung", body)

    def handle_bh_buchung(self, aktion):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        monat = self.form_value(form, "monat").strip()
        zurueck = "/admin/buchhaltung/bank" + (f"?monat={quote_plus(monat)}" if re.match(r"^\d{4}-\d{2}$", monat or "") else "?")
        if aktion == "regel-neu":
            muster = self.form_value(form, "muster").strip()
            if muster:
                con = bh.db()
                con.execute("INSERT INTO regeln (name, muster, erstellt) VALUES (?, ?, ?)", (muster, muster, bh.jetzt_text()))
                con.commit()
                con.close()
                bh.abgleichen()
            return self.redirect(zurueck + "&msg=" + quote_plus("Regel angelegt."))
        if aktion == "regel-loeschen":
            rid = self.form_value(form, "regel_id")
            if rid.isdigit():
                bh.regel_loeschen(int(rid))
            return self.redirect(zurueck + "&msg=" + quote_plus("Regel gelöscht."))
        bid = self.form_value(form, "buchung_id").strip()
        if not bid.isdigit():
            return self.redirect(zurueck)
        if aktion == "zuordnen":
            belege = [int(k[6:]) for k, v in form.items() if k.startswith("beleg_") and k[6:].isdigit() and v]
            if not belege:
                return self.redirect(zurueck + "&error=" + quote_plus("Bitte mindestens einen Beleg ankreuzen."))
            bh.zuordnen(int(bid), belege)
            return self.redirect(zurueck + "&msg=" + quote_plus("Zugeordnet."))
        if aktion == "loesen":
            beleg = self.form_value(form, "beleg_id")
            bh.zuordnung_loesen(int(bid), int(beleg) if beleg.isdigit() else None)
            return self.redirect(zurueck + "&msg=" + quote_plus("Zuordnung gelöst."))
        if aktion == "kein-beleg":
            muster = self.form_value(form, "muster").strip() if self.form_value(form, "immer") else ""
            bh.kein_beleg(int(bid), muster)
            return self.redirect(zurueck + "&msg=" + quote_plus("Als „kein Beleg nötig“ markiert." + (f" Regel „{muster}“ angelegt." if muster else "")))
        self.redirect(zurueck)

    def handle_bh_konto_upload(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        datei = form.get("datei")
        if not isinstance(datei, UploadedFile) or not datei.content:
            return self.redirect("/admin/buchhaltung/bank?error=" + quote_plus("Bitte eine Datei auswählen."))
        konto = (self.form_value(form, "konto_neu").strip() or self.form_value(form, "konto").strip())[:60]
        try:
            ergebnis = bh.kontoauszug_importieren(datei.content, datei.filename or "auszug", konto)
        except Exception as exc:
            return self.redirect("/admin/buchhaltung/bank?error=" + quote_plus(f"Die Datei konnte nicht gelesen werden: {exc}"))
        z = bh.db()
        letzter = z.execute("SELECT MAX(datum) FROM buchungen WHERE import_id=(SELECT MAX(id) FROM importe)").fetchone()[0]
        z.close()
        monat = (letzter or "")[:7] or bh.jetzt().strftime("%Y-%m")
        self.redirect(f"/admin/buchhaltung/bank?monat={monat}&msg=" + quote_plus(f"{ergebnis['neu']} neue Buchungen übernommen, {ergebnis['doppelt']} waren schon da. Abgleich ist gelaufen."))

    # ---------------------------------------------------------- Monatsexport
    def show_bh_export_zip(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        monat = self.bh_monat(query or {})
        daten, name, _z, _e = bh.monatsexport(monat)
        self.send_bytes(daten, "application/zip", name, inline=False)

    def handle_bh_monat(self, aktion):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        monat = self.form_value(form, "monat").strip()
        if not re.match(r"^\d{4}-\d{2}$", monat):
            return self.redirect("/admin/buchhaltung/export")
        if aktion == "abschliessen":
            bh.monat_abschliessen(monat)
            text = f"Monat {monat[5:]}/{monat[:4]} abgeschlossen."
        else:
            bh.monat_wieder_oeffnen(monat)
            text = f"Monat {monat[5:]}/{monat[:4]} ist wieder offen."
        self.redirect(f"/admin/buchhaltung/export?monat={monat}&msg=" + quote_plus(text))

    # ---------------------------------------------------------- Filiale: Beleg fotografieren
    def show_beleg_foto(self, query=None):
        buyer = self.current_buyer_key()
        if not buyer:
            return self.redirect("/login")
        query = query or {}
        location = find_location(buyer)
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        <section class="vw-karte vw-schmal bh-foto">
            <h2>Beleg fotografieren</h2>
            <p>Kassenbon oder Rechnung, die du bar oder privat bezahlt hast? Foto machen, kurz Betrag eintragen, fertig.</p>
            <form method="post" action="/beleg" enctype="multipart/form-data" class="vw-formular">
                <label class="bh-foto-knopf">📷 Foto aufnehmen oder Datei wählen<input type="file" name="foto" accept="image/*,.pdf" capture="environment" required></label>
                <div class="vw-zwei"><label>Betrag (€)<input name="betrag" inputmode="decimal" placeholder="z. B. 23,90"></label>
                <label>Wofür?<input name="notiz" placeholder="z. B. Milch beim Edeka"></label></div>
                <label>Dein Name<input name="name" placeholder="optional"></label>
                <label class="vw-schalter bh-schalter"><input type="checkbox" name="bar" value="1" checked><span>Bar bezahlt</span></label>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Beleg abschicken</button></div>
            </form>
        </section>"""
        self.send_html(page("Beleg fotografieren", body, buyer_key=buyer))

    def handle_beleg_foto(self):
        buyer = self.current_buyer_key()
        if not buyer:
            return self.redirect("/login")
        form = self.read_form()
        location = find_location(buyer)
        ort = location["name"] if location else buyer
        betrag = self.form_value(form, "betrag").strip()
        notiz = " · ".join(x for x in (self.form_value(form, "notiz").strip(), self.form_value(form, "name").strip()) if x)
        wofuer = self.form_value(form, "notiz").strip()
        vorgaben = {"bar": bool(self.form_value(form, "bar")), "notiz": f"Von {ort}: {notiz}"[:300], "lieferant": (wofuer or f"Barbeleg {ort}")[:60]}
        if betrag and bh.cent(betrag) is not None:
            vorgaben["betrag"] = abs(bh.cent(betrag))
        ids = self.bh_upload_verarbeiten(form, quelle=f"Foto {ort}", vorgaben=vorgaben)
        if not ids:
            return self.redirect("/beleg?error=" + quote_plus("Das Foto konnte nicht gespeichert werden. Bitte noch einmal versuchen."))
        self.redirect("/beleg?msg=" + quote_plus("Danke! Der Beleg ist angekommen."))

    # =================================================================
    #   BUCHHALTUNG: Anfordern · Einkauf prüfen
    # =================================================================
    def bh_adresse_raten(self, buchung):
        """Mail-Adresse des Lieferanten aus früheren Rechnungs-Mails ableiten."""
        con = bh.db()
        try:
            woerter = bh._woerter(buchung["gegenpartei"] + " " + buchung["zweck"])
            for b in con.execute("SELECT lieferant, mail_von FROM belege WHERE mail_von != '' ORDER BY id DESC").fetchall():
                if woerter & bh._woerter(b["lieferant"] + " " + b["mail_von"]):
                    adresse = parseaddr(b["mail_von"])[1]
                    if adresse and not re.search(r"no-?reply|donotreply|nicht-antworten", adresse, re.I):
                        return adresse
            for r in con.execute("SELECT * FROM lieferanten WHERE muster LIKE '%@%'").fetchall():
                if woerter & bh._woerter(r["name"]):
                    return r["muster"]
        finally:
            con.close()
        return ""

    def bh_anfordern_text(self, buchung):
        con = bh.db()
        belege_adresse = bh.einstellung(con, "belege_adresse") or ""
        firma = (bh.einstellung(con, "eigene_namen") or "Opa Peters").split(",")[0].strip()
        con.close()
        betreff = f"Rechnung fehlt: Zahlung über {bh.betrag_text(abs(buchung['betrag']))} vom {bh.datum_de(buchung['datum'])}"
        hinweis = (f"Bitte senden Sie uns die Rechnung an {belege_adresse} – gern auch künftig alle Rechnungen direkt an diese Adresse."
                   if belege_adresse else "Bitte senden Sie uns die Rechnung als PDF per E-Mail.")
        text = (f"Guten Tag,\n\nfür die folgende Zahlung liegt uns leider noch keine Rechnung vor:\n\n"
                f"Datum: {bh.datum_de(buchung['datum'])}\nBetrag: {bh.betrag_text(abs(buchung['betrag']))}\n"
                f"Verwendungszweck: {buchung['zweck'] or '–'}\n\n{hinweis}\n\nVielen Dank und freundliche Grüße\n{firma}")
        return betreff, text

    def bh_anfordern_html(self, bu):
        if bu["betrag"] >= 0 or bu["status"] not in ("offen", "vorschlag"):
            return ""
        con = bh.db()
        bisher = con.execute("SELECT * FROM anforderungen WHERE buchung_id=? ORDER BY id DESC", (bu["id"],)).fetchall()
        con.close()
        an = self.bh_adresse_raten(bu)
        betreff, text = self.bh_anfordern_text(bu)
        mailto = f"mailto:{quote(an)}?subject={quote(betreff)}&body={quote(text)}"
        verlauf = "".join(f'<li>{esc(a["zeit"][:16])} an {esc(a["an"])} ({esc(a["weg"])})</li>' for a in bisher)
        return f"""
        <h4 class="vw-zwischentitel">Rechnung beim Lieferanten anfordern</h4>
        {f'<ul class="bh-importe">{verlauf}</ul>' if verlauf else ''}
        <form method="post" action="/admin/buchhaltung/buchung/anfordern" class="vw-formular">
            <input type="hidden" name="buchung_id" value="{bu['id']}"><input type="hidden" name="monat" value="{esc((bu['datum'] or '')[:7])}">
            <label>An<input type="email" name="an" value="{esc(an)}" placeholder="rechnung@lieferant.de" required></label>
            <label>Betreff<input name="betreff" value="{esc(betreff)}"></label>
            <label>Text<textarea name="text" rows="9">{esc(text)}</textarea></label>
            <div class="vw-knopfreihe">
                <button class="primary" type="submit" {'disabled title="E-Mail-Versand ist auf dem Server nicht eingerichtet"' if not smtp_is_configured() else ''}>Anfordern</button>
                <a class="button" href="{esc(mailto)}" data-bh-mailto>Im eigenen Mail-Programm öffnen</a>
            </div>
        </form>"""

    def handle_bh_anfordern(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        bid = self.form_value(form, "buchung_id").strip()
        monat = self.form_value(form, "monat").strip()
        zurueck = f"/admin/buchhaltung/bank?monat={quote_plus(monat)}"
        an = self.form_value(form, "an").strip()
        if not bid.isdigit() or not email_sieht_gueltig_aus(an):
            return self.redirect(zurueck + "&error=" + quote_plus("Bitte eine gültige E-Mail-Adresse eintragen."))
        ok, fehler = mail_senden(an, self.form_value(form, "betreff").strip()[:200], self.form_value(form, "text")[:5000])
        if not ok:
            return self.redirect(zurueck + "&error=" + quote_plus(f"Mail ging nicht raus: {fehler}"))
        con = bh.db()
        con.execute("INSERT INTO anforderungen (buchung_id, an, zeit, weg) VALUES (?, ?, ?, 'Mail')", (int(bid), an, bh.jetzt_text()))
        bh.protokolliere(con, "anforderung", f"Buchung {bid}: Rechnung angefordert bei {an}")
        con.commit()
        con.close()
        self.redirect(zurueck + "&msg=" + quote_plus(f"Rechnung bei {an} angefordert."))

    # ---------------------------------------------------------- Einkauf prüfen
    def bh_einkauf_pruefen(self, beleg):
        """Vergleicht eine Eingangsrechnung mit den Bestellungen beim selben Lieferanten."""
        if beleg["art"] != "ER" or not beleg["lieferant"] or not beleg["datum"]:
            return None
        con = db()
        quellen = [r[0] for r in con.execute("SELECT DISTINCT source FROM order_items WHERE deleted_at IS NULL AND source != ''")]
        con.close()
        passend = [q for q in quellen if bh.gleicher_lieferant(q, beleg["lieferant"])]
        if not passend:
            return None
        try:
            rechnungstag = datetime.strptime(beleg["datum"], "%Y-%m-%d")
        except ValueError:
            return None
        von, bis = rechnungstag - timedelta(days=21), rechnungstag + timedelta(days=3)
        con = db()
        alle = con.execute("SELECT id, location, created_at FROM orders").fetchall()
        con.close()
        orders = [o for o in alle if von <= parse_order_created_at(o["created_at"]) <= bis]
        bestellt = {}
        for item in bestell_positionen([o["id"] for o in orders], nur_offene=False):
            if item["source"] not in passend:
                continue
            key = (item["product_name"] or "", item["package_size"] or "")
            e = bestellt.setdefault(key, {"name": key[0], "gebinde": key[1], "menge": 0, "orte": {}})
            e["menge"] += int(item["quantity"] or 0)
            ort = next((o["location"] for o in orders if o["id"] == item["order_id"]), "")
            e["orte"][ort] = e["orte"].get(ort, 0) + int(item["quantity"] or 0)
        posten, herkunft = bh.positionen(beleg["id"])
        preise = {(bh.schluessel_text(a["artikel"])): a for a in bh.preisentwicklung(beleg["lieferant"]) if a["beleg_id"] == beleg["id"]}
        # bestes Paar je Bestellposition suchen (gierig nach Ähnlichkeit)
        paare = sorted(((bh.namens_aehnlichkeit(b["name"] + " " + b["gebinde"], p["name"]), bi, pi)
                        for bi, b in enumerate(bestellt.values()) for pi, p in enumerate(posten)), reverse=True)
        b_frei, p_frei, zuordnung = set(range(len(bestellt))), set(range(len(posten))), {}
        for wert, bi, pi in paare:
            if wert < 0.5:
                break
            if bi in b_frei and pi in p_frei:
                zuordnung[bi] = pi
                b_frei.discard(bi)
                p_frei.discard(pi)
        zeilen = []
        liste_b = list(bestellt.values())
        for bi, b in enumerate(liste_b):
            p = posten[zuordnung[bi]] if bi in zuordnung else None
            if p is None:
                status = "fehlt" if posten else "unbekannt"
            elif abs((p["menge"] or 0) - b["menge"]) < 0.01:
                status = "ok"
            else:
                status = "menge"
            preis = preise.get(bh.schluessel_text(p["name"])) if p else None
            zeilen.append({"bestellt": b, "position": p, "status": status, "preis": preis})
        extra = [posten[pi] for pi in sorted(p_frei)]
        abweichungen = sum(1 for z in zeilen if z["status"] in ("fehlt", "menge")) + len(extra)
        erhoehungen = [a for a in preise.values() if a["prozent"] > 0]
        return {"quelle": passend[0], "orders": orders, "zeilen": zeilen, "extra": extra, "herkunft": herkunft,
                "posten": posten, "abweichungen": abweichungen, "erhoehungen": erhoehungen, "bestellt": bool(bestellt)}

    def bh_pruef_marke(self, ergebnis):
        if ergebnis is None:
            return ""
        if not ergebnis["posten"]:
            return '<span class="vw-marke grau">Positionen nicht lesbar</span>'
        if not ergebnis["bestellt"]:
            return '<span class="vw-marke grau">keine Bestellung gefunden</span>'
        teile = []
        if ergebnis["abweichungen"]:
            teile.append(f'<span class="vw-marke rot">⚠ {ergebnis["abweichungen"]} Abweichung(en)</span>')
        else:
            teile.append('<span class="vw-marke gruen">✓ passt zur Bestellung</span>')
        if ergebnis["erhoehungen"]:
            teile.append(f'<span class="vw-marke honig">↑ {len(ergebnis["erhoehungen"])} Preis(e) gestiegen</span>')
        return " ".join(teile)

    def bh_pruef_tabelle(self, beleg, ergebnis):
        if ergebnis is None:
            return ""
        if not ergebnis["bestellt"]:
            return f'<p class="vw-klein">Im Bestellsystem gibt es rund um den {esc(bh.datum_de(beleg["datum"]))} keine Bestellungen bei {esc(ergebnis["quelle"])}.</p>'
        zeilen = []
        for z in ergebnis["zeilen"]:
            b, p = z["bestellt"], z["position"]
            marke = {"ok": '<span class="vw-marke gruen">✓</span>', "menge": '<span class="vw-marke rot">Menge anders</span>',
                     "fehlt": '<span class="vw-marke rot">nicht berechnet</span>', "unbekannt": '<span class="vw-marke grau">?</span>'}[z["status"]]
            preis = ""
            if z["preis"]:
                a = z["preis"]
                preis = f'<small class="vw-klein-block {"minus" if a["prozent"] > 0 else "plus"}">{"↑" if a["prozent"] > 0 else "↓"} {str(a["prozent"]).replace(".", ",")} % (vorher {esc(bh.betrag_text(a["alt"]))})</small>'
            orte = ", ".join(f"{o} {m}" for o, m in b["orte"].items())
            zeilen.append(
                f'<tr><td><b>{esc(b["name"])}</b><small class="vw-klein-block">{esc(b["gebinde"])} · {esc(orte)}</small></td>'
                f'<td class="zahl">{b["menge"]}</td>'
                f'<td>{esc(p["name"]) if p else "–"}</td>'
                f'<td class="zahl">{(("%g" % p["menge"]) + " " + esc(p["einheit"] or "")) if p else "–"}</td>'
                f'<td class="zahl">{esc(bh.betrag_text(p["einzelpreis"])) if p and p["einzelpreis"] is not None else "–"}{preis}</td>'
                f'<td>{marke}</td></tr>'
            )
        for p in ergebnis["extra"]:
            zeilen.append(
                f'<tr class="bh-extra"><td>–</td><td class="zahl">–</td><td><b>{esc(p["name"])}</b></td>'
                f'<td class="zahl">{"%g" % (p["menge"] or 0)} {esc(p["einheit"] or "")}</td><td class="zahl">{esc(bh.betrag_text(p["einzelpreis"]))}</td>'
                f'<td><span class="vw-marke rot">nicht bestellt</span></td></tr>'
            )
        hinweis = ""
        if ergebnis["posten"] and ergebnis["herkunft"] == "PDF-Text":
            hinweis = '<p class="vw-klein">Positionen aus dem PDF-Text gelesen – bei ungewöhnlichem Rechnungslayout kann eine Zeile fehlen. E-Rechnungen (ZUGFeRD/XRechnung) werden genau gelesen.</p>'
        elif not ergebnis["posten"]:
            hinweis = '<p class="vw-klein">Die Positionen dieser Rechnung konnten nicht gelesen werden (z. B. Scan oder ungewöhnliches Layout). Die Bestellung steht trotzdem zum Vergleich da.</p>'
        return f"""
        <p class="vw-klein">Verglichen mit Bestellungen bei <b>{esc(ergebnis['quelle'])}</b> vom {esc(bh.datum_de((datetime.strptime(beleg['datum'], '%Y-%m-%d') - timedelta(days=21)).date().isoformat()))} bis zum Rechnungsdatum.</p>
        <div class="vw-tabelle-wrap"><table class="vw-tabelle bh-pruef">
            <tr><th>Bestellt</th><th class="zahl">Menge</th><th>Auf der Rechnung</th><th class="zahl">Menge</th><th class="zahl">Einzelpreis</th><th></th></tr>
            {''.join(zeilen)}
        </table></div>{hinweis}"""

    def show_bh_einkauf(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        tage = 120
        grenze = (bh.jetzt() - timedelta(days=tage)).date().isoformat()
        con = bh.db()
        belege = con.execute("SELECT * FROM belege WHERE art='ER' AND status NOT IN ('ignoriert','doppelt','portal') AND datum >= ? ORDER BY datum DESC", (grenze,)).fetchall()
        con.close()
        karten = []
        for b in belege:
            ergebnis = self.bh_einkauf_pruefen(b)
            if ergebnis is None:
                continue
            karten.append(f"""
            <details class="vw-karte bh-pruef-karte" id="e-{b['id']}" {'open' if ergebnis['abweichungen'] or ergebnis['erhoehungen'] else ''}>
                <summary><span><b>{esc(b['lieferant'])}</b> · {esc(bh.datum_de(b['datum']))} · {esc(b['rechnungsnr'] or 'ohne Nr.')} · {esc(bh.betrag_text(b['betrag']))}</span><span>{self.bh_pruef_marke(ergebnis)}</span></summary>
                {self.bh_pruef_tabelle(b, ergebnis)}
                <p class="vw-klein"><a href="/admin/buchhaltung/datei?id={b['id']}" target="_blank" rel="noopener">Rechnung ansehen</a></p>
            </details>""")
        erhoehungen = [a for a in bh.preisentwicklung() if a["prozent"] > 0][:30]
        preis_zeilen = "".join(
            f'<tr><td>{esc(bh.datum_de(a["datum_neu"]))}</td><td>{esc(a["lieferant"])}</td><td><b>{esc(a["artikel"])}</b></td>'
            f'<td class="zahl">{esc(bh.betrag_text(a["alt"]))}</td><td class="zahl"><b>{esc(bh.betrag_text(a["neu"]))}</b></td><td class="zahl minus">+{str(a["prozent"]).replace(".", ",")} %</td></tr>'
            for a in erhoehungen
        )
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.bh_kopf('einkauf', 'Stimmt die Rechnung mit dem überein, was bestellt wurde? Und sind Preise gestiegen?')}
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>Preiserhöhungen</h3><span class="vw-zahl {'rot' if erhoehungen else ''}">{len(erhoehungen)}</span></div>
            {f'<div class="vw-tabelle-wrap vw-ohne-rand"><table class="vw-tabelle"><tr><th>Seit</th><th>Lieferant</th><th>Artikel</th><th class="zahl">vorher</th><th class="zahl">jetzt</th><th class="zahl">Plus</th></tr>{preis_zeilen}</table></div>' if erhoehungen else '<p class="vw-leer">Keine Preiserhöhungen gefunden. Preise werden erkannt, sobald von einem Lieferanten mindestens zwei Rechnungen mit lesbaren Positionen da sind.</p>'}
        </section>
        <h3 class="vw-tag">Rechnungen der letzten {tage} Tage von Lieferanten aus dem Bestellsystem</h3>
        {''.join(karten) or '<p class="vw-leer">Noch keine Rechnungen von Lieferanten, bei denen über das Bestellsystem bestellt wird. Der Abgleich klappt, wenn der Lieferantenname auf der Rechnung zur „Bezugsquelle“ im Sortiment passt.</p>'}
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Einkauf prüfen", body)


    # =================================================================
    #   KASSENABSCHLUSS (Standort) & TAGESABSCHLUSS (Admin)
    # =================================================================
    def show_kasse(self, query=None):
        buyer = self.current_buyer_key()
        if not buyer:
            return self.redirect("/login")
        query = query or {}
        location = find_location(buyer)
        if not location or not kasse_einstellungen()["team_zaehlt"]:
            return self.redirect("/cockpit")
        heute = berlin_now().date().isoformat()
        _ls, ab = kasse_tag(location["id"], heute)
        ab = ab or {}
        wechsel = int(kasse_einstellungen()["wechselgeld"].get(location["id"], 0) or 0)

        def feld(name, label, hilfe="", pflicht=False):
            wert = ab.get(name)
            text = "" if wert is None else f"{wert / 100:.2f}".replace(".", ",")
            return (f'<label>{label}<input name="{name}" inputmode="decimal" value="{esc(text)}" placeholder="0,00" {"required" if pflicht else ""}>'
                    + (f'<small class="vw-klein-block">{hilfe}</small>' if hilfe else "") + "</label>")
        ergebnis = ""
        if ab:
            diff = kasse_differenz(location["id"], None, ab)
            if diff is not None:
                ok = abs(diff) < kasse_einstellungen()["schwelle"]
                ergebnis = f'<div class="vw-status {"ok" if ok else "warn"}">Kassendifferenz: <b>{esc(euro_text(diff, True))}</b>{" – passt." if ok else " – bitte noch einmal nachzählen."}</div>'
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        <section class="vw-karte vw-schmal kasse-form">
            <h2>Kassenabschluss {esc(location['name'])}</h2>
            <p>Am Ende des Tages: Zahlen vom Kassenabschluss (Z-Bon) abtippen und das Bargeld zählen. Du kannst es bis Mitternacht beliebig oft korrigieren.</p>
            {ergebnis}
            <form method="post" action="/kasse" enctype="multipart/form-data" class="vw-formular">
                <div class="vw-zwei">{feld('umsatz', 'Umsatz gesamt laut Z-Bon (€)')}{feld('bar', 'Davon bar laut Z-Bon (€)')}</div>
                {feld('gezaehlt', 'Bargeld in der Kasse – gezählt (€)', f'Alles Geld in der Kasse zählen{", inklusive " + euro_text(wechsel) + " Wechselgeld" if wechsel else ""}.', True)}
                <div class="vw-zwei">{feld('entnommen', 'Bar entnommen (€)', 'z. B. Einkauf bezahlt – sonst leer lassen')}
                    <label>Wofür entnommen?<input name="entnahme_zweck" value="{esc(ab.get('entnahme_zweck', ''))}" placeholder="z. B. Milch Edeka"></label></div>
                <label>Besonderheiten heute<textarea name="notiz" rows="3" placeholder="z. B. Vitrine 2 kühlt schlecht, Becher fast leer, viel los wegen Stadtfest">{esc(ab.get('notiz', ''))}</textarea></label>
                <div class="vw-zwei"><label>Dein Name<input name="name" value="{esc(ab.get('name', ''))}" required></label>
                    <label class="bh-foto-knopf">📷 Foto vom Z-Bon (optional)<input type="file" name="foto" accept="image/*,.pdf" capture="environment"></label></div>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Kassenabschluss speichern</button></div>
            </form>
            <p class="vw-klein">Tipp: Wenn du bar etwas eingekauft hast, fotografiere den Bon zusätzlich unter <a href="/beleg">📷 Beleg fotografieren</a>.</p>
        </section>"""
        self.send_html(page("Kassenabschluss", body, buyer_key=buyer))

    def handle_kasse(self):
        buyer = self.current_buyer_key()
        if not buyer:
            return self.redirect("/login")
        location = find_location(buyer)
        if not location:
            return self.redirect("/")
        form = self.read_form()
        heute = berlin_now().date().isoformat()
        werte = {}
        for name in ("umsatz", "bar", "gezaehlt", "entnommen"):
            roh = self.form_value(form, name).strip()
            werte[name] = bh.cent(roh) if roh else None
            if roh and werte[name] is None:
                return self.redirect("/kasse?error=" + quote_plus(f"Bitte bei „{name}“ nur einen Betrag eintragen, z. B. 123,45."))
        if werte["gezaehlt"] is None:
            return self.redirect("/kasse?error=" + quote_plus("Bitte das gezählte Bargeld eintragen."))
        foto = ""
        datei = form.get("foto") if isinstance(form, dict) else None
        datei = datei[0] if isinstance(datei, list) else datei
        if datei is not None and getattr(datei, "filename", "") and getattr(datei, "content", b""):
            endung = os.path.splitext(datei.filename)[1].lower()
            if endung in (".jpg", ".jpeg", ".png", ".heic", ".webp", ".pdf") and len(datei.content) < 15 * 1024 * 1024:
                foto = f"{heute}_{re.sub(r'[^a-z0-9]+', '-', location['id'].lower())}_{uuid.uuid4().hex[:6]}{endung}"
                with open(os.path.join(KASSE_FOTO_DIR, foto), "wb") as f:
                    f.write(datei.content)
        con = db()
        alt = con.execute("SELECT foto FROM kasse_abschluss WHERE location_id=? AND datum=?", (location["id"], heute)).fetchone()
        con.execute(
            """INSERT INTO kasse_abschluss (location_id, datum, umsatz, bar, karte, gezaehlt, entnommen, entnahme_zweck, notiz, name, foto, erstellt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(location_id, datum) DO UPDATE SET umsatz=excluded.umsatz, bar=excluded.bar, karte=excluded.karte, gezaehlt=excluded.gezaehlt,
                 entnommen=excluded.entnommen, entnahme_zweck=excluded.entnahme_zweck, notiz=excluded.notiz, name=excluded.name,
                 foto=excluded.foto, erstellt=excluded.erstellt""",
            (location["id"], heute, werte["umsatz"], werte["bar"],
             (werte["umsatz"] - werte["bar"]) if werte["umsatz"] is not None and werte["bar"] is not None else None,
             werte["gezaehlt"], werte["entnommen"] or 0, self.form_value(form, "entnahme_zweck").strip()[:120],
             self.form_value(form, "notiz").strip()[:1000], self.form_value(form, "name").strip()[:60],
             foto or (alt["foto"] if alt else ""), berlin_now().strftime("%Y-%m-%d %H:%M")))
        con.commit()
        con.close()
        self.redirect("/kasse?msg=" + quote_plus("Danke! Der Kassenabschluss ist gespeichert."))

    def show_kasse_foto(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        name = os.path.basename(self.query_text(query or {}, "datei"))
        pfad = os.path.join(KASSE_FOTO_DIR, name)
        if not name or not os.path.exists(pfad):
            return self.send_html(b"Nicht gefunden", 404)
        typ = {".pdf": "application/pdf", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic"}.get(os.path.splitext(name)[1].lower(), "image/jpeg")
        with open(pfad, "rb") as f:
            self.send_bytes(f.read(), typ)

    # ---------------------------------------------------------- Admin: Tagesabschluss
    def show_tagesabschluss(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        datum = self.query_text(query, "datum")
        heute = berlin_now().date()
        try:
            tag = datetime.strptime(datum, "%Y-%m-%d").date()
        except ValueError:
            tag = heute if berlin_now().hour >= 12 else heute - timedelta(days=1)
        datum = tag.isoformat()
        daten = tagesabschluss_daten(datum)
        einst = kasse_einstellungen()
        vorher, nachher = (tag - timedelta(days=1)).isoformat(), (tag + timedelta(days=1)).isoformat()
        leiste = (f'<div class="vw-monat"><a class="button vw-rund" href="/admin/tagesabschluss?datum={vorher}" aria-label="Tag davor">‹</a>'
                  f'<b>{WOCHENTAGE[tag.weekday()]}, {tag.strftime("%d.%m.%Y")}</b>'
                  + (f'<a class="button vw-rund" href="/admin/tagesabschluss?datum={nachher}" aria-label="Tag danach">›</a>' if tag < heute else '<span class="vw-rund"></span>')
                  + "</div>")
        hinweise = "".join(f'<div class="ta-hinweis {f}">{esc(t)}</div>' for f, t in daten["hinweise"]) or '<p class="vw-leer">Keine Auffälligkeiten. 👍</p>'
        vergleich = ""
        if daten["vergleich"] is not None:
            vergleich = f'<span class="ta-vergleich {"plus" if daten["vergleich"] >= 0 else "minus"}">{daten["vergleich"]:+d} % zur Vorwoche</span>'
        karten = []
        for s in daten["standorte"]:
            loc, ls, ab = s["loc"], s["ls"], s["ab"]
            quelle = "Lightspeed" if ls and ls.get("umsatz") is not None else ("Eingabe Team" if ab and ab.get("umsatz") is not None else "")
            bon_schnitt = round(s["umsatz"] / s["bons"]) if s["umsatz"] and s["bons"] else None
            zahlen = [("Umsatz", euro_text(s["umsatz"])), ("Bar", euro_text(s["bar"])), ("Karte", euro_text(s["karte"]))]
            if s["bons"]:
                zahlen += [("Bons", str(s["bons"])), ("Ø Bon", euro_text(bon_schnitt))]
            if s["vorwoche"] is not None:
                zahlen.append(("Vorwoche", euro_text(s["vorwoche"])))
            if s["schnitt"] is not None:
                zahlen.append(("Ø 4 Wochen", euro_text(s["schnitt"])))
            if s["storno"]:
                zahlen.append(("Stornos", euro_text(abs(s["storno"]))))
            if s["rabatt"]:
                zahlen.append(("Rabatte", euro_text(abs(s["rabatt"]))))
            zahlen_html = "".join(f'<div class="ta-zahl"><small>{esc(k)}</small><b>{esc(v)}</b></div>' for k, v in zahlen)
            kasse_html = '<p class="vw-klein">Kein Kassenabschluss eingetragen.</p>' if einst["team_zaehlt"] else ""
            if ls and (ls.get("kassendiff") is not None or ls.get("gezaehlt") is not None):
                kasse_html = (f'<p class="vw-klein">Kassenzählung in Lightspeed: gezählt <b>{esc(euro_text(ls.get("gezaehlt")))}</b>'
                              + (f' · Differenz <b class="{"minus" if abs(ls["kassendiff"]) >= einst["schwelle"] else ""}">{esc(euro_text(ls["kassendiff"], True))}</b>' if ls.get("kassendiff") is not None else "") + "</p>")
            elif ab:
                diff = s["diff"]
                kasse_html = (
                    f'<p class="vw-klein">Gezählt: <b>{esc(euro_text(ab.get("gezaehlt")))}</b>'
                    + (f' · entnommen {esc(euro_text(ab.get("entnommen")))} ({esc(ab.get("entnahme_zweck") or "ohne Angabe")})' if ab.get("entnommen") else "")
                    + (f' · Wechselgeld {esc(euro_text(int(einst["wechselgeld"].get(loc["id"], 0) or 0)))}' if einst["wechselgeld"].get(loc["id"]) else "")
                    + (f' · Differenz <b class="{"minus" if diff and abs(diff) >= einst["schwelle"] else ""}">{esc(euro_text(diff, True))}</b>' if diff is not None else "")
                    + f' · von {esc(ab.get("name") or "?")}, {esc((ab.get("erstellt") or "")[11:16])} Uhr'
                    + (f' · <a href="/admin/kasse-foto?datei={quote_plus(ab["foto"])}" target="_blank" rel="noopener">Z-Bon ansehen</a>' if ab.get("foto") else "")
                    + "</p>"
                    + (f'<p class="ta-notiz">„{esc(ab["notiz"])}“</p>' if ab.get("notiz") else "")
                )
            team_html = "".join(
                f'<div class="vw-zeile"><span><b>{esc(z["employee_name"])}</b><small>{esc(z["start_time"])}–{esc(z["end_time"])}</small></span>'
                f'<span class="vw-zeit">{esc(stunden_text(int(z["duration_minutes"] or 0)))}</span></div>'
                + (f'<p class="ta-notiz">„{esc(z["note"].strip())}“</p>' if (z["note"] or "").strip() else "")
                for z in s["team"]
            ) or '<p class="vw-leer">Keine Zeiten eingetragen.</p>'
            team_kopf = ("Team · " + esc(stunden_text(s["minuten"]))) if s["minuten"] else "Team"; team_kopf += "" + (f' · {esc(euro_kurz(s["pro_stunde"]))} pro Stunde' if s["pro_stunde"] else "")
            aufgaben_html = ""
            if s["aufgaben"]:
                fertig, gesamt, offen = s["aufgaben"]
                aufgaben_html = (f'<h4 class="vw-zwischentitel">Tagesaufgaben {fertig}/{gesamt}</h4>'
                                 + ("".join(f'<div class="ta-offen">○ {esc(t)}</div>' for t in offen) or '<p class="vw-klein">Alles erledigt ✓</p>'))
            karten.append(f"""
            <section class="vw-karte ta-standort">
                <div class="vw-karte-kopf"><h3>{esc(loc['name'])}</h3>{f'<span class="vw-marke grau">{quelle}</span>' if quelle else ''}</div>
                <div class="ta-zahlen">{zahlen_html}</div>
                {f'<h4 class="vw-zwischentitel">Kassenabschluss</h4>{kasse_html}' if kasse_html else ''}
                <h4 class="vw-zwischentitel">{team_kopf}</h4><div class="vw-liste">{team_html}</div>
                {aufgaben_html}
            </section>""")
        con = db()
        berichte = con.execute("SELECT * FROM kasse_berichte ORDER BY id DESC LIMIT 8").fetchall()
        con.close()
        berichte_html = "".join(
            f'<li><b>{esc(b["empfangen"])}</b> · {esc(b["dateiname"])} – <span class="{"" if b["status"] == "ok" else "minus"}">{esc(b["meldung"])}</span></li>'
            for b in berichte) or "<li>Noch kein Lightspeed-Bericht angekommen.</li>"
        alle_orte = get_locations()
        erlaubt = einst["standorte"]
        orte_html = "".join(
            f"""<div class="ta-ort-einst"><label class="vw-schalter bh-schalter"><input type="checkbox" name="standort_{esc(l['id'])}" value="1" {'checked' if erlaubt is None or l['id'] in erlaubt else ''}><span><b>{esc(l['name'])}</b> hat eine Kasse</span></label>
                <div class="vw-zwei"><label>Name in Lightspeed<input name="lsname_{esc(l['id'])}" value="{esc(einst['lightspeed_namen'].get(l['id'], ''))}" placeholder="{esc(l['name'])}"></label>
                <label>Wechselgeld in der Kasse (€)<input name="wechsel_{esc(l['id'])}" inputmode="decimal" value="{esc(f"{int(einst['wechselgeld'].get(l['id'], 0) or 0) / 100:.2f}".replace('.', ','))}"></label></div></div>"""
            for l in alle_orte)
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {admin_kopf('Tagesabschluss', ('Kasse, Team und Auffälligkeiten – kommt morgens aufs Handy, sobald der Lightspeed-Bericht da ist.' if einst['zeitpunkt'] == 'morgens' else f"Kasse, Team und Auffälligkeiten – kommt um {einst['uhrzeit']} Uhr aufs Handy."),
                    '<button type="button" class="button" data-dialog="taEinst">Einstellungen</button>')}
        {leiste}
        <section class="vw-karte ta-gesamt">
            <div><small>Umsatz alle Standorte</small><b>{esc(euro_text(daten['summe']))}</b>{vergleich}</div>
            <div class="ta-hinweise">{hinweise}</div>
        </section>
        <div class="vw-raster-2">{''.join(karten) or '<p class="vw-leer">Unter Einstellungen festlegen, welche Standorte eine Kasse haben.</p>'}</div>
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>Lightspeed-Berichte</h3></div>
            <p class="vw-klein">Lightspeed schickt den Tagesbericht morgens per Mail. Kommt er an ein Postfach, das unter Buchhaltung → Quellen verbunden ist, wird er automatisch eingelesen. Hier kannst du einen Bericht auch von Hand hochladen.</p>
            <ul class="bh-importe">{berichte_html}</ul>
            <form method="post" action="/admin/tagesabschluss/bericht" enctype="multipart/form-data" class="vw-knopfreihe">
                <input type="file" name="datei" accept=".csv,.xls,.xlsx,.pdf" required><button class="button" type="submit">Bericht hochladen</button></form>
        </section>
        <dialog class="vw-dialog" id="taEinst">
            <div class="vw-dialog-kopf"><h3>Tagesabschluss einstellen</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/tagesabschluss/einstellungen" class="vw-formular">
                <label class="vw-schalter bh-schalter"><input type="checkbox" name="aktiv" value="1" {'checked' if einst['aktiv'] else ''}><span>Tagesabschluss aufs Handy schicken</span></label>
                <div class="vw-zwei"><label>Wann?<select name="zeitpunkt">
                    <option value="morgens" {'selected' if einst['zeitpunkt'] == 'morgens' else ''}>Morgens, sobald der Lightspeed-Bericht von gestern da ist</option>
                    <option value="abends" {'selected' if einst['zeitpunkt'] == 'abends' else ''}>Abends zur Uhrzeit (braucht Lightspeed-Schnittstelle oder Team-Zählung)</option></select></label>
                    <label>Uhrzeit (nur abends)<input type="time" name="uhrzeit" value="{esc(einst['uhrzeit'])}"></label></div>
                <label class="vw-schalter bh-schalter"><input type="checkbox" name="team_zaehlt" value="1" {'checked' if einst['team_zaehlt'] else ''}><span>Team trägt abends einen Kassenabschluss im Bestellsystem ein (sonst nur Lightspeed)</span></label>
                <div class="vw-zwei"><label>Zusätzlich per Mail an (optional)<input type="email" name="mail_an" value="{esc(einst['mail_an'])}"></label>
                    <label>Kassendifferenz melden ab (€)<input name="schwelle" inputmode="decimal" value="{esc(f"{einst['schwelle'] / 100:.2f}".replace('.', ','))}"></label></div>
                <label>Absender der Lightspeed-Mails (optional)<input name="absender" value="{esc(einst['absender'])}" placeholder="wird automatisch erkannt, wenn „lightspeed“ im Absender steht"></label>
                <h4 class="vw-zwischentitel">Standorte</h4>{orte_html}
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
            </form>
            <form method="post" action="/admin/tagesabschluss/senden" class="vw-knopfreihe"><input type="hidden" name="datum" value="{esc(datum)}">
                <button class="vw-leise" type="submit">Diesen Tagesabschluss jetzt als Test aufs Handy schicken</button></form>
        </dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Tagesabschluss", body)

    def handle_tagesabschluss(self, aktion):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        if aktion == "einstellungen":
            neu = {
                "aktiv": bool(self.form_value(form, "aktiv")),
                "zeitpunkt": "abends" if self.form_value(form, "zeitpunkt") == "abends" else "morgens",
                "team_zaehlt": bool(self.form_value(form, "team_zaehlt")),
                "uhrzeit": self.form_value(form, "uhrzeit") if re.match(r"^\d{2}:\d{2}$", self.form_value(form, "uhrzeit")) else "22:30",
                "mail_an": self.form_value(form, "mail_an").strip() if email_sieht_gueltig_aus(self.form_value(form, "mail_an")) else "",
                "schwelle": abs(bh.cent(self.form_value(form, "schwelle")) or 500),
                "absender": self.form_value(form, "absender").strip()[:120],
                "standorte": [l["id"] for l in get_locations() if self.form_value(form, f"standort_{l['id']}")],
                "lightspeed_namen": {l["id"]: self.form_value(form, f"lsname_{l['id']}").strip()[:80] for l in get_locations() if self.form_value(form, f"lsname_{l['id']}").strip()},
                "wechselgeld": {l["id"]: abs(bh.cent(self.form_value(form, f"wechsel_{l['id']}")) or 0) for l in get_locations()},
            }
            kasse_einstellungen_speichern(neu)
            return self.redirect("/admin/tagesabschluss?msg=" + quote_plus("Gespeichert."))
        if aktion == "senden":
            datum = self.form_value(form, "datum")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", datum or ""):
                datum = berlin_now().date().isoformat()
            if not push_is_configured():
                return self.redirect(f"/admin/tagesabschluss?datum={datum}&error=" + quote_plus("Push ist auf dem Server noch nicht eingerichtet."))
            ergebnis = tagesabschluss_senden(datum)
            return self.redirect(f"/admin/tagesabschluss?datum={datum}&msg=" + quote_plus(f"An {ergebnis.get('sent', 0)} Gerät(e) geschickt."))
        if aktion == "bericht":
            datei = form.get("datei") if isinstance(form, dict) else None
            datei = datei[0] if isinstance(datei, list) else datei
            if datei is None or not getattr(datei, "content", b""):
                return self.redirect("/admin/tagesabschluss?error=" + quote_plus("Bitte eine Datei auswählen."))
            gespeichert, meldung = kassenbericht_importieren(datei.content, datei.filename or "bericht.csv", "Von Hand hochgeladen",
                                                             (berlin_now().date() - timedelta(days=1)).isoformat())
            ziel = f"/admin/tagesabschluss?datum={gespeichert[-1][1]}&" if gespeichert else "/admin/tagesabschluss?"
            return self.redirect(ziel + ("msg=" if gespeichert else "error=") + quote_plus(meldung))
        self.redirect("/admin/tagesabschluss")

    # =================================================================
    #   BUCHHALTUNG (neu): Übersicht · Bank · Belege · Lieferanten · Export · Einstellungen
    # =================================================================
    def bh_kopf(self, aktiv, unter="", aktionen="", monat=None):
        m = f"?monat={monat}" if monat else ""
        reiter = [
            ("uebersicht", "Übersicht", f"/admin/buchhaltung{m}"),
            ("bank", "Bank", f"/admin/buchhaltung/bank{m}"),
            ("belege", "Belege", f"/admin/buchhaltung/belege{m}"),
            ("lieferanten", "Lieferanten", f"/admin/buchhaltung/lieferanten{m}"),
            ("export", "Export", f"/admin/buchhaltung/export{m}"),
        ]
        zahnrad = ('<a class="button bh-zahnrad" href="/admin/buchhaltung/einstellungen" aria-label="Einstellungen und Postfächer" title="Postfächer und Einstellungen">'
                   '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg></a>')
        leiste = self.bh_monat_leiste(self.path.split("?")[0], monat) if monat else ""
        return admin_kopf("Buchhaltung", unter, f'<div class="bh-kopf-rechts">{leiste}{aktionen}{zahnrad}</div>', reiter, aktiv)

    def bh_vormonat(self, monat):
        j, m = int(monat[:4]), int(monat[5:])
        return f"{j - (m == 1)}-{12 if m == 1 else m - 1:02d}"

    def bh_summen(self, monat):
        con = bh.db()
        r = con.execute("SELECT COALESCE(SUM(CASE WHEN betrag<0 THEN -betrag END),0) aus, COALESCE(SUM(CASE WHEN betrag>0 THEN betrag END),0) ein FROM buchungen WHERE substr(datum,1,7)=?", (monat,)).fetchone()
        con.close()
        return r["aus"], r["ein"]

    def bh_veraenderung(self, jetzt_wert, vorher, mehr_ist_gut):
        if not vorher or not jetzt_wert:
            return '<span class="bh-trend">noch kein Vergleich</span>'
        p = round((jetzt_wert / vorher - 1) * 100)
        gut = (p >= 0) == mehr_ist_gut
        return f'<span class="bh-trend {"gut" if gut else "schlecht"}">{p:+d} % zum Vormonat</span>'

    def bh_pruefpunkte(self, monat):
        """Die Liste „Das solltest du dir ansehen“: (farbe, titel, unterzeile, link, knopf)"""
        punkte = []
        con = bh.db()
        offen = con.execute("SELECT * FROM buchungen WHERE substr(datum,1,7)=? AND status='offen' ORDER BY ABS(betrag) DESC", (monat,)).fetchall()
        vorschlag = con.execute("SELECT COUNT(*) FROM buchungen WHERE substr(datum,1,7)=? AND status='vorschlag'", (monat,)).fetchone()[0]
        unsicher = con.execute("SELECT * FROM belege WHERE status='unsicher' ORDER BY id DESC").fetchall()
        portal = con.execute("SELECT COUNT(*) FROM belege WHERE status='portal'").fetchone()[0]
        neu = con.execute("SELECT COUNT(*) FROM belege WHERE status='neu' AND sicher=0 AND substr(datum,1,7)=?", (monat,)).fetchone()[0]
        konten = con.execute("SELECT k.name, MAX(b.datum) bis FROM konten k LEFT JOIN buchungen b ON b.konto_id=k.id GROUP BY k.id").fetchall()
        # Ausgaben je Gegenpartei: dieser Monat gegen die 3 Monate davor
        m1 = self.bh_vormonat(monat)
        m2 = self.bh_vormonat(m1)
        m3 = self.bh_vormonat(m2)
        summen = {}
        for r in con.execute("SELECT gegenpartei, substr(datum,1,7) m, -SUM(betrag) s FROM buchungen WHERE betrag<0 AND substr(datum,1,7) IN (?,?,?,?) GROUP BY gegenpartei, m", (monat, m1, m2, m3)):
            summen.setdefault(bh.schluessel_text(r["gegenpartei"]), {"name": r["gegenpartei"], "m": {}})["m"][r["m"]] = r["s"]
        frueher = {bh.schluessel_text(r[0]) for r in con.execute("SELECT DISTINCT gegenpartei FROM buchungen WHERE substr(datum,1,7) < ?", (monat,))}
        hat_vormonate = con.execute("SELECT 1 FROM buchungen WHERE substr(datum,1,7) < ? LIMIT 1", (monat,)).fetchone()
        neue = con.execute("SELECT gegenpartei, MIN(datum) d, SUM(betrag) s FROM buchungen WHERE substr(datum,1,7)=? AND betrag<0 GROUP BY gegenpartei", (monat,)).fetchall()
        con.close()
        if offen:
            namen = " · ".join(f"{b['gegenpartei'] or 'Unbekannt'} {bh.betrag_text(abs(b['betrag']))}" for b in offen[:3]) + (" …" if len(offen) > 3 else "")
            punkte.append(("rot", f"{len(offen)} Zahlung(en) ohne Beleg", namen, f"/admin/buchhaltung/bank?monat={monat}&filter=offen", "Belege nachreichen", True))
        if vorschlag:
            punkte.append(("honig", f"{vorschlag} Vorschlag/Vorschläge kurz bestätigen", "Das System hat einen passenden Beleg gefunden, ist sich aber nicht ganz sicher.", f"/admin/buchhaltung/bank?monat={monat}&filter=vorschlag", "Bestätigen", False))
        if unsicher:
            namen = " · ".join(f"„{(b['original_name'] or b['lieferant'] or 'Dokument')[:40]}“" for b in unsicher[:2])
            punkte.append(("honig", f"{len(unsicher)} Dokument(e) – ist das eine Rechnung?", namen, "/admin/buchhaltung/belege#rechnung-frage", "Ansehen", False))
        if neu:
            punkte.append(("honig", f"{neu} Beleg(e) mit unvollständigen Angaben", "Betrag oder Datum konnte nicht sicher gelesen werden.", f"/admin/buchhaltung/belege?monat={monat}&filter=ergaenzen", "Ergänzen", False))
        if portal:
            punkte.append(("honig", f"{portal} Rechnung(en) liegen nur im Kundenportal", "Dort herunterladen und hier hochladen.", "/admin/buchhaltung/belege?filter=portal", "Ansehen", False))
        for eintrag in summen.values():
            jetzt_s = eintrag["m"].get(monat, 0)
            vorher = [eintrag["m"][x] for x in (m1, m2, m3) if x in eintrag["m"]]
            if jetzt_s >= 10000 and len(vorher) >= 2:
                schnitt = sum(vorher) / len(vorher)
                if jetzt_s > 1.6 * schnitt:
                    punkte.append(("blau", f"Ungewöhnlich hoch: {eintrag['name']}", f"{bh.betrag_text(jetzt_s)} in diesem Monat – sonst im Schnitt {bh.betrag_text(round(schnitt))}",
                                   f"/admin/buchhaltung/lieferanten?monat={monat}&l={quote_plus(bh.schluessel_text(eintrag['name']))}", "Ansehen", False))
        if hat_vormonate:
            for r in neue:
                if r["gegenpartei"] and bh.schluessel_text(r["gegenpartei"]) not in frueher:
                    punkte.append(("blau", f"Neuer Empfänger: {r['gegenpartei']}", f"Erste Zahlung am {bh.datum_de(r['d'])} über {bh.betrag_text(abs(r['s']))}",
                                   f"/admin/buchhaltung/bank?monat={monat}&q={quote_plus(r['gegenpartei'])}", "Ansehen", False))
        ende = (datetime.strptime(monat + "-01", "%Y-%m-%d") + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        heute = bh.jetzt().date()
        soll_bis = min(ende.date(), heute - timedelta(days=1))
        if not konten:
            punkte.append(("blau", "Noch kein Kontoauszug hochgeladen", "Ohne Kontoauszug weiß das System nicht, welche Belege fehlen.", "/admin/buchhaltung/bank", "Kontoauszug hochladen", False))
        for k in konten:
            if k["bis"] and k["bis"] < soll_bis.isoformat() and k["bis"][:7] <= monat:
                punkte.append(("blau", f"Kontoauszug {k['name']} fehlt ab {bh.datum_de((datetime.strptime(k['bis'], '%Y-%m-%d') + timedelta(days=1)).date().isoformat())}",
                               f"Letzte Buchung im System: {bh.datum_de(k['bis'])}", "/admin/buchhaltung/bank#auszug", "Kontoauszug hochladen", False))
        return punkte

    # ---------------------------------------------------------- Übersicht
    def show_bh_uebersicht(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        monat = self.bh_monat(query)
        z = bh.monats_zahlen(monat)
        con = bh.db()
        quellen = con.execute("SELECT * FROM quellen ORDER BY name").fetchall()
        letzter = bh.einstellung(con, "letzter_lauf")
        adresse = bh.einstellung(con, "belege_adresse")
        abschluss = con.execute("SELECT * FROM monate WHERE monat=?", (monat,)).fetchone()
        belege_monat = con.execute("SELECT COUNT(*) FROM belege WHERE substr(datum,1,7)=? AND status NOT IN ('ignoriert','doppelt','unsicher')", (monat,)).fetchone()[0]
        auto = con.execute("SELECT COUNT(DISTINCT z.buchung_id) FROM zuordnung z JOIN buchungen b ON b.id=z.buchung_id WHERE substr(b.datum,1,7)=? AND z.art IN ('auto','dauer')", (monat,)).fetchone()[0]
        con.close()
        aus, ein = self.bh_summen(monat)
        aus_v, ein_v = self.bh_summen(self.bh_vormonat(monat))
        erledigt = z["zugeordnet"] + z["kein_beleg"]
        punkte = self.bh_pruefpunkte(monat)
        offen_anzahl = len(punkte)
        auto_quote = round(100 * auto / z["zugeordnet"]) if z["zugeordnet"] else None
        prozent = round(100 * erledigt / z["gesamt"]) if z["gesamt"] else 0
        # Die drei Schritte
        s1_ok = bool(quellen) and belege_monat > 0
        s2_ok = z["gesamt"] > 0 and not (z["offen"] or z["vorschlag"])
        s3_ok = bool(abschluss)
        jetzt_dran = 1 if not s1_ok else (2 if not s2_ok else (3 if not s3_ok else 0))

        def schritt(nr, titel, text, ok, balken=""):
            klasse = "ok" if ok else ("dran" if nr == jetzt_dran else "")
            zusatz = " ✓" if ok else (" – jetzt dran" if nr == jetzt_dran else "")
            return f'<div class="bh-schritt {klasse}"><b>{nr} · {esc(titel)}{zusatz}</b><span>{text}</span>{balken}</div>'
        quellen_text = f"{len(quellen)} Postfach/Postfächer" if quellen else '<a href="/admin/buchhaltung/einstellungen">Postfach verbinden</a>'
        s1 = schritt(1, "Belege sammeln", f"{belege_monat} Belege · {quellen_text}, Uploads und Fotos", s1_ok)
        s2 = schritt(2, "Zuordnen", (f"{erledigt} von {z['gesamt']} Zahlungen erledigt" if z["gesamt"] else "Kontoauszug hochladen"), s2_ok,
                     f'<span class="vw-balken"><span style="width:{prozent}%"></span></span>' if z["gesamt"] else "")
        s3 = schritt(3, "Abschließen", (f"Abgeschlossen am {esc(abschluss['abgeschlossen_am'][:10])}" if abschluss else "Export für DATEV, dann Monat sperren"), s3_ok)
        if s3_ok:
            ueberschrift = "Dieser Monat ist abgeschlossen"
            unter = "Später gefundene Belege landen automatisch in den Nachträgen."
        elif not z["gesamt"] and not belege_monat:
            ueberschrift = "Noch nichts da"
            unter = "Verbinde ein Postfach und lade den Kontoauszug hoch – dann geht es los."
        elif offen_anzahl:
            ueberschrift = "Fast fertig" if prozent >= 80 else "Es gibt etwas zu tun"
            unter = f"Noch {offen_anzahl} Punkt(e), dann kann der Monat zu DATEV"
        else:
            ueberschrift = "Alles erledigt"
            unter = "Der Monat kann exportiert werden."
        punkte_html = "".join(
            f'<div class="bh-punkt"><span class="bh-punkt-farbe {f}"></span><div class="bh-punkt-text"><b>{esc(t)}</b><small>{esc(u)}</small></div>'
            f'<a class="button{" primary" if haupt else ""}" href="{esc(l)}">{esc(k)}</a></div>'
            for f, t, u, l, k, haupt in punkte
        ) or '<p class="vw-leer">Nichts zu tun – alles andere hat das System erledigt. 👍</p>'
        quellen_status = ""
        if quellen:
            fehler = [q for q in quellen if q["letzter_fehler"]]
            quellen_status = (f'{len(quellen)} Postfach/Postfächer verbunden · zuletzt abgeholt {esc(letzter[:16]) if letzter else "noch nie"}'
                              + (f'<br><span class="minus">Problem bei: {esc(", ".join(q["name"] for q in fehler))}</span>' if fehler else ""))
        else:
            quellen_status = "Noch kein Postfach verbunden."
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.bh_lauf_html()}
        {self.bh_kopf('uebersicht', '', '', monat)}
        <section class="vw-karte bh-fortschritt">
            <div class="bh-fortschritt-kopf"><h3>{esc(ueberschrift)}</h3><span>{esc(unter)}</span></div>
            <div class="bh-schritte">{s1}{s2}{s3}</div>
        </section>
        <div class="vw-kennzahlen">
            <div><small>Ausgaben</small><b>{esc(bh.betrag_text(aus))}</b>{self.bh_veraenderung(aus, aus_v, False)}</div>
            <div><small>Einnahmen</small><b>{esc(bh.betrag_text(ein))}</b>{self.bh_veraenderung(ein, ein_v, True)}</div>
            <div><small>Automatisch zugeordnet</small><b>{f"{auto_quote} %" if auto_quote is not None else "–"}</b><span class="bh-trend">{auto} von {z['zugeordnet']} ohne Klick</span></div>
            <div><small>Zu prüfen</small><b class="{'minus' if offen_anzahl else ''}">{offen_anzahl}</b><span class="bh-trend">{'Punkte unten' if offen_anzahl else 'nichts offen'}</span></div>
        </div>
        <div class="bh-zwei">
            <section class="vw-karte bh-haupt">
                <div class="vw-karte-kopf"><h3>Das solltest du dir ansehen</h3></div>
                <div class="bh-punkte">{punkte_html}</div>
            </section>
            <aside class="bh-seite">
                <section class="vw-karte bh-adresse">
                    <small>Deine Belegadresse</small>
                    <b>{esc(adresse) if adresse else '<a href="/admin/buchhaltung/einstellungen#einstellungen">festlegen</a>'}</b>
                    <span>Lieferanten, Team und du schicken Rechnungen einfach hierhin. Fotos gehen über „Beleg fotografieren“ in der App.</span>
                </section>
                <section class="vw-karte">
                    <div class="vw-karte-kopf"><h3>Beleg hinzufügen</h3></div>
                    <button type="button" class="bh-ablage" data-dialog="belegHochladen">PDF oder Foto hochladen</button>
                </section>
                <section class="vw-karte">
                    <div class="vw-karte-kopf"><h3>Postfächer</h3><a class="vw-leise" href="/admin/buchhaltung/einstellungen">verwalten</a></div>
                    <p class="vw-klein">{quellen_status}</p>
                    {self.bh_abholen_knopf(None, 'Jetzt abholen', 'button') if quellen else '<a class="button" href="/admin/buchhaltung/einstellungen">+ Postfach verbinden</a>'}
                </section>
            </aside>
        </div>
        {self.bh_upload_dialog()}
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Buchhaltung", body)

    # ---------------------------------------------------------- Bank
    def show_bh_bank(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        monat = self.bh_monat(query)
        filt = self.query_text(query, "filter") or "alle"
        suche = self.query_text(query, "q")
        con = bh.db()
        konten = con.execute("SELECT k.*, MAX(b.datum) bis FROM konten k LEFT JOIN buchungen b ON b.konto_id=k.id GROUP BY k.id ORDER BY k.name").fetchall()
        konto_namen = {k["id"]: k["name"] for k in konten}
        buchungen = con.execute("SELECT * FROM buchungen WHERE substr(datum,1,7)=? ORDER BY datum DESC, id DESC", (monat,)).fetchall()
        zuord = {}
        for r in con.execute("SELECT z.buchung_id, b.id, b.lieferant, b.rechnungsnr, b.art, b.bar, b.dauer FROM zuordnung z JOIN belege b ON b.id=z.beleg_id"):
            zuord.setdefault(r["buchung_id"], []).append(r)
        vorschlaege = {r["id"]: r for r in con.execute("SELECT * FROM belege WHERE id IN (SELECT vorschlag_id FROM buchungen WHERE vorschlag_id IS NOT NULL)")}
        con.close()
        if suche:
            s = bh.schluessel_text(suche)
            buchungen = [b for b in buchungen if s in bh.schluessel_text(f"{b['gegenpartei']} {b['zweck']} {bh.betrag_text(b['betrag'])}")]
        z = {"alle": len(buchungen)}
        for bu in buchungen:
            z[bu["status"]] = z.get(bu["status"], 0) + 1
        if filt != "alle":
            buchungen = [bu for bu in buchungen if bu["status"] == filt]
        chips = [("alle", f"Alle {z['alle']}"), ("offen", f"Fehlt {z.get('offen', 0)}")]
        if z.get("vorschlag"):
            chips.append(("vorschlag", f"Bitte bestätigen {z['vorschlag']}"))
        chips += [("zugeordnet", f"Beleg da {z.get('zugeordnet', 0)}"), ("kein_beleg", f"Kein Beleg nötig {z.get('kein_beleg', 0)}")]
        q_teil = f"&q={quote_plus(suche)}" if suche else ""
        chips_html = "".join(f'<a class="vw-chip" aria-pressed="{"true" if k == filt else "false"}" href="/admin/buchhaltung/bank?monat={monat}&filter={k}{q_teil}">{esc(t)}</a>' for k, t in chips)
        zeilen = []
        for bu in buchungen:
            if bu["status"] == "zugeordnet":
                b0 = zuord.get(bu["id"], [])
                info = '<span class="bh-status da">✓ ' + esc(", ".join((b["rechnungsnr"] or b["lieferant"] or "Beleg") for b in b0)[:40]) + "</span>"
            elif bu["status"] == "vorschlag" and bu["vorschlag_id"] in vorschlaege:
                v = vorschlaege[bu["vorschlag_id"]]
                info = (f'<span class="bh-status frage">Passt {esc(v["rechnungsnr"] or v["lieferant"])}?</span>'
                        f'<form method="post" action="/admin/buchhaltung/buchung/zuordnen" class="vw-inline-form"><input type="hidden" name="buchung_id" value="{bu["id"]}"><input type="hidden" name="beleg_{v["id"]}" value="1"><input type="hidden" name="monat" value="{monat}"><button class="button bh-klein" type="submit">Ja</button></form>')
            elif bu["status"] == "kein_beleg":
                info = '<span class="bh-status grau">kein Beleg nötig</span>'
            else:
                info = '<span class="bh-status fehlt">fehlt</span>'
            zeilen.append(f"""
            <tr class="vw-klickzeile" data-bh-buchung="{bu['id']}">
                <td>{esc(bh.datum_de(bu['datum'])[:6])}<small class="vw-klein-block">{esc(konto_namen.get(bu['konto_id'], ''))}</small></td>
                <td><b>{esc(bu['gegenpartei'] or '–')}</b><small class="vw-klein-block">{esc(bu['zweck'][:90])}</small></td>
                <td class="zahl {'minus' if bu['betrag'] < 0 else 'plus'}"><b>{esc(bh.betrag_text(bu['betrag']))}</b></td>
                <td class="bh-info">{info}</td>
            </tr>""")
        konten_text = " · ".join(f"{k['name']} bis {bh.datum_de(k['bis'])}" for k in konten if k["bis"]) or "Noch kein Kontoauszug hochgeladen"
        konto_optionen = '<option value="">automatisch (aus der Datei)</option>' + "".join(f'<option value="{esc(k["name"])}">{esc(k["name"])}</option>' for k in konten)
        leer = ("Keine Zahlungen in diesem Monat. Lade oben rechts einen Kontoauszug hoch." if not z["alle"] and not suche
                else "Hier ist nichts – prima." if filt == "offen" else "Keine Treffer.")
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.bh_kopf('bank', esc(konten_text), '<button type="button" class="primary" data-dialog="auszugHochladen" id="auszug">Kontoauszug hochladen</button>', monat)}
        <form method="get" action="/admin/buchhaltung/bank" class="vw-werkzeug bh-filterleiste"><input type="hidden" name="monat" value="{monat}"><input type="hidden" name="filter" value="{esc(filt)}">
            <div class="vw-chips">{chips_html}</div>
            <label class="vw-suche"><span class="sr-only">Suchen</span><input type="search" name="q" value="{esc(suche)}" placeholder="Name oder Betrag"></label></form>
        <div class="vw-tabelle-wrap"><table class="vw-tabelle bh-buchungen">
            <tr><th>Datum</th><th>Empfänger / Zahler</th><th class="zahl">Betrag</th><th>Beleg</th></tr>
            {''.join(zeilen) or f'<tr><td colspan="4" class="vw-leer">{leer}</td></tr>'}
        </table></div>
        <p class="vw-klein">Klick auf eine Zahlung: Beleg zuordnen oder hochladen, beim Lieferanten anfordern oder „kein Beleg nötig“.</p>
        <dialog class="vw-dialog" id="auszugHochladen">
            <div class="vw-dialog-kopf"><h3>Kontoauszug hochladen</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/buchhaltung/konto/upload" enctype="multipart/form-data" class="vw-formular">
                <label>Datei aus dem Online-Banking<input type="file" name="datei" accept=".xml,.zip,.csv,.txt" required></label>
                <p class="vw-klein">Am besten im Format <b>CAMT.053</b> (XML oder ZIP). CSV geht auch – auch die Abrechnung von PayPal oder der Kreditkarte. Doppelte Zahlungen werden erkannt, überlappende Zeiträume sind also kein Problem.</p>
                <label>Zu welchem Konto?<select name="konto">{konto_optionen}</select></label>
                <label>… oder neues Konto anlegen<input name="konto_neu" placeholder="z. B. PayPal oder Kreditkarte"></label>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Hochladen und abgleichen</button></div>
            </form>
        </dialog>
        {self.bh_upload_dialog()}
        <dialog class="vw-dialog vw-seitenfenster bh-panel" id="bhBuchungPanel"><div id="bhBuchungPanelInhalt"></div></dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Bank", body)

    # ---------------------------------------------------------- Belege
    def show_bh_belege(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        monat = self.bh_monat(query)
        filt = self.query_text(query, "filter") or "alle"
        suche = self.query_text(query, "q")
        con = bh.db()
        belegt = {r["beleg_id"] for r in con.execute("SELECT beleg_id FROM zuordnung")}
        unsicher = con.execute("SELECT * FROM belege WHERE status='unsicher' ORDER BY id DESC").fetchall()
        portal = con.execute("SELECT * FROM belege WHERE status='portal' ORDER BY id DESC").fetchall()
        im_monat = con.execute("SELECT * FROM belege WHERE substr(COALESCE(NULLIF(datum,''), erstellt),1,7)=? AND status NOT IN ('unsicher','portal') ORDER BY COALESCE(NULLIF(datum,''), erstellt) DESC, id DESC", (monat,)).fetchall()
        aussortiert_alle = con.execute("SELECT * FROM belege WHERE status IN ('ignoriert','doppelt') ORDER BY id DESC LIMIT 300").fetchall()
        con.close()
        gueltig = [b for b in im_monat if b["status"] not in ("ignoriert", "doppelt")]
        gruppen = {
            "alle": gueltig,
            "eingang": [b for b in gueltig if b["art"] in ("ER", "GS")],
            "ausgang": [b for b in gueltig if b["art"] == "AR"],
            "ohne": [b for b in gueltig if b["id"] not in belegt and not b["bar"]],
            "ergaenzen": [b for b in gueltig if b["status"] == "neu" and not b["sicher"]],
            "portal": list(portal),
            "ignoriert": list(aussortiert_alle),
        }
        zeilen = gruppen.get(filt, gueltig)
        if suche:
            s = bh.schluessel_text(suche)
            zeilen = [b for b in zeilen if s in bh.schluessel_text(" ".join([b["lieferant"], b["rechnungsnr"], b["original_name"], b["mail_betreff"], bh.betrag_text(b["betrag"])]))]
        chips = [("alle", f"Alle {len(gruppen['alle'])}"), ("eingang", f"Eingang {len(gruppen['eingang'])}"), ("ausgang", f"Ausgang {len(gruppen['ausgang'])}"),
                 ("ohne", f"Ohne Zahlung {len(gruppen['ohne'])}")]
        if gruppen["ergaenzen"]:
            chips.append(("ergaenzen", f"Unvollständig {len(gruppen['ergaenzen'])}"))
        if portal:
            chips.append(("portal", f"Im Portal holen {len(portal)}"))
        chips.append(("ignoriert", "Aussortiert"))
        chips_html = "".join(f'<a class="vw-chip" aria-pressed="{"true" if k == filt else "false"}" href="/admin/buchhaltung/belege?monat={monat}&filter={k}">{esc(t)}</a>' for k, t in chips)
        frage_html = ""
        if unsicher:
            fragen = "".join(
                f"""<div class="bh-punkt"><div class="bh-punkt-text"><b>{esc(b['original_name'] or 'Dokument')}</b> · {esc(b['lieferant'] or parseaddr(b['mail_von'] or '')[1] or '')}{(' · ' + esc(bh.betrag_text(b['betrag']))) if b['betrag'] is not None else ''}<small>{esc(b['notiz'] or '')}</small></div>
                <button type="button" class="button" data-bh-beleg="{b['id']}">Ansehen</button>
                <form method="post" action="/admin/buchhaltung/beleg/urteil" class="vw-inline-form"><input type="hidden" name="id" value="{b['id']}">
                <button class="primary" type="submit" name="urteil" value="ja">Ja, Rechnung</button><button class="button" type="submit" name="urteil" value="nein">Keine Rechnung</button></form></div>"""
                for b in unsicher)
            frage_html = f"""<section class="vw-karte bh-frage-karte" id="rechnung-frage"><div class="vw-karte-kopf"><h3>Ist das eine Rechnung? ({len(unsicher)})</h3></div>
                <p class="vw-klein">Deine Antwort merkt sich das System für ähnliche Dokumente vom selben Absender.</p>{fragen}</section>"""
        kacheln = []
        for b in zeilen:
            if b["status"] == "portal":
                status = f'<a class="bh-status frage" href="{esc(b["portal_link"])}" target="_blank" rel="noopener">im Portal öffnen</a>'
            elif b["status"] in ("ignoriert", "doppelt"):
                status = f'<span class="bh-status grau">{esc(bh.STATUS_TEXT.get(b["status"], b["status"]))}</span>'
            elif b["id"] in belegt:
                status = '<span class="bh-status da">✓ Zahlung</span>'
            elif b["bar"]:
                status = '<span class="bh-status da">✓ bar bezahlt</span>'
            elif b["status"] == "neu" and not b["sicher"]:
                status = '<span class="bh-status frage">bitte ergänzen</span>'
            else:
                status = '<span class="bh-status frage">Zahlung offen</span>'
            endung = os.path.splitext(b["datei"] or "")[1].lower().lstrip(".") or "pdf"
            kacheln.append(f"""
            <button type="button" class="bh-kachel" data-bh-beleg="{b['id']}">
                <span class="bh-kachel-bild"><span class="bh-art {b['art']}">{esc(bh.ARTEN.get(b['art'], b['art']))}</span><small>{esc(endung.upper())}{' · Foto' if b['bar'] else ''}</small></span>
                <b>{esc(b['lieferant'] or 'Unbekannt')}</b>
                <small>{esc(bh.datum_de(b['datum']))} · {esc(b['rechnungsnr'] or 'ohne Nr.')}</small>
                <span class="bh-kachel-fuss"><b>{esc(bh.betrag_text(b['betrag'], b['waehrung']))}</b>{status}</span>
            </button>""")
        anzahl = len(gruppen["alle"])
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.bh_lauf_html()}
        {self.bh_kopf('belege', f'{anzahl} Belege in diesem Monat · automatisch benannt (Datum_Art_Lieferant_Nummer_Betrag)', '<button type="button" class="primary" data-dialog="belegHochladen">Beleg hochladen</button>', monat)}
        {frage_html}
        <form method="get" action="/admin/buchhaltung/belege" class="vw-werkzeug bh-filterleiste"><input type="hidden" name="monat" value="{monat}"><input type="hidden" name="filter" value="{esc(filt)}">
            <div class="vw-chips">{chips_html}</div>
            <label class="vw-suche"><span class="sr-only">Suchen</span><input type="search" name="q" value="{esc(suche)}" placeholder="Lieferant, Nummer, Betrag"></label></form>
        {'<p class="vw-klein">Alles, was keine Rechnung ist – automatisch oder von dir aussortiert. Nichts wird gelöscht; ein Klick auf das Dokument und „Wieder aufnehmen“ holt es zurück.</p>' if filt == 'ignoriert' else ''}
        <div class="bh-kacheln">{''.join(kacheln) or '<p class="vw-leer">Hier ist gerade nichts.</p>'}</div>
        {self.bh_upload_dialog()}
        <dialog class="vw-dialog vw-seitenfenster bh-panel" id="bhBelegPanel"><div id="bhBelegPanelInhalt"></div></dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Belege", body)

    # ---------------------------------------------------------- Lieferanten
    def bh_lieferanten_daten(self, jahr):
        con = bh.db()
        belege = con.execute("SELECT * FROM belege WHERE art IN ('ER','GS') AND status NOT IN ('ignoriert','doppelt','unsicher','portal') AND substr(datum,1,4)=?", (jahr,)).fetchall()
        offen = con.execute("SELECT * FROM buchungen WHERE status='offen' AND betrag<0 AND substr(datum,1,4)=?", (jahr,)).fetchall()
        belegt = {r["beleg_id"] for r in con.execute("SELECT beleg_id FROM zuordnung")}
        gelernt = {r["absender"]: r["urteil"] for r in con.execute("SELECT * FROM gelernt WHERE titel='*'")}
        con.close()
        liste = {}
        for b in belege:
            key = bh.schluessel_text(b["lieferant"] or "unbekannt") or "unbekannt"
            e = liste.setdefault(key, {"key": key, "name": b["lieferant"] or "Unbekannt", "belege": [], "summe": 0, "monate": {}, "fehlt": [], "ohne_zahlung": 0, "mail": ""})
            e["belege"].append(b)
            betrag = b["betrag"] or 0
            e["summe"] += -betrag if b["art"] == "GS" else betrag
            e["monate"][(b["datum"] or "")[:7]] = e["monate"].get((b["datum"] or "")[:7], 0) + betrag
            if b["id"] not in belegt and not b["bar"]:
                e["ohne_zahlung"] += 1
            if not e["mail"] and b["mail_von"]:
                e["mail"] = parseaddr(b["mail_von"])[1]
        for bu in offen:
            treffer = next((e for e in liste.values() if bh.gleicher_lieferant(e["name"], bu["gegenpartei"])), None)
            if treffer is None:
                key = bh.schluessel_text(bu["gegenpartei"] or "unbekannt") or "unbekannt"
                treffer = liste.setdefault(key, {"key": key, "name": bu["gegenpartei"] or "Unbekannt", "belege": [], "summe": 0, "monate": {}, "fehlt": [], "ohne_zahlung": 0, "mail": ""})
            treffer["fehlt"].append(bu)
        for e in liste.values():
            e["immer"] = bool(e["mail"]) and gelernt.get(e["mail"].lower()) == "ja"
        return sorted(liste.values(), key=lambda e: -e["summe"])

    def show_bh_lieferanten(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        monat = self.bh_monat(query)
        jahr = monat[:4]
        suche = self.query_text(query, "q")
        alle = self.bh_lieferanten_daten(jahr)
        liste = [e for e in alle if not suche or bh.schluessel_text(suche) in e["key"]]
        gewaehlt = self.query_text(query, "l") or (liste[0]["key"] if liste else "")
        e = next((x for x in alle if x["key"] == gewaehlt), None)
        zeilen = []
        for x in liste:
            if x["fehlt"]:
                stand = f'<span class="bh-status fehlt">{len(x["fehlt"])} Beleg(e) fehlen</span>'
            elif x["ohne_zahlung"]:
                stand = f'<span class="bh-status frage">{x["ohne_zahlung"]} Zahlung(en) offen</span>'
            else:
                stand = '<span class="bh-status da">✓ alles da</span>'
            zeilen.append(f"""<tr class="vw-klickzeile {'ist-aktiv' if e and x['key'] == e['key'] else ''}" data-href="/admin/buchhaltung/lieferanten?monat={monat}&l={quote_plus(x['key'])}{('&q=' + quote_plus(suche)) if suche else ''}">
                <td><b>{esc(x['name'])}</b></td><td class="zahl">{len(x['belege'])}</td><td class="zahl"><b>{esc(bh.betrag_text(x['summe']))}</b></td><td>{stand}</td></tr>""")
        detail = '<p class="vw-leer">Noch keine Lieferanten. Sie entstehen automatisch aus deinen Belegen und Zahlungen.</p>'
        if e:
            namen = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
            letzte6 = []
            m = monat
            for _ in range(6):
                letzte6.append(m)
                m = self.bh_vormonat(m)
            letzte6.reverse()
            hoechst = max([e["monate"].get(x, 0) for x in letzte6] + [1])
            balken = "".join(
                f'<div class="bh-balken-spalte"><span title="{esc(bh.betrag_text(e["monate"].get(x, 0)))}" style="height:{max(3, round(120 * e["monate"].get(x, 0) / hoechst))}px" class="{"jetzt" if x == monat else ""}"></span><small>{namen[int(x[5:]) - 1]}</small></div>'
                for x in letzte6)
            monate_mit = [v for k, v in e["monate"].items() if k]
            schnitt = round(sum(monate_mit) / len(monate_mit)) if monate_mit else 0
            belege_html = "".join(
                f'<button type="button" class="vw-zeile bh-zeile-knopf" data-bh-beleg="{b["id"]}"><span>{esc(bh.datum_de(b["datum"]))} · {esc(b["rechnungsnr"] or "ohne Nr.")}</span><b>{esc(bh.betrag_text(b["betrag"]))}</b></button>'
                for b in sorted(e["belege"], key=lambda b: b["datum"] or "", reverse=True)[:8])
            fehlt_html = "".join(
                f'<a class="vw-zeile" href="/admin/buchhaltung/bank?monat={(bu["datum"] or "")[:7]}&filter=offen"><span>{esc(bh.datum_de(bu["datum"]))} · Zahlung ohne Beleg</span><b class="minus">{esc(bh.betrag_text(bu["betrag"]))}</b></a>'
                for bu in e["fehlt"][:6])
            immer = ""
            if e["mail"]:
                immer = f"""<form method="post" action="/admin/buchhaltung/lieferant/immer" class="bh-immer"><input type="hidden" name="mail" value="{esc(e['mail'])}"><input type="hidden" name="zurueck" value="/admin/buchhaltung/lieferanten?monat={monat}&l={quote_plus(e['key'])}">
                    <label class="vw-schalter bh-schalter"><input type="checkbox" name="an" value="1" {'checked' if e['immer'] else ''} data-auto-submit><span>Alles von {esc(e['mail'])} ist eine Rechnung</span></label></form>"""
            detail = f"""
                <small class="bh-ueber">Lieferant</small>
                <h3 class="bh-detail-titel">{esc(e['name'])}</h3>
                <p class="vw-klein">{esc(e['mail']) + ' · Rechnungen kommen per Mail' if e['mail'] else 'Rechnungen kommen per Upload oder Foto'}</p>
                <h4 class="vw-zwischentitel">Ausgaben pro Monat</h4>
                <div class="bh-balken">{balken}</div>
                <div class="bh-mini-zahlen"><div><small>Ø pro Monat {esc(jahr)}</small><b>{esc(bh.betrag_text(schnitt))}</b></div><div><small>Summe {esc(jahr)}</small><b>{esc(bh.betrag_text(e['summe']))}</b></div></div>
                {f'<h4 class="vw-zwischentitel">Belege fehlen</h4><div class="vw-liste">{fehlt_html}</div>' if fehlt_html else ''}
                <h4 class="vw-zwischentitel">Letzte Belege</h4><div class="vw-liste">{belege_html or '<p class="vw-leer">Noch keine.</p>'}</div>
                {immer}"""
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.bh_kopf('lieferanten', f'Automatisch aus deinen Belegen und Zahlungen {esc(jahr)} – nichts einzutragen.', '', monat)}
        <div class="bh-zwei">
            <section class="bh-haupt">
                <form method="get" action="/admin/buchhaltung/lieferanten" class="vw-werkzeug"><input type="hidden" name="monat" value="{monat}">
                    <label class="vw-suche"><span class="sr-only">Suchen</span><input type="search" name="q" value="{esc(suche)}" placeholder="Lieferant suchen"></label></form>
                <div class="vw-tabelle-wrap"><table class="vw-tabelle">
                    <tr><th>Lieferant</th><th class="zahl">Belege {esc(jahr)}</th><th class="zahl">Summe {esc(jahr)}</th><th>Stand</th></tr>
                    {''.join(zeilen) or '<tr><td colspan="4" class="vw-leer">Noch keine Lieferanten.</td></tr>'}
                </table></div>
            </section>
            <aside class="vw-karte bh-seite bh-detail">{detail}</aside>
        </div>
        <dialog class="vw-dialog vw-seitenfenster bh-panel" id="bhBelegPanel"><div id="bhBelegPanelInhalt"></div></dialog>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Lieferanten", body)

    def handle_bh_lieferant_immer(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        mail = self.form_value(form, "mail").strip().lower()
        zurueck = self.form_value(form, "zurueck")
        if not zurueck.startswith("/admin/buchhaltung/lieferanten"):
            zurueck = "/admin/buchhaltung/lieferanten"
        if mail:
            con = bh.db()
            if self.form_value(form, "an"):
                con.execute("INSERT OR REPLACE INTO gelernt (absender, titel, urteil, beispiel, erstellt) VALUES (?, '*', 'ja', 'von dir festgelegt', ?)", (mail, bh.jetzt_text()))
            else:
                con.execute("DELETE FROM gelernt WHERE absender=? AND titel='*' AND urteil='ja'", (mail,))
            con.commit()
            con.close()
        self.redirect(zurueck)

    # ---------------------------------------------------------- Export
    def show_bh_export(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        monat = self.bh_monat(query)
        z = bh.monats_zahlen(monat)
        con = bh.db()
        abschluss = con.execute("SELECT * FROM monate WHERE monat=?", (monat,)).fetchone()
        abschluesse = {r["monat"]: r for r in con.execute("SELECT * FROM monate")}
        unsicher = con.execute("SELECT COUNT(*) FROM belege WHERE status='unsicher'").fetchone()[0]
        er = con.execute("SELECT COUNT(*) FROM belege WHERE art IN ('ER','GS') AND status NOT IN ('ignoriert','doppelt','portal','unsicher') AND substr(datum,1,7)=?", (monat,)).fetchone()[0]
        ar = con.execute("SELECT COUNT(*) FROM belege WHERE art='AR' AND status NOT IN ('ignoriert','doppelt','portal','unsicher') AND substr(datum,1,7)=?", (monat,)).fetchone()[0]
        konten = con.execute("SELECT k.name, MAX(b.datum) bis FROM konten k LEFT JOIN buchungen b ON b.konto_id=k.id GROUP BY k.id").fetchall()
        nachtraege = con.execute("SELECT COUNT(*) FROM belege b JOIN monate m ON m.monat=substr(b.datum,1,7) WHERE b.erstellt > m.abgeschlossen_am AND b.status NOT IN ('ignoriert','doppelt','unsicher','portal')").fetchone()[0]
        con.close()
        ende = ((datetime.strptime(monat + "-01", "%Y-%m-%d") + timedelta(days=32)).replace(day=1) - timedelta(days=1)).date()
        auszug_ok = bool(konten) and all((k["bis"] or "") >= min(ende, bh.jetzt().date() - timedelta(days=1)).isoformat() for k in konten)

        def punkt(ok, titel, text):
            return f'<div class="bh-check"><span class="bh-check-zeichen {"ok" if ok else "warn"}">{"✓" if ok else "!"}</span><div><b>{esc(titel)}</b><small>{esc(text)}</small></div></div>'
        fehlend = z["offen"] + z["vorschlag"]
        checks = "".join([
            punkt(auszug_ok, "Kontoauszüge bis Monatsende", " · ".join(f"{k['name']} bis {bh.datum_de(k['bis'])}" for k in konten) or "Noch kein Kontoauszug hochgeladen"),
            punkt(not fehlend, f"{fehlend} Zahlung(en) ohne Beleg" if fehlend else "Alle Zahlungen haben einen Beleg",
                  "Du kannst trotzdem exportieren – sie stehen dann auf der Liste „Fehlende Belege“." if fehlend else f"{z['zugeordnet']} zugeordnet, {z['kein_beleg']} ohne Beleg nötig"),
            punkt(not unsicher, "Alle Dokumente geprüft" if not unsicher else f"{unsicher} „Rechnung?“-Frage(n) offen", "Keine offenen Fragen" if not unsicher else "Unter Belege oben beantworten"),
        ])
        inhalt = "".join(f'<div class="bh-ordner"><span>📁</span><span>{esc(n)}</span><small>{esc(c)}</small></div>' for n, c in [
            ("Rechnungseingang", f"{er} Belege"), ("Rechnungsausgang", f"{ar} Belege"), ("Fehlende_Belege.pdf", f"{fehlend} Zahlungen"),
            ("Offene_Rechnungen.pdf", "Rechnungen ohne Zahlung"), ("Abgleich.xlsx", "jede Zahlung mit ihrem Beleg")])
        andere = []
        for m in bh.monate_mit_daten()[:8]:
            if m == monat:
                continue
            ab = abschluesse.get(m)
            zz = bh.monats_zahlen(m)
            stand = (f'<span class="bh-status da">✓ abgeschlossen {esc(ab["abgeschlossen_am"][:10])}</span>' if ab
                     else (f'<span class="bh-status frage">{zz["offen"] + zz["vorschlag"]} fehlen</span>' if zz["offen"] + zz["vorschlag"] else '<span class="bh-status grau">offen</span>'))
            andere.append(f'<a class="vw-zeile" href="/admin/buchhaltung/export?monat={m}"><span>{m[5:]}/{m[:4]}</span>{stand}</a>')
        name = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"][int(monat[5:]) - 1]
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.bh_kopf('export', 'Ein Klick – du bekommst eine ZIP-Datei, die du bei DATEV hochlädst.', '', monat)}
        <div class="bh-zwei">
            <section class="vw-karte bh-haupt">
                <div class="vw-karte-kopf"><h3>{esc(name)} – bereit?</h3>{f'<span class="vw-marke gruen">abgeschlossen {esc(abschluss["abgeschlossen_am"][:10])}</span>' if abschluss else ''}</div>
                {checks}
                <div class="vw-knopfreihe bh-export-knoepfe">
                    <a class="button primary" href="/admin/buchhaltung/export.zip?monat={monat}">⤓ {esc(name)} als ZIP herunterladen</a>
                    <form method="post" action="/admin/buchhaltung/monat/{'oeffnen' if abschluss else 'abschliessen'}" {'' if abschluss else f'data-vw-confirm="{esc(name)} abschließen? Später gefundene Belege landen dann im Ordner Nachtrag."'}><input type="hidden" name="monat" value="{monat}"><button class="button" type="submit">{'Wieder öffnen' if abschluss else 'Monat abschließen'}</button></form>
                </div>
                <p class="vw-klein">Abschließen sperrt den Monat. Kommt danach noch ein Beleg, landet er beim nächsten Export im Ordner „Nachtrag“.</p>
            </section>
            <aside class="bh-seite">
                <section class="vw-karte"><div class="vw-karte-kopf"><h3>Inhalt der ZIP-Datei</h3></div>{inhalt}
                    <p class="vw-klein">Dateinamen: Datum_Art_Lieferant_Nummer_Betrag. E-Rechnungen zusätzlich als XML-Original.</p></section>
                <section class="vw-karte"><div class="vw-karte-kopf"><h3>Andere Monate</h3></div><div class="vw-liste">{''.join(andere) or '<p class="vw-leer">Noch keine.</p>'}</div>
                    {f'<p class="vw-klein">{nachtraege} Nachtrag/Nachträge für abgeschlossene Monate – sie kommen beim Export des jeweiligen Monats in den Ordner „Nachtrag“.</p>' if nachtraege else ''}</section>
            </aside>
        </div>
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Export", body)

    # ---------------------------------------------------------- Einstellungen (Zahnrad)
    def show_bh_einstellungen(self, query=None):
        if not self.is_admin():
            return self.redirect("/admin/login")
        query = query or {}
        con = bh.db()
        quellen = con.execute("SELECT * FROM quellen ORDER BY name").fetchall()
        namen = bh.einstellung(con, "eigene_namen")
        domains = bh.einstellung(con, "eigene_domains")
        belege_adresse = bh.einstellung(con, "belege_adresse")
        erinnerung_an = bh.einstellung(con, "erinnerung_an")
        gelernt = con.execute("SELECT * FROM gelernt ORDER BY erstellt DESC").fetchall()
        regeln = con.execute("SELECT * FROM regeln ORDER BY name").fetchall()
        importe = con.execute("SELECT i.*, k.name konto FROM importe i LEFT JOIN konten k ON k.id=i.konto_id ORDER BY i.id DESC LIMIT 5").fetchall()
        con.close()
        karten, dialoge = [], []
        for q in quellen:
            d = dict(q)
            status = "Problem" if q["letzter_fehler"] else ("pausiert" if not q["aktiv"] else "aktiv")
            marke = "rot" if q["letzter_fehler"] else ("grau" if not q["aktiv"] else "gruen")
            karten.append(f"""
            <div class="bh-quelle-zeile" id="q-{q['id']}">
                <div><b>{esc(q['name'])}</b> <span class="vw-marke {marke}">{status}</span>
                    <small class="vw-klein-block">{esc(q['benutzer'])} · {esc(q['letzter_status'] or 'noch nie abgeholt')}{' · jeder Anhang ist ein Beleg' if q['alles_ist_beleg'] else ''}{(' · liest ab ' + esc(bh.datum_de(q['start_datum']))) if q['start_datum'] else ''}</small></div>
                <div class="vw-knopfreihe">
                    {self.bh_abholen_knopf(q['id'], 'Abholen', 'button')}
                    <button type="button" class="button" data-dialog="quelle-{q['id']}">Bearbeiten</button>
                </div>
            </div>""")
            dialoge.append(f"""
            <dialog class="vw-dialog" id="quelle-{q['id']}">
                <div class="vw-dialog-kopf"><h3>Postfach bearbeiten</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
                <form method="post" action="/admin/buchhaltung/quelle/speichern" class="vw-formular">{self.bh_quelle_formular(d)}
                    <p class="vw-klein">Ändere „Ab wann einlesen“ auf ein früheres Datum, um ältere Mails noch einmal zu durchsuchen. Schon vorhandene Belege werden nicht doppelt angelegt.</p>
                    <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div></form>
                <div class="vw-knopfreihe">
                    <form method="post" action="/admin/buchhaltung/quelle/testen"><input type="hidden" name="quelle_id" value="{q['id']}"><button class="button" type="submit">Verbindung testen</button></form>
                    <form method="post" action="/admin/buchhaltung/quelle/aktiv"><input type="hidden" name="quelle_id" value="{q['id']}"><button class="vw-leise" type="submit">{'Pausieren' if q['aktiv'] else 'Wieder aktivieren'}</button></form>
                </div>
                <form method="post" action="/admin/buchhaltung/quelle/loeschen" class="vw-gefahrzone" data-vw-confirm="Postfach „{esc(q['name'])}“ entfernen? Bereits gefundene Belege bleiben erhalten.">
                    <input type="hidden" name="quelle_id" value="{q['id']}"><span>Postfach entfernen (Belege bleiben)</span><button class="danger" type="submit">Entfernen</button></form>
            </dialog>""")
        gelernt_html = ('<div class="vw-tabelle-wrap vw-ohne-rand"><table class="vw-tabelle"><tr><th>Absender</th><th>Dokumentart</th><th>Entscheidung</th><th></th></tr>' + ''.join(
            f"""<tr><td>{esc(r['absender'] if r['absender'] != '*' else 'jeder Absender')}</td><td>{esc(r['titel'] if r['titel'] != '*' else 'alles')}<small class="vw-klein-block">{esc(r['beispiel'])}</small></td>
            <td>{'<span class="vw-marke gruen">Rechnung</span>' if r['urteil'] == 'ja' else '<span class="vw-marke grau">überspringen</span>'}</td>
            <td><form method="post" action="/admin/buchhaltung/erkennung/regel-loeschen"><input type="hidden" name="absender" value="{esc(r['absender'])}"><input type="hidden" name="titel" value="{esc(r['titel'])}"><button class="vw-leise" type="submit">Vergessen</button></form></td></tr>"""
            for r in gelernt) + '</table></div>') if gelernt else '<p class="vw-leer">Noch nichts gelernt.</p>'
        regeln_html = "".join(
            f'<li><span>{esc(r["muster"])}</span><form method="post" action="/admin/buchhaltung/regel/loeschen" class="vw-inline-form"><input type="hidden" name="regel_id" value="{r["id"]}"><button class="vw-leise" type="submit" aria-label="Regel löschen">×</button></form></li>'
            for r in regeln)
        importe_html = "".join(f'<li>{esc(i["erstellt"][:16])} · {esc(i["konto"] or "")} · {esc(i["dateiname"])}: {i["neu"]} neu, {i["doppelt"]} schon da</li>' for i in importe)
        body = f"""
        {admin_meldungen(self.query_text(query, 'msg'), self.query_text(query, 'error'))}
        {self.bh_lauf_html()}
        {admin_kopf('Buchhaltung · Einstellungen', 'Postfächer, Belegadresse, Regeln und gelernte Rechnungserkennung.', '<a class="button" href="/admin/buchhaltung">‹ Zurück zur Übersicht</a>')}
        <section class="vw-karte">
            <div class="vw-karte-kopf"><h3>Postfächer</h3><button type="button" class="primary" data-dialog="neueQuelle">+ Postfach verbinden</button></div>
            <p class="vw-klein">Die Postfächer werden nur gelesen – nichts wird gelöscht, verschoben oder als gelesen markiert. Passwörter werden verschlüsselt gespeichert. Abgeholt wird jede Nacht und morgens um halb sechs automatisch.</p>
            {''.join(karten) or '<p class="vw-leer">Noch kein Postfach. Tipp: Lege bei Strato ein eigenes Postfach belege@opapeters.de an und verbinde es hier mit „Jeder Anhang ist ein Beleg“.</p>'}
        </section>
        <section class="vw-karte" id="einstellungen">
            <div class="vw-karte-kopf"><h3>Allgemein</h3></div>
            <form method="post" action="/admin/buchhaltung/einstellungen" class="vw-formular">
                <div class="vw-zwei"><label>Deine Belegadresse<input type="email" name="belege_adresse" value="{esc(belege_adresse)}" placeholder="belege@opapeters.de"></label>
                    <label>Monatserinnerung am 3. an<input type="email" name="erinnerung_an" value="{esc(erinnerung_an)}" placeholder="info@opapeters.de"></label></div>
                <div class="vw-zwei"><label>Eigene Firmennamen (mit Komma)<input name="eigene_namen" value="{esc(namen)}"></label>
                    <label>Eigene E-Mail-Domains (mit Komma)<input name="eigene_domains" value="{esc(domains)}"></label></div>
                <p class="vw-klein">Rechnungen, in denen deine Firma als Verkäufer steht, gelten als Ausgangsrechnung.</p>
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Speichern</button></div>
            </form>
        </section>
        <div class="vw-raster-2">
            <section class="vw-karte"><div class="vw-karte-kopf"><h3>Kein Beleg nötig</h3></div>
                <p class="vw-klein">Zahlungen, deren Empfänger oder Verwendungszweck diesen Text enthält, zählen nie als fehlend (z. B. Lohn, Finanzamt, Bankgebühren).</p>
                <ul class="bh-regeln">{regeln_html or '<li class="vw-klein">Noch keine Regeln.</li>'}</ul>
                <form method="post" action="/admin/buchhaltung/regel/neu" class="vw-inline-form"><input name="muster" placeholder="z. B. Finanzamt" required class="vw-wachsen"><button class="button" type="submit">+ Regel</button></form></section>
            <section class="vw-karte"><div class="vw-karte-kopf"><h3>Letzte Kontoauszüge</h3></div><ul class="bh-importe">{importe_html or '<li class="vw-klein">Noch nichts hochgeladen.</li>'}</ul></section>
        </div>
        <details class="vw-karte vw-aufklapp" id="gelernt">
            <summary>Rechnungserkennung: was das System gelernt hat ({len(gelernt)})</summary>
            <p class="vw-klein">Ein PDF wird nur übernommen, wenn es wie eine Rechnung aussieht. BWA, Umsatzberichte, Mahnungen, Angebote, Lieferscheine, AGB usw. werden übersprungen. Unklare Fälle landen unter Belege → „Ist das eine Rechnung?“.</p>
            {gelernt_html}
            <form method="post" action="/admin/buchhaltung/erkennung/neu-pruefen" class="vw-knopfreihe" data-vw-confirm="Alle noch nicht bestätigten Belege aus Postfächern neu prüfen? Nichts wird gelöscht.">
                <button class="button" type="submit">Alle Belege neu prüfen</button></form>
        </details>
        <form method="post" action="/admin/buchhaltung/leeren" class="vw-gefahrzone" data-vw-confirm="Wirklich ALLE Belege und Kontoauszüge löschen? Vorher wird automatisch eine Sicherung angelegt. Postfächer, Einstellungen und Gelerntes bleiben.">
            <span>Buchhaltung leeren – alle Belege und Kontoauszüge entfernen und bei null anfangen (mit Sicherung)</span><button class="danger" type="submit">Leeren</button></form>
        <dialog class="vw-dialog" id="neueQuelle">
            <div class="vw-dialog-kopf"><h3>Postfach verbinden</h3><button type="button" class="vw-zu" data-schliessen aria-label="Schließen">×</button></div>
            <form method="post" action="/admin/buchhaltung/quelle/speichern" class="vw-formular">{self.bh_quelle_formular()}
                <div class="vw-dialog-fuss"><button class="primary" type="submit">Verbinden und testen</button></div></form>
        </dialog>
        {''.join(dialoge)}
        <script src="/static/verwaltung.js?v={ASSET_VERSION}" defer></script>"""
        self.admin_seite("Buchhaltung · Einstellungen", body)

    def handle_bh_leeren(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        self.read_form()
        try:
            ziel = bh.alles_leeren("Von Hand geleert")
        except Exception as fehler:
            return self.redirect("/admin/buchhaltung/einstellungen?error=" + quote_plus(f"Leeren fehlgeschlagen: {fehler}"))
        self.redirect("/admin/buchhaltung?msg=" + quote_plus(f"Alles geleert. Sicherung: {os.path.basename(ziel)}. Ab jetzt kommen nur neue Mails dazu."))


    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        maybe_run_auto_time_export()
        try:
            if path == "/login":
                return self.handle_buyer_login()
            if path == "/cart-draft":
                return self.handle_cart_draft()
            if path == "/cockpit/task":
                return self.handle_cockpit_task_state()
            if path == "/cockpit/order-state":
                return self.handle_cockpit_order_state()
            if path == "/cockpit/production-job":
                return self.handle_cockpit_production_job_state()
            if path == "/messages/send":
                return self.handle_location_message_send()
            if path == "/api/production-job":
                return self.handle_api_production_job()
            if path == "/push/subscribe":
                return self.handle_push_subscribe()
            if path == "/push/unsubscribe":
                return self.handle_push_unsubscribe()
            if path == "/order":
                return self.handle_order()
            if path == "/time":
                return self.handle_time_entry()
            if path == "/admin/login":
                return self.handle_admin_login()
            if path == "/admin/push/test":
                return self.handle_admin_push_test()
            if path == "/admin/add-product":
                return self.handle_add_product()
            if path == "/admin/update-product":
                return self.handle_update_product()
            if path == "/admin/bulk-products":
                return self.handle_bulk_products()
            if path == "/admin/orders/combined-pdf":
                return self.handle_combined_order_pdf()
            if path == "/admin/einkauf/pdf":
                return self.handle_einkauf_pdf()
            if path == "/admin/einkauf/erledigt":
                return self.handle_einkauf_erledigt()
            if path == "/admin/orders/status":
                return self.handle_order_status()
            if path == "/admin/orders/delete":
                return self.handle_delete_orders()
            if path == "/admin/order-items/status":
                return self.handle_order_item_status()
            if path == "/admin/order-items/delete":
                return self.handle_delete_order_items()
            if path == "/admin/delete-product":
                return self.handle_delete_product()
            if path == "/admin/add-category":
                return self.handle_add_category()
            if path == "/admin/rename-category":
                return self.handle_rename_category()
            if path == "/admin/import-confirm":
                return self.handle_import_confirm()
            if path == "/admin/delete-category":
                return self.handle_delete_category()
            if path == "/admin/import-products":
                return self.handle_import_products()
            if path == "/admin/settings":
                return self.handle_settings()
            if path == "/admin/cockpit-content":
                return self.handle_admin_cockpit_content()
            if path == "/admin/aufgaben/neu":
                return self.handle_aufgaben("neu")
            if path == "/admin/aufgaben/gruppe":
                return self.handle_aufgaben_gruppe()
            if path == "/admin/aufgaben/gruppe-loeschen":
                return self.handle_aufgaben_gruppe(loeschen=True)
            if path == "/admin/aufgaben/speichern":
                return self.handle_aufgaben("speichern")
            if path == "/admin/aufgaben/loeschen":
                return self.handle_aufgaben("loeschen")
            if path == "/admin/messages/reply":
                return self.handle_admin_message_reply()
            if path == "/admin/employees":
                return self.handle_employees()
            if path == "/admin/locations":
                return self.handle_locations()
            if path == "/admin/visibility":
                return self.handle_visibility()
            if path == "/admin/location/speichern":
                return self.handle_location_speichern()
            if path == "/admin/location/neu":
                return self.handle_location_neu()
            if path == "/admin/location/loeschen":
                return self.handle_location_loeschen()
            if path == "/admin/settings/testmail":
                return self.handle_test_mail()
            if path == "/admin/settings/tagesmail":
                return self.handle_tagesmail_jetzt()
            if path == "/admin/buchhaltung/abholen":
                return self.handle_bh_abholen()
            if path == "/admin/buchhaltung/quelle/speichern":
                return self.handle_bh_quelle("speichern")
            if path == "/admin/buchhaltung/quelle/testen":
                return self.handle_bh_quelle("testen")
            if path == "/admin/buchhaltung/quelle/aktiv":
                return self.handle_bh_quelle("aktiv")
            if path == "/admin/buchhaltung/quelle/loeschen":
                return self.handle_bh_quelle("loeschen")
            if path == "/admin/buchhaltung/einstellungen":
                return self.handle_bh_einstellungen()
            if path == "/admin/buchhaltung/lieferant/immer":
                return self.handle_bh_lieferant_immer()
            if path == "/admin/buchhaltung/leeren":
                return self.handle_bh_leeren()
            if path == "/admin/buchhaltung/beleg/upload":
                return self.handle_bh_beleg_upload()
            if path == "/admin/buchhaltung/beleg/speichern":
                return self.handle_bh_beleg_speichern()
            if path == "/admin/buchhaltung/beleg/urteil":
                return self.handle_bh_beleg_urteil()
            if path == "/admin/buchhaltung/erkennung/neu-pruefen":
                return self.handle_bh_erkennung("neu-pruefen")
            if path == "/admin/buchhaltung/erkennung/regel-loeschen":
                return self.handle_bh_erkennung("regel-loeschen")
            if path == "/admin/buchhaltung/konto/upload":
                return self.handle_bh_konto_upload()
            if path == "/admin/buchhaltung/buchung/zuordnen":
                return self.handle_bh_buchung("zuordnen")
            if path == "/admin/buchhaltung/buchung/loesen":
                return self.handle_bh_buchung("loesen")
            if path == "/admin/buchhaltung/buchung/kein-beleg":
                return self.handle_bh_buchung("kein-beleg")
            if path == "/admin/buchhaltung/regel/neu":
                return self.handle_bh_buchung("regel-neu")
            if path == "/admin/buchhaltung/buchung/anfordern":
                return self.handle_bh_anfordern()
            if path == "/admin/buchhaltung/regel/loeschen":
                return self.handle_bh_buchung("regel-loeschen")
            if path == "/admin/buchhaltung/monat/abschliessen":
                return self.handle_bh_monat("abschliessen")
            if path == "/admin/buchhaltung/monat/oeffnen":
                return self.handle_bh_monat("oeffnen")
            if path == "/beleg":
                return self.handle_beleg_foto()
            if path == "/kasse":
                return self.handle_kasse()
            if path == "/admin/tagesabschluss/einstellungen":
                return self.handle_tagesabschluss("einstellungen")
            if path == "/admin/tagesabschluss/senden":
                return self.handle_tagesabschluss("senden")
            if path == "/admin/tagesabschluss/bericht":
                return self.handle_tagesabschluss("bericht")
            if path == "/admin/time/create":
                return self.handle_create_time_entry()
            if path == "/admin/time/settings":
                return self.handle_time_settings()
            if path == "/admin/time/update":
                return self.handle_update_time_entry()
            if path == "/admin/time/delete":
                return self.handle_delete_time_entry()
            self.send_error(404)
        except RequestTooLarge as exc:
            self.send_html(page("Datei zu groß", f"<div class='error box narrow'>{esc(exc)}</div>", admin=self.is_admin(), buyer_key=self.current_buyer_key()), 413)

    def handle_buyer_login(self):
        form = self.read_form()
        requested_open_location = self.form_value(form, "open_location").strip()
        buyer_key = self.form_value(form, "buyer_key").strip()
        pin = self.form_value(form, "pin").strip()
        location = find_location(buyer_key)
        expected_password = (location or {}).get("password", "")
        if location and pin == expected_password:
            if not (location_can_order(location) or location_can_time(location)):
                return self.show_buyer_login("Für diese Rolle ist aktuell kein Bereich freigeschaltet.")
            target = "/cockpit"
            self.send_response(303)
            self.send_header("Set-Cookie", f"buyer={make_buyer_token(buyer_key)}; HttpOnly; SameSite=Lax; Path=/")
            self.send_header("Location", target)
            self.end_headers()
        else:
            self.show_buyer_login("Falscher Standort oder falsches Standort-Passwort.")

    def handle_cockpit_task_state(self):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        form = self.read_form()
        task_id = self.form_value(form, "task_id").strip()
        state = self.form_value(form, "state", "open").strip()
        completed_by = self.form_value(form, "completed_by").strip()
        valid_ids = {task["id"] for task in normalize_cockpit_tasks((location or {}).get("cockpit_tasks", []))}
        if task_id not in valid_ids:
            return self.redirect("/cockpit?msg=" + quote_plus("Aufgabe wurde nicht gefunden."))
        if state == "done" and completed_by not in get_time_employee_names(True):
            return self.redirect("/cockpit?msg=" + quote_plus("Bitte auswählen, wer die Aufgabe erledigt hat."))
        set_cockpit_state(buyer_key, "task", task_id, state, completed_by if state == "done" else "")
        return self.redirect("/cockpit")

    def handle_cockpit_order_state(self):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        form = self.read_form()
        order_id = self.form_value(form, "order_id").strip()
        state = self.form_value(form, "state", "open").strip()
        valid_ids = {item["id"] for item in normalize_cockpit_orders((location or {}).get("cockpit_orders", []))}
        if order_id not in valid_ids:
            return self.redirect("/cockpit?msg=" + quote_plus("Bestellinformation wurde nicht gefunden."))
        set_cockpit_state(buyer_key, "order", order_id, state)
        return self.redirect("/cockpit")

    def handle_location_message_send(self):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location:
            return self.redirect("/login")
        form = self.read_form()
        recipient_name = self.form_value(form, "recipient_name").strip()
        message = self.form_value(form, "message").strip()
        if recipient_name not in get_time_employee_names(True):
            return self.redirect("/messages/select")
        if not message:
            return self.redirect("/messages?recipient=" + quote_plus(recipient_name) + "&error=" + quote_plus("Bitte eine Nachricht eingeben."))
        create_cockpit_message(location["id"], recipient_name, "location", location["name"], message)
        send_push_notification({
            "title": "Neue Nachricht",
            "body": f"{location['name']} an {recipient_name}: {message[:90]}",
            "url": "/admin/messages",
            "tag": f"message-{location['id']}-{normalize_text_key(recipient_name)}",
        })
        return self.redirect("/messages?recipient=" + quote_plus(recipient_name) + "&msg=" + quote_plus("Nachricht wurde gesendet."))

    def handle_admin_message_reply(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        location_id = self.form_value(form, "location_id").strip()
        recipient_name = self.form_value(form, "recipient_name").strip()
        message = self.form_value(form, "message").strip()
        if not find_location(location_id):
            return self.redirect("/admin/messages?error=" + quote_plus("Standort wurde nicht gefunden."))
        if not recipient_name or not message:
            return self.redirect("/admin/messages?location=" + quote_plus(location_id) + "&recipient=" + quote_plus(recipient_name) + "&error=" + quote_plus("Bitte eine Antwort eingeben."))
        create_cockpit_message(location_id, recipient_name, "admin", "Backend", message)
        return self.redirect("/admin/messages?location=" + quote_plus(location_id) + "&recipient=" + quote_plus(recipient_name) + "&msg=" + quote_plus("Antwort wurde gesendet."))

    def handle_cockpit_production_job_state(self):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location or not (is_production_location(location) or is_schwarzenbek_location(location)):
            return self.redirect("/cockpit?msg=" + quote_plus("Produktionsauftrag wurde nicht gefunden."))
        form = self.read_form()
        job_id = self.form_value(form, "job_id").strip()
        state = self.form_value(form, "state", "open").strip()
        completed_by = self.form_value(form, "completed_by").strip()
        owner_location = location if is_production_location(location) else find_production_job_owner(job_id)
        valid_ids = {item["id"] for item in normalize_production_jobs((owner_location or {}).get("production_jobs", []))}
        if not owner_location or job_id not in valid_ids:
            return self.redirect("/cockpit?msg=" + quote_plus("Produktionsauftrag wurde nicht gefunden."))
        if state == "started" and not completed_by:
            return self.redirect("/cockpit?msg=" + quote_plus("Bitte eintragen, wer den Auftrag angefangen hat."))
        if state == "done" and not completed_by:
            previous = get_cockpit_state_details(owner_location["id"]).get(("production_job", job_id), {})
            completed_by = previous.get("completed_by", "")
        set_cockpit_state(owner_location["id"], "production_job", job_id, state, completed_by)
        return self.redirect("/cockpit")

    def handle_api_production_job(self):
        if not COCKPIT_API_TOKEN:
            return self.send_json({"ok": False, "error": "Schnittstelle ist nicht eingerichtet."}, 403)
        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-API-Key", "")
        expected = f"Bearer {COCKPIT_API_TOKEN}"
        if not (hmac.compare_digest(auth, expected) or hmac.compare_digest(api_key, COCKPIT_API_TOKEN)):
            return self.send_json({"ok": False, "error": "Nicht erlaubt."}, 401)
        payload = self.read_json_payload(max_bytes=64 * 1024)
        section = str(payload.get("section") or "").strip().lower()
        customer_name = str(payload.get("customer_name") or payload.get("name") or payload.get("title") or "").strip()
        order_text = str(payload.get("order_text") or payload.get("what") or payload.get("title") or "").strip()
        title = customer_name or order_text
        due_date = str(payload.get("due_date") or "").strip()
        note = str(payload.get("note") or "").strip()
        location_hint = str(payload.get("location") or "produktion").strip()
        if section not in PRODUCTION_SECTIONS:
            return self.send_json({"ok": False, "error": "Bereich muss backen, eisvitrinen oder eistorten sein."}, 400)
        if not title:
            return self.send_json({"ok": False, "error": "Name oder bestellter Inhalt fehlt."}, 400)
        if not is_valid_iso_date(due_date):
            return self.send_json({"ok": False, "error": "Datum muss im Format JJJJ-MM-TT angegeben werden."}, 400)
        locations = get_locations()
        target_location = None
        normalized_hint = normalize_text_key(location_hint)
        for location in locations:
            if normalize_text_key(location.get("id", "")) == normalized_hint or normalize_text_key(location.get("name", "")) == normalized_hint:
                target_location = location
                break
        if not target_location:
            for location in locations:
                if is_production_location(location):
                    target_location = location
                    break
        if not target_location:
            return self.send_json({"ok": False, "error": "Standort Produktion wurde nicht gefunden."}, 404)
        job = {
            "id": make_cockpit_item_id("production"),
            "section": section,
            "title": title,
            "customer_name": customer_name,
            "order_text": order_text,
            "due_date": due_date,
            "note": note,
            "active": True,
            "source": str(payload.get("source") or "api").strip()[:40] or "api",
        }
        updated_locations = []
        for location in locations:
            if location["id"] == target_location["id"]:
                jobs = normalize_production_jobs(location.get("production_jobs", []))
                jobs.append(job)
                location = dict(location)
                location["production_jobs"] = normalize_production_jobs(jobs)
            updated_locations.append(location)
        save_locations(updated_locations)
        return self.send_json({"ok": True, "id": job["id"], "location": target_location["name"]})

    def handle_admin_login(self):
        form = self.read_form()
        pin = self.form_value(form, "pin")
        settings = load_settings()
        if verify_pin(pin, settings.get("admin_pin_hash")):
            self.send_response(303)
            self.send_header("Set-Cookie", f"admin={make_admin_token()}; HttpOnly; SameSite=Lax; Path=/")
            self.send_header("Location", "/admin")
            self.end_headers()
        else:
            self.show_admin_login("Falsche PIN.")

    def handle_add_product(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        try:
            bulk_count = int(self.form_value(form, "bulk_add_count", "0") or "0")
        except ValueError:
            bulk_count = 0
        if bulk_count > 0:
            prepared = []
            for index in range(bulk_count):
                name = self.form_value(form, f"bulk_name_{index}").strip()
                category_value = self.form_value(form, f"bulk_category_{index}").strip() or "Allgemein"
                package_size = self.form_value(form, f"bulk_package_size_{index}").strip()
                source = self.form_value(form, f"bulk_source_{index}").strip()
                if not any([name, category_value and category_value != "Allgemein", package_size, source]):
                    continue
                if not (name and package_size and source):
                    continue
                categories = ensure_categories(split_categories(category_value))
                prepared.append((name, category_text(categories), package_size, source))
            if not prepared:
                return self.redirect("/admin/products?error=" + quote_plus("Bitte mindestens eine vollständige Produktzeile ausfüllen."))
            visible_to_text = ALL_LOCATIONS_KEY
            con = db()
            now = datetime.now().isoformat(timespec="seconds")
            for name, category, package_size, source in prepared:
                con.execute(
                    """
                    INSERT INTO products (name, package_size, category, source, visible_to, image_filename, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, package_size, category, source, visible_to_text, None, now),
                )
            con.commit()
            con.close()
            return self.redirect("/admin/products?msg=" + quote_plus(f"{len(prepared)} Produkt(e) angelegt."))
        name = self.form_value(form, "name").strip()
        categories = ensure_categories(form_categories(form, "category"))
        category = category_text(categories)
        package_size = self.form_value(form, "package_size").strip()
        source = self.form_value(form, "source").strip()
        visible_to = [location["id"] for location in get_locations() if self.form_value(form, f"visible_{location['id']}")]
        if not visible_to:
            visible_to = location_ids()
        visible_to_text = ALL_LOCATIONS_KEY if set(visible_to) == set(location_ids()) else ",".join(visible_to)
        image_filename = None
        if "image" in form and isinstance(form["image"], UploadedFile):
            item = form["image"]
            if item.filename and item.content:
                if len(item.content) > MAX_IMAGE_BYTES:
                    return self.redirect("/admin/products?error=" + quote_plus("Das Produktbild ist zu groß. Bitte maximal 6 MB hochladen."))
                image_filename = slug_filename(item.filename)
                with open(os.path.join(UPLOAD_DIR, image_filename), "wb") as f:
                    f.write(item.content)
        if name and package_size and source and categories:
            con = db()
            con.execute(
                """
                INSERT INTO products (name, package_size, category, source, visible_to, image_filename, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, package_size, category, source, visible_to_text, image_filename, datetime.now().isoformat(timespec="seconds")),
            )
            con.commit()
            con.close()
            self.redirect("/admin/products?msg=" + quote_plus("Produkt gespeichert."))
        else:
            self.redirect("/admin/products?error=" + quote_plus("Bitte Produktname, mindestens eine Kategorie, Gebindegröße und Bezugsquelle ausfüllen."))

    def handle_delete_product(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        pid = self.form_value(form, "id")
        con = db()
        con.execute(
            "UPDATE products SET active=0, visible_to=?, deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (NO_LOCATIONS_KEY, berlin_now().isoformat(timespec="seconds"), pid),
        )
        con.commit()
        changed = con.total_changes
        con.close()
        if changed:
            self.redirect("/admin/products?msg=" + quote_plus("Produkt gelöscht. Bereits gespeicherte Bestellungen behalten ihre Produktdaten."))
        else:
            self.redirect("/admin/products?error=" + quote_plus("Produkt wurde nicht gefunden oder war bereits gelöscht."))

    def handle_update_product(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        product_id = self.form_value(form, "id").strip()
        product = get_product(product_id)
        if not product:
            return self.redirect("/admin/products?error=" + quote_plus("Produkt wurde nicht gefunden."))
        name = self.form_value(form, "name").strip()
        categories = ensure_categories(form_categories(form, "category", product_categories(product)))
        category = category_text(categories)
        package_size = self.form_value(form, "package_size").strip()
        source = self.form_value(form, "source").strip()
        active = 1 if self.form_value(form, "active", "1") == "1" else 0
        visible_to = [location["id"] for location in get_locations() if self.form_value(form, f"visible_{location['id']}")]
        if not visible_to:
            visible_to = location_ids()
        visible_to_text = ALL_LOCATIONS_KEY if set(visible_to) == set(location_ids()) else ",".join(visible_to)
        if not (name and package_size and source and categories):
            return self.redirect(f"/admin/edit-product?id={quote_plus(product_id)}&error=" + quote_plus("Bitte Produktname, mindestens eine Kategorie, Gebindegröße und Bezugsquelle ausfüllen."))
        image_filename = product["image_filename"]
        if "image" in form and isinstance(form["image"], UploadedFile):
            item = form["image"]
            if item.filename and item.content:
                if len(item.content) > MAX_IMAGE_BYTES:
                    return self.redirect(f"/admin/edit-product?id={quote_plus(product_id)}&error=" + quote_plus("Das Produktbild ist zu groß. Bitte maximal 6 MB hochladen."))
                image_filename = slug_filename(item.filename)
                with open(os.path.join(UPLOAD_DIR, image_filename), "wb") as f:
                    f.write(item.content)
        con = db()
        con.execute(
            """
            UPDATE products
            SET name=?, category=?, package_size=?, source=?, visible_to=?, image_filename=?, active=?
            WHERE id=?
            """,
            (name, category, package_size, source, visible_to_text, image_filename, active, product_id),
        )
        con.commit()
        con.close()
        self.redirect("/admin/products?msg=" + quote_plus("Produktänderungen gespeichert."))

    def handle_bulk_products(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        product_ids = [key.replace("selected_", "", 1) for key, value in form.items() if key.startswith("selected_") and value]
        product_ids = [pid for pid in product_ids if str(pid).isdigit()]
        if not product_ids:
            return self.redirect("/admin/products?error=" + quote_plus("Bitte zuerst mindestens ein Produkt auswählen."))

        placeholders = ",".join("?" for _ in product_ids)
        action = self.form_value(form, "bulk_action", "update")
        con = db()
        try:
            if action == "delete":
                con.execute(
                    f"UPDATE products SET active=0, visible_to=?, deleted_at=? WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                    [NO_LOCATIONS_KEY, berlin_now().isoformat(timespec="seconds"), *product_ids],
                )
                con.commit()
                return self.redirect("/admin/products?msg=" + quote_plus(f"{con.total_changes} Produkt(e) gelöscht. Bereits gespeicherte Bestellungen behalten ihre Produktdaten."))

            updates = []
            params = []
            bulk_categories = [category for category in get_category_names(True) if self.form_value(form, category_input_name("bulk_category", category))]
            bulk_source = self.form_value(form, "bulk_source").strip()
            bulk_active = self.form_value(form, "bulk_active", "keep")
            visibility_mode = self.form_value(form, "bulk_visibility_mode", "keep")

            if bulk_categories:
                updates.append("category=?")
                params.append(category_text(ensure_categories(bulk_categories)))
            if bulk_source:
                updates.append("source=?")
                params.append(bulk_source)
            if bulk_active in ["0", "1"]:
                updates.append("active=?")
                params.append(1 if bulk_active == "1" else 0)
            if visibility_mode == "all":
                updates.append("visible_to=?")
                params.append(ALL_LOCATIONS_KEY)
            elif visibility_mode == "custom":
                visible_to = [location["id"] for location in get_locations() if self.form_value(form, f"bulk_visible_{location['id']}")]
                if not visible_to:
                    con.close()
                    return self.redirect("/admin/products?error=" + quote_plus("Bitte für die neue Sichtbarkeit mindestens einen Standort auswählen."))
                visible_to_text = ALL_LOCATIONS_KEY if set(visible_to) == set(location_ids()) else ",".join(visible_to)
                updates.append("visible_to=?")
                params.append(visible_to_text)

            if not updates:
                con.close()
                return self.redirect("/admin/products?error=" + quote_plus("Bitte mindestens eine Änderung für die ausgewählten Produkte auswählen."))

            params.extend(product_ids)
            con.execute(f"UPDATE products SET {', '.join(updates)} WHERE id IN ({placeholders}) AND deleted_at IS NULL", params)
            con.commit()
            return self.redirect("/admin/products?msg=" + quote_plus(f"{len(product_ids)} Produkt(e) aktualisiert."))
        finally:
            con.close()

    def handle_combined_order_pdf(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        return_location = self.form_value(form, "return_location").strip()
        return_day = self.form_value(form, "return_day").strip()
        order_ids = [key.replace("order_", "", 1) for key, value in form.items() if key.startswith("order_") and value]
        orders = get_orders_by_ids(order_ids)
        if len(orders) < 1:
            return self.redirect(admin_orders_return_url(error="Bitte mindestens eine Bestellung auswählen.", open_location=return_location, open_day=return_day))
        try:
            pdf_filename = create_combined_order_pdf(orders)
            with open(os.path.join(ORDER_DIR, pdf_filename), "rb") as f:
                store_pdf_file(pdf_filename, f.read())
        except Exception as exc:
            print(f"Gesamtbestellung-PDF fehlgeschlagen: {exc}")
            return self.redirect(admin_orders_return_url(error="Die Gesamtbestellung konnte nicht als PDF erstellt werden.", open_location=return_location, open_day=return_day))
        return self.redirect(admin_orders_return_url(message=f"Gesamtbestellung aus {len(orders)} Bestellung(en) wurde erstellt.", pdf=pdf_filename, open_location=return_location, open_day=return_day))

    def handle_delete_orders(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        return_to = self.form_value(form, "return_to", "/admin/orders").strip()
        return_location = self.form_value(form, "return_location").strip()
        return_day = self.form_value(form, "return_day").strip()
        if return_to not in ["/admin", "/admin/orders"]:
            return_to = "/admin/orders"
        order_ids = [key.replace("order_", "", 1) for key, value in form.items() if key.startswith("order_") and value]
        if not order_ids:
            if return_to == "/admin/orders":
                return self.redirect(admin_orders_return_url(error="Bitte zuerst mindestens eine Bestellung auswählen.", open_location=return_location, open_day=return_day))
            return self.redirect(return_to + "?error=" + quote_plus("Bitte zuerst mindestens eine Bestellung auswählen."))
        deleted_count = delete_orders_by_ids(order_ids)
        if not deleted_count:
            if return_to == "/admin/orders":
                return self.redirect(admin_orders_return_url(error="Die ausgewählte Bestellung wurde nicht gefunden.", open_location=return_location, open_day=return_day))
            return self.redirect(return_to + "?error=" + quote_plus("Die ausgewählte Bestellung wurde nicht gefunden."))
        if return_to == "/admin/orders":
            return self.redirect(admin_orders_return_url(message=f"{deleted_count} Bestellung(en) gelöscht.", open_location=return_location))
        return self.redirect(return_to + "?msg=" + quote_plus(f"{deleted_count} Bestellung(en) gelöscht."))

    def handle_order_status(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        return_location = self.form_value(form, "return_location").strip()
        return_day = self.form_value(form, "return_day").strip()
        order_ids = [key.replace("order_", "", 1) for key, value in form.items() if key.startswith("order_") and value]
        if not order_ids:
            return self.redirect(admin_orders_return_url(error="Bitte zuerst mindestens eine Bestellung auswählen.", open_location=return_location, open_day=return_day))
        completed = self.form_value(form, "completed", "1") == "1"
        changed = set_orders_completed(order_ids, completed=completed)
        if not changed:
            return self.redirect(admin_orders_return_url(error="Die ausgewählte Bestellung wurde nicht gefunden.", open_location=return_location, open_day=return_day))
        message = "Bestellung als erledigt markiert." if completed else "Bestellung wieder geöffnet."
        return self.redirect(admin_orders_return_url(message=message, open_location=return_location, open_day=return_day))

    def handle_order_item_status(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        return_order = self.form_value(form, "return_order").strip()
        return_location = self.form_value(form, "return_location").strip()
        return_target = "/admin/orders"
        if return_location:
            return_target += "?ansicht=" + quote_plus(return_location) if return_location in ("offen", "erledigt") else "?open_location=" + quote_plus(return_location)
        if return_order.isdigit():
            return_target += "#order-" + return_order
        item_ids = [key.replace("item_", "", 1) for key, value in form.items() if key.startswith("item_") and value]
        if not item_ids:
            return self.redirect("/admin/orders?error=" + quote_plus("Bitte zuerst mindestens eine Position auswählen."))
        completed = self.form_value(form, "completed", "1") == "1"
        changed = set_order_items_completed(item_ids, completed=completed)
        if not changed:
            return self.redirect("/admin/orders?error=" + quote_plus("Die ausgewählte Position wurde nicht gefunden."))
        bestellungen_automatisch_erledigen()
        return self.redirect(return_target)

    def handle_delete_order_items(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        return_order = self.form_value(form, "return_order").strip()
        return_location = self.form_value(form, "return_location").strip()
        return_target = "/admin/orders"
        if return_location:
            return_target += "?ansicht=" + quote_plus(return_location) if return_location in ("offen", "erledigt") else "?open_location=" + quote_plus(return_location)
        if return_order.isdigit():
            return_target += "#order-" + return_order
        item_ids = [key.replace("item_", "", 1) for key, value in form.items() if key.startswith("item_") and value]
        if not item_ids:
            return self.redirect("/admin/orders?error=" + quote_plus("Bitte zuerst mindestens eine Position auswählen."))
        changed = delete_order_items_by_ids(item_ids)
        if not changed:
            return self.redirect("/admin/orders?error=" + quote_plus("Die ausgewählte Position wurde nicht gefunden."))
        return self.redirect(return_target)

    def handle_delete_category(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        name = self.form_value(form, "name").strip()
        if name and category_key(name) != category_key("Allgemein"):
            con = db()
            product_rows = con.execute("SELECT category FROM products WHERE deleted_at IS NULL").fetchall()
            product_count = sum(1 for row in product_rows if product_has_category(row["category"], name))
            if product_count:
                con.close()
                return self.redirect("/admin/categories?error=" + quote_plus("Kategorie kann nicht gelöscht werden, solange noch Produkte zugeordnet sind. Bitte Produkte vorher bearbeiten."))
            cur = con.execute(
                "UPDATE categories SET active=0, deleted_at=? WHERE category_key=? AND deleted_at IS NULL",
                (berlin_now().isoformat(timespec="seconds"), category_key(name)),
            )
            con.commit()
            deleted = cur.rowcount
            con.close()
            if not deleted:
                return self.redirect("/admin/categories?error=" + quote_plus("Kategorie wurde nicht gefunden oder war bereits gelöscht."))
        self.redirect("/admin/categories?msg=" + quote_plus("Kategorie gelöscht."))

    def handle_settings(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        settings = load_settings()
        if self.form_value(form, "change_admin_pin"):
            current_pin = self.form_value(form, "current_admin_pin").strip()
            new_pin = self.form_value(form, "new_admin_pin").strip()
            confirm_pin = self.form_value(form, "new_admin_pin_confirm").strip()
            if not verify_pin(current_pin, settings.get("admin_pin_hash")):
                return self.redirect("/admin/settings?error=" + quote_plus("Die aktuelle Admin-PIN ist falsch."))
            if len(new_pin) < 4:
                return self.redirect("/admin/settings?error=" + quote_plus("Die neue Admin-PIN muss mindestens 4 Zeichen haben."))
            if new_pin != confirm_pin:
                return self.redirect("/admin/settings?error=" + quote_plus("Die neue Admin-PIN und die Wiederholung stimmen nicht überein."))
            settings["admin_pin_hash"] = hash_pin(new_pin)
            save_settings(settings)
            return self.redirect("/admin/settings?msg=" + quote_plus("Admin-PIN gespeichert. Bitte beim nächsten Login die neue PIN verwenden."))
        settings["order_email_to"] = self.form_value(form, "order_email_to").strip()
        settings["whatsapp_number"] = self.form_value(form, "whatsapp_number").strip()
        settings["time_export_email"] = self.form_value(form, "time_export_email").strip() or "info@opapeters.de"
        save_settings(settings)
        self.redirect("/admin/settings?msg=" + quote_plus("Einstellungen gespeichert."))

    def handle_employees(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        try:
            count = int(self.form_value(form, "employee_count", "0") or "0")
        except ValueError:
            count = 0
        employees = []
        for index in range(count):
            status = self.form_value(form, f"employee_status_{index}").strip()
            if self.form_value(form, f"employee_remove_{index}") or status == "loeschen":
                continue
            name = self.form_value(form, f"employee_name_{index}").strip()
            if not name:
                continue
            employees.append({
                "name": name,
                "active": (status == "aktiv") if status else bool(self.form_value(form, f"employee_active_{index}")),
                "hourly_wage": parse_decimal(self.form_value(form, f"employee_hourly_wage_{index}")),
                "monthly_hours": parse_decimal(self.form_value(form, f"employee_monthly_hours_{index}")),
            })
        new_name = self.form_value(form, "new_employee_name").strip()
        if new_name:
            employees.append({
                "name": new_name,
                "active": True,
                "hourly_wage": parse_decimal(self.form_value(form, "new_employee_hourly_wage")),
                "monthly_hours": parse_decimal(self.form_value(form, "new_employee_monthly_hours")),
            })
        employees = normalize_time_employees(employees)
        save_time_employees(employees)
        active_count = sum(1 for employee in employees if employee.get("active"))
        self.redirect("/admin/employees?msg=" + quote_plus(f"Personen gespeichert. {active_count} aktive Person(en) verfügbar."))


    def collect_min_stock_items(self, form, prefix):
        try:
            count = int(self.form_value(form, f"{prefix}_count", "0") or "0")
        except ValueError:
            count = 0
        items = []
        for row_index in range(count):
            product_id = self.form_value(form, f"{prefix}_product_{row_index}").strip()
            quantity = self.form_value(form, f"{prefix}_qty_{row_index}").strip()
            if not product_id and not quantity:
                continue
            items.append({"product_id": product_id, "quantity": quantity})
        return normalize_min_stock_items(items)

    def collect_cockpit_tasks(self, form, prefix):
        try:
            count = int(self.form_value(form, f"{prefix}_task_count", "0") or "0")
        except ValueError:
            count = 0
        items = []
        for row_index in range(count):
            title = self.form_value(form, f"{prefix}_task_title_{row_index}").strip()
            if not title:
                continue
            items.append({
                "id": self.form_value(form, f"{prefix}_task_id_{row_index}").strip(),
                "title": title,
                "template": bool(self.form_value(form, f"{prefix}_task_template_{row_index}")),
                "active": bool(self.form_value(form, f"{prefix}_task_active_{row_index}")),
                "image_filename": self.save_cockpit_image_upload(
                    form,
                    f"{prefix}_task_image_{row_index}",
                    self.form_value(form, f"{prefix}_task_image_current_{row_index}").strip(),
                ),
            })
        return normalize_cockpit_tasks(items)

    def collect_cockpit_orders(self, form, prefix):
        try:
            count = int(self.form_value(form, f"{prefix}_order_count", "0") or "0")
        except ValueError:
            count = 0
        items = []
        for row_index in range(count):
            title = self.form_value(form, f"{prefix}_order_title_{row_index}").strip()
            note = self.form_value(form, f"{prefix}_order_note_{row_index}").strip()
            if not title and not note:
                continue
            items.append({
                "id": self.form_value(form, f"{prefix}_order_id_{row_index}").strip(),
                "title": title,
                "note": note,
                "active": bool(self.form_value(form, f"{prefix}_order_active_{row_index}")),
                "image_filename": self.save_cockpit_image_upload(
                    form,
                    f"{prefix}_order_image_{row_index}",
                    self.form_value(form, f"{prefix}_order_image_current_{row_index}").strip(),
                ),
            })
        return normalize_cockpit_orders(items)

    def collect_cockpit_infos(self, form, prefix):
        try:
            count = int(self.form_value(form, f"{prefix}_info_count", "0") or "0")
        except ValueError:
            count = 0
        items = []
        for row_index in range(count):
            title = self.form_value(form, f"{prefix}_info_title_{row_index}").strip()
            text = self.form_value(form, f"{prefix}_info_text_{row_index}").strip()
            if not title and not text:
                continue
            items.append({
                "id": self.form_value(form, f"{prefix}_info_id_{row_index}").strip(),
                "title": title,
                "text": text,
                "active": bool(self.form_value(form, f"{prefix}_info_active_{row_index}")),
                "image_filename": self.save_cockpit_image_upload(
                    form,
                    f"{prefix}_info_image_{row_index}",
                    self.form_value(form, f"{prefix}_info_image_current_{row_index}").strip(),
                ),
            })
        return normalize_cockpit_infos(items)


    def save_cockpit_image_upload(self, form, field_name, current_filename=""):
        current_filename = os.path.basename(str(current_filename or "").strip())
        uploaded = form.get(field_name)
        if not isinstance(uploaded, UploadedFile) or not uploaded.filename or not uploaded.content:
            return current_filename
        if len(uploaded.content) > MAX_IMAGE_BYTES:
            raise RequestTooLarge("Das Bild ist zu groß. Bitte maximal 6 MB hochladen.")
        image_filename = slug_filename(uploaded.filename)
        with open(os.path.join(UPLOAD_DIR, image_filename), "wb") as f:
            f.write(uploaded.content)
        return image_filename


    def collect_production_jobs(self, form, prefix):
        try:
            count = int(self.form_value(form, f"{prefix}_production_count", "0") or "0")
        except ValueError:
            count = 0
        items = []
        for row_index in range(count):
            customer_name = self.form_value(form, f"{prefix}_production_customer_name_{row_index}").strip()
            legacy_title = self.form_value(form, f"{prefix}_production_title_{row_index}").strip()
            order_text = self.form_value(form, f"{prefix}_production_order_text_{row_index}").strip()
            due_date = self.form_value(form, f"{prefix}_production_due_date_{row_index}").strip()
            section = self.form_value(form, f"{prefix}_production_section_{row_index}").strip()
            note = self.form_value(form, f"{prefix}_production_note_{row_index}").strip()
            if not customer_name and not legacy_title and not order_text and not due_date and not note:
                continue
            items.append({
                "id": self.form_value(form, f"{prefix}_production_id_{row_index}").strip(),
                "section": section,
                "title": customer_name or legacy_title or order_text,
                "customer_name": customer_name or legacy_title,
                "order_text": order_text,
                "due_date": due_date,
                "note": note,
                "active": bool(self.form_value(form, f"{prefix}_production_active_{row_index}")),
                "source": "admin",
                "image_filename": self.save_cockpit_image_upload(
                    form,
                    f"{prefix}_production_image_{row_index}",
                    self.form_value(form, f"{prefix}_production_image_current_{row_index}").strip(),
                ),
            })
        return normalize_production_jobs(items)


    def handle_admin_cockpit_content(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        requested_open_location = self.form_value(form, "open_location").strip()
        try:
            location_count = int(self.form_value(form, "location_count", "0") or "0")
        except ValueError:
            location_count = 0
        existing_locations = get_locations()
        locations_by_id = {location["id"]: location for location in existing_locations}
        updated_locations = []
        for index in range(location_count):
            location_id = self.form_value(form, f"location_id_{index}").strip()
            location = locations_by_id.get(location_id)
            if not location:
                continue
            prefix = f"cockpit_content_{index}"
            updated = dict(location)
            updated["cockpit_tasks"] = self.collect_cockpit_tasks(form, prefix)
            updated["cockpit_orders"] = self.collect_cockpit_orders(form, prefix)
            updated["cockpit_infos"] = self.collect_cockpit_infos(form, prefix)
            updated["production_jobs"] = self.collect_production_jobs(form, prefix)
            updated_locations.append(updated)
        updated_by_id = {location["id"]: location for location in updated_locations}
        final_locations = [updated_by_id.get(location["id"], location) for location in existing_locations]
        save_locations(normalize_locations(final_locations))
        valid_location_ids = {location["id"] for location in final_locations}
        target = "/admin/cockpit-content?msg=" + quote_plus("Aufträge und Aufgaben gespeichert.")
        if requested_open_location in valid_location_ids:
            target += "&location=" + quote_plus(requested_open_location)
            target += "#cockpit-content-" + quote_plus(requested_open_location)
        self.redirect(target)


    def handle_locations(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        requested_open_location = self.form_value(form, "open_location").strip()
        try:
            location_count = int(self.form_value(form, "location_count", "0") or "0")
        except ValueError:
            location_count = 0
        locations = []
        existing_locations_by_id = {location["id"]: location for location in get_locations()}
        existing_category_updates = {}
        categories = get_category_names(True)
        for index in range(location_count):
            if self.form_value(form, f"location_remove_{index}"):
                continue
            name = self.form_value(form, f"location_name_{index}").strip()
            if not name:
                continue
            location_id = self.form_value(form, f"location_id_{index}").strip() or make_location_id(name)
            existing_location = existing_locations_by_id.get(location_id, {})
            locations.append(
                {
                    "id": location_id,
                    "name": name,
                    "role": self.form_value(form, f"location_role_{index}", "standard").strip(),
                    "contact_name": self.form_value(form, f"location_contact_name_{index}").strip(),
                    "password": self.form_value(form, f"location_password_{index}").strip(),
                    "time_tracking_enabled": bool(self.form_value(form, f"location_time_tracking_{index}")),
                    "time_tracking_max_end": self.form_value(form, f"location_time_tracking_max_end_{index}").strip(),
                    "min_stock_enabled": bool(self.form_value(form, f"location_min_stock_enabled_{index}")),
                    "min_stock_items": self.collect_min_stock_items(form, f"location_min_stock_{index}"),
                    "cockpit_tasks": existing_location.get("cockpit_tasks", []),
                    "cockpit_orders": existing_location.get("cockpit_orders", []),
                    "cockpit_infos": existing_location.get("cockpit_infos", []),
                    "production_jobs": existing_location.get("production_jobs", []),
                }
            )
            existing_category_updates[location_id] = [
                category for category in categories
                if self.form_value(form, f"location_category_{index}_{category}")
            ]
        new_name = self.form_value(form, "new_location_name").strip()
        new_location_categories = [
            category for category in categories
            if self.form_value(form, f"new_location_category_{category}")
        ]
        if new_name:
            locations.append(
                {
                    "id": make_location_id(new_name),
                    "name": new_name,
                    "role": self.form_value(form, "new_location_role", "standard").strip(),
                    "contact_name": self.form_value(form, "new_location_contact_name").strip(),
                    "password": self.form_value(form, "new_location_password").strip(),
                    "time_tracking_enabled": bool(self.form_value(form, "new_location_time_tracking")),
                    "time_tracking_max_end": self.form_value(form, "new_location_time_tracking_max_end").strip(),
                    "min_stock_enabled": bool(self.form_value(form, "new_location_min_stock_enabled")),
                    "min_stock_items": self.collect_min_stock_items(form, "new_location_min_stock"),
                    "cockpit_tasks": [],
                    "cockpit_orders": [],
                    "cockpit_infos": [],
                    "production_jobs": [],
                }
            )
        if not locations:
            return self.redirect("/admin/locations?msg=" + quote_plus("Bitte mindestens einen Standort anlegen."))
        normalized_locations = normalize_locations(locations)
        new_location_id = normalized_locations[-1]["id"] if new_name and normalized_locations else ""
        save_locations(normalized_locations)
        valid_location_ids = {location["id"] for location in normalized_locations}
        if requested_open_location not in valid_location_ids:
            requested_open_location = new_location_id if new_location_id else ""
        for location_id, selected_categories in existing_category_updates.items():
            if location_id in valid_location_ids:
                apply_category_visibility_for_location(location_id, selected_categories)
        if new_location_id:
            apply_category_visibility_for_location(new_location_id, new_location_categories)
        target = "/admin/locations?msg=" + quote_plus("Standorte gespeichert.")
        if requested_open_location:
            target += "&open_location=" + quote_plus(requested_open_location)
            target += "#location-" + quote_plus(requested_open_location)
        self.redirect(target)

    def handle_visibility(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        location_id = self.form_value(form, "location_id").strip()
        if not find_location(location_id):
            return self.redirect("/admin/visibility?error=" + quote_plus("Bitte einen gültigen Standort auswählen."))
        selected_products = {
            key.replace("product_", "", 1)
            for key, value in form.items()
            if key.startswith("product_") and value
        }
        selected_categories = {
            key.replace("category_", "", 1)
            for key, value in form.items()
            if key.startswith("category_") and value
        }
        con = db()
        rows = con.execute("SELECT id, category, visible_to FROM products WHERE deleted_at IS NULL").fetchall()
        updated = 0
        for row in rows:
            visible = product_visible_location_keys(row["visible_to"])
            should_show = str(row["id"]) in selected_products or any(category in selected_categories for category in product_categories(row))
            if should_show and location_id not in visible:
                visible.append(location_id)
                updated += 1
            elif not should_show and location_id in visible:
                visible = [key for key in visible if key != location_id]
                updated += 1
            con.execute("UPDATE products SET visible_to=? WHERE id=?", (store_visible_locations(visible), row["id"]))
        con.commit()
        con.close()
        text = f"Sortiment gespeichert. {updated} Änderung(en)."
        return self.redirect("/admin/location?id=" + quote_plus(location_id) + "&tab=sortiment&msg=" + quote_plus(text))

    def handle_time_entry(self):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        location = find_location(buyer_key)
        if not location or not location_can_time(location):
            return self.redirect("/")
        form = self.read_form()
        employee_name = self.form_value(form, "employee_name").strip()
        work_location = self.form_value(form, "work_location").strip()
        start_time = self.form_value(form, "start_time").strip()
        end_time = self.form_value(form, "end_time").strip()
        note = self.form_value(form, "note").strip()
        allowed_employees = {normalize_text_key(name) for name in get_time_employee_names(True)}
        if not allowed_employees:
            return self.show_time_form("Bitte zuerst im Adminbereich Personen für die Zeiterfassung anlegen.")
        if normalize_text_key(employee_name) not in allowed_employees:
            return self.show_time_form("Bitte eine angelegte Person aus der Liste auswählen.")
        duration, error = validate_time_entry(location, employee_name, work_location, start_time, end_time, admin=False)
        if error:
            return self.show_time_form(error)
        if find_duplicate_time_entry(employee_name, today_iso(), start_time, end_time):
            return self.show_time_form("Schicht bereits registriert.")
        now = berlin_now().strftime("%d.%m.%Y %H:%M")
        con = db()
        con.execute(
            """
            INSERT INTO time_entries (location_id, location_name, employee_name, work_location, work_date, start_time, end_time, duration_minutes, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (buyer_key, location["name"], employee_name, work_location, today_iso(), start_time, end_time, duration, note, now),
        )
        con.commit()
        con.close()
        send_time_entry_push(employee_name, work_location, today_iso(), start_time, end_time, duration, note, created_by_admin=False)
        order_back_button = '<a class="button primary" href="/">Zurück zum Bestellsystem</a>' if location_can_order(location) else ""
        body = f"""
        <section class="box narrow success">
            <h2>Zeiterfassung gespeichert</h2>
            <p>Deine Zeiterfassung wurde erfolgreich gespeichert.</p>
            <p>Wir wünschen dir einen schönen Feierabend.</p>
            <p>{esc(farewell_message())}</p>
            <p>{order_back_button} <a class="button" href="/time">Weitere Zeit erfassen</a></p>
        </section>
        """
        self.send_html(page("Zeiterfassung gespeichert", body, buyer_key=buyer_key))

    def handle_create_time_entry(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        location_id = self.form_value(form, "location_id").strip()
        location = find_location(location_id)
        if not location:
            return self.redirect("/admin/time?error=" + quote_plus("Bitte einen gültigen Standort auswählen."))
        employee_name = self.form_value(form, "employee_name").strip()
        active_employees = {normalize_text_key(name) for name in get_time_employee_names(active_only=True)}
        if not active_employees:
            return self.redirect("/admin/time?error=" + quote_plus("Bitte zuerst unter Personen mindestens eine aktive Person anlegen."))
        if normalize_text_key(employee_name) not in active_employees:
            return self.redirect("/admin/time?error=" + quote_plus("Bitte eine aktive Person aus der Liste auswählen."))
        try:
            shift_count = min(max(int(self.form_value(form, "shift_count", "0") or 0), 1), 50)
        except ValueError:
            shift_count = 0
        work_location = location["name"]
        shift_rows = []
        first_month = current_month()
        for index in range(shift_count):
            work_date = self.form_value(form, f"shift_date_{index}").strip()
            start_time = self.form_value(form, f"shift_start_{index}").strip()
            end_time = self.form_value(form, f"shift_end_{index}").strip()
            note = self.form_value(form, f"shift_note_{index}").strip()
            if not any([start_time, end_time, note]):
                continue
            row_number = index + 1
            if not work_date or not start_time or not end_time:
                return self.redirect("/admin/time?error=" + quote_plus(f"Zeile {row_number}: Bitte Datum, Anfang und Ende ausfüllen oder die Zeile leer lassen."))
            try:
                datetime.strptime(work_date, "%Y-%m-%d")
            except ValueError:
                return self.redirect("/admin/time?error=" + quote_plus(f"Zeile {row_number}: Bitte ein gültiges Datum verwenden."))
            if not is_valid_hhmm(start_time):
                return self.redirect(f"/admin/time?month={quote_plus(work_date[:7])}&error=" + quote_plus(f"Zeile {row_number}: Bitte die Anfangszeit in 15-Minuten-Schritten auswählen."))
            duration, error = validate_time_entry(location, employee_name, work_location, start_time, end_time, admin=True)
            first_month = work_date[:7] if not shift_rows else first_month
            if error:
                return self.redirect(f"/admin/time?month={quote_plus(work_date[:7])}&error=" + quote_plus(f"Zeile {row_number}: {error}"))
            for existing_date, existing_start, existing_end, _existing_duration, _existing_note in shift_rows:
                if existing_date == work_date:
                    existing_start_minutes = minutes_from_clock_time(existing_start)
                    existing_end_minutes = minutes_from_hhmm(existing_end)
                    start_minutes = minutes_from_clock_time(start_time)
                    end_minutes = minutes_from_hhmm(end_time)
                    if existing_start_minutes is not None and existing_end_minutes is not None and start_minutes is not None and end_minutes is not None:
                        if existing_start_minutes < end_minutes and start_minutes < existing_end_minutes:
                            return self.redirect(f"/admin/time?month={quote_plus(work_date[:7])}&error=" + quote_plus(f"Zeile {row_number}: Schicht bereits registriert."))
            if find_duplicate_time_entry(employee_name, work_date, start_time, end_time):
                return self.redirect(f"/admin/time?month={quote_plus(work_date[:7])}&error=" + quote_plus(f"Zeile {row_number}: Schicht bereits registriert."))
            shift_rows.append((work_date, start_time, end_time, duration, note))
        if not shift_rows:
            return self.redirect("/admin/time?error=" + quote_plus("Bitte mindestens eine Schicht vollständig ausfüllen."))
        now = berlin_now().strftime("%d.%m.%Y %H:%M")
        con = db()
        for work_date, start_time, end_time, duration, note in shift_rows:
            con.execute(
                """
                INSERT INTO time_entries (location_id, location_name, employee_name, work_location, work_date, start_time, end_time, duration_minutes, note, created_at, updated_at, edited)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (location_id, location["name"], employee_name, work_location, work_date, start_time, end_time, duration, note, now, now),
            )
        con.commit()
        con.close()
        for work_date, start_time, end_time, duration, note in shift_rows:
            send_time_entry_push(employee_name, work_location, work_date, start_time, end_time, duration, note, created_by_admin=True)
        message = "Eine Schicht wurde manuell nachgetragen." if len(shift_rows) == 1 else f"{len(shift_rows)} Schichten wurden manuell nachgetragen."
        self.redirect(f"/admin/time?month={quote_plus(first_month)}&msg=" + quote_plus(message))

    def handle_time_settings(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        month = self.form_value(form, "month", current_month()).strip()
        if not re.match(r"^\d{4}-\d{2}$", month):
            month = current_month()
        locations = get_locations()
        for location in locations:
            value = self.form_value(form, f"time_limit_{location['id']}").strip()
            if value and not is_valid_hhmm(value):
                return self.redirect(
                    f"/admin/time?month={quote_plus(month)}&error="
                    + quote_plus(f"Bitte für {location['name']} eine gültige Uhrzeit in 15-Minuten-Schritten auswählen.")
                )
            location["time_tracking_max_end"] = value
        save_locations(locations)
        return self.redirect(
            f"/admin/time?month={quote_plus(month)}&msg="
            + quote_plus("Die maximalen Endzeiten wurden gespeichert.")
        )

    def handle_update_time_entry(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        entry_id = self.form_value(form, "id").strip()
        entry = time_entry_by_id(entry_id)
        if not entry:
            return self.redirect("/admin/time?error=" + quote_plus("Zeiteintrag wurde nicht gefunden."))
        work_date = self.form_value(form, "work_date").strip()
        try:
            datetime.strptime(work_date, "%Y-%m-%d")
        except ValueError:
            return self.redirect("/admin/time?error=" + quote_plus("Bitte ein gültiges Datum verwenden."))
        employee_name = self.form_value(form, "employee_name").strip()
        work_location = self.form_value(form, "work_location").strip()
        start_time = self.form_value(form, "start_time").strip()
        end_time = self.form_value(form, "end_time").strip()
        note = self.form_value(form, "note").strip()
        duration, error = validate_time_entry(find_location(entry["location_id"]), employee_name, work_location, start_time, end_time, admin=True)
        month = work_date[:7]
        if error:
            return self.redirect(f"/admin/time?month={quote_plus(month)}&error=" + quote_plus(error))
        if find_duplicate_time_entry(employee_name, work_date, start_time, end_time, exclude_id=entry_id):
            return self.redirect(f"/admin/time?month={quote_plus(month)}&error=" + quote_plus("Schicht bereits registriert."))
        updated_at = berlin_now().strftime("%d.%m.%Y %H:%M")
        con = db()
        con.execute(
            """
            UPDATE time_entries
            SET employee_name=?, work_location=?, work_date=?, start_time=?, end_time=?, duration_minutes=?, note=?, updated_at=?, edited=1
            WHERE id=?
            """,
            (employee_name, work_location, work_date, start_time, end_time, duration, note, updated_at, entry_id),
        )
        con.commit()
        con.close()
        self.redirect(f"/admin/time?month={quote_plus(month)}&msg=" + quote_plus("Zeiteintrag gespeichert. Die Stunden wurden neu berechnet."))

    def handle_delete_time_entry(self):
        if not self.is_admin():
            return self.redirect("/admin/login")
        form = self.read_form()
        entry_id = self.form_value(form, "id").strip()
        month = self.form_value(form, "month", current_month()).strip()
        if not re.match(r"^\d{4}-\d{2}$", month):
            month = current_month()
        if not entry_id.isdigit():
            return self.redirect(f"/admin/time?month={quote_plus(month)}&error=" + quote_plus("Ungültiger Zeiteintrag."))
        con = db()
        cur = con.execute("DELETE FROM time_entries WHERE id=?", (entry_id,))
        con.commit()
        deleted = cur.rowcount
        con.close()
        if deleted:
            return self.redirect(f"/admin/time?month={quote_plus(month)}&msg=" + quote_plus("Zeiteintrag gelöscht."))
        return self.redirect(f"/admin/time?month={quote_plus(month)}&error=" + quote_plus("Zeiteintrag wurde nicht gefunden."))

    def handle_order(self):
        buyer_key = self.current_buyer_key()
        if not buyer_key:
            return self.redirect("/login")
        form = self.read_form()
        location_id = buyer_key
        location_data = find_location(location_id)
        if not location_can_order(location_data):
            return self.redirect("/time" if location_can_time(location_data) else "/choose")
        location = location_data["name"] if location_data else ""
        ordered_by = self.form_value(form, "ordered_by").strip()
        note = self.form_value(form, "note").strip()
        if not location or not ordered_by:
            return self.show_order_form("Bitte Standort und Namen des Bestellers ausfüllen.")

        products = get_products(True, buyer_key=buyer_key)
        items = []
        for p in products:
            raw = self.form_value(form, f"qty_{p['id']}", "0").strip() or "0"
            try:
                qty = int(raw)
            except ValueError:
                qty = 0
            qty = min(qty, MAX_ORDER_QUANTITY)
            if qty > 0:
                items.append(
                    {
                        "product_id": p["id"],
                        "product_name": p["name"],
                        "package_size": p["package_size"],
                        "category": category_text(product_categories(p)),
                        "source": p["source"],
                        "quantity": qty,
                    }
                )
        if not items:
            return self.show_order_form("Bitte bei mindestens einem Produkt eine Menge eintragen.")

        order_image_filename = None
        for image_field in ["order_image", "order_image_camera", "order_image_gallery"]:
            uploaded_image = form.get(image_field)
            if isinstance(uploaded_image, UploadedFile) and uploaded_image.filename and uploaded_image.content:
                if len(uploaded_image.content) > MAX_ORDER_IMAGE_BYTES:
                    return self.show_order_form("Das Bild zur Bestellung ist zu groß. Bitte maximal 10 MB hochladen.")
                order_image_filename = slug_filename(uploaded_image.filename)
                with open(os.path.join(ORDER_IMAGE_DIR, order_image_filename), "wb") as f:
                    f.write(uploaded_image.content)
                break

        order_number = berlin_now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()
        created_at = berlin_now().strftime("%d.%m.%Y %H:%M")
        buyer_group = location
        order = {
            "order_number": order_number,
            "location": location,
            "ordered_by": ordered_by,
            "buyer_group": buyer_group,
            "note": note,
            "order_image_filename": order_image_filename,
            "created_at": created_at,
        }
        try:
            pdf_filename = create_pdf(order, items)
            with open(os.path.join(ORDER_DIR, pdf_filename), "rb") as f:
                pdf_data = f.read()
            store_pdf_file(pdf_filename, pdf_data)
        except Exception as exc:
            print(f"PDF-Erstellung fehlgeschlagen: {exc}")
            return self.show_order_form("Die PDF zur Bestellung konnte nicht erstellt werden. Bitte prüfe die Eingaben und versuche es erneut.")
        # Keine Mail pro Bestellung mehr – die Bestellungen kommen gesammelt um 6 Uhr am nächsten Morgen (tagesmail_uhr_starten).
        con = db()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO orders (order_number, location, ordered_by, buyer_group, note, pdf_filename, pdf_data, order_image_filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_number, location, ordered_by, buyer_group, note, pdf_filename, pdf_data, order_image_filename, created_at),
        )
        order_id = cur.lastrowid
        for item in items:
            cur.execute(
                "INSERT INTO order_items (order_id, product_id, product_name, package_size, category, source, quantity) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, item["product_id"], item["product_name"], item["package_size"], item["category"], item["source"], item["quantity"]),
            )
        con.commit()
        con.close()
        try:
            send_order_push(order_number, location, ordered_by, items, note)
        except Exception as exc:
            print(f"Push-Benachrichtigung für Bestellung fehlgeschlagen: {exc}")
        delete_cart_draft(location_id)
        order_for_links = dict(order)
        order_for_links["pdf_filename"] = pdf_filename
        order_for_links["base_url"] = self.base_url()
        wa_link = whatsapp_order_link(order_for_links)
        whatsapp_button = f'<a class="button" target="_blank" rel="noopener" href="{esc(wa_link)}">Per WhatsApp senden</a>' if wa_link else ''
        cart_cleanup_script = f"""
        <script>
        (function () {{
          var locationKey = {json.dumps(location_id)};
          try {{
            window.localStorage.removeItem('opaPetersCart:' + locationKey);
            window.localStorage.removeItem('opaPetersCartBackup:' + locationKey);
          }} catch (error) {{}}
          try {{
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/cart-draft', true);
            xhr.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
            xhr.send(JSON.stringify({{ action: 'clear' }}));
          }} catch (error) {{}}
        }})();
        </script>
        """
        body = f"""
        <section class="box narrow success">
            <h2>Bestellung gesendet</h2>
            <p>Die Bestellung <strong>{esc(order_number)}</strong> wurde erstellt.</p>
            <p>Deine Bestellung wurde erfolgreich übermittelt. Vielen Dank.</p>
            {f'<p>Das Bild zur Bestellung wurde mitgespeichert.</p>' if order_image_filename else ''}
            <p>Die PDF wurde erstellt und kann direkt geöffnet oder gedruckt werden.</p>
            <p><a class="button" href="/">Neue Bestellung erfassen</a> <a class="button primary" href="{esc(pdf_viewer_href(pdf_filename))}">PDF öffnen / drucken</a> {whatsapp_button}</p>
        </section>{cart_cleanup_script}"""
        self.send_html(page("Bestellung gesendet", body, buyer_key=buyer_key))


def main():
    init_db()
    bh.init(DATA_DIR)
    bh.nachtlauf_starten(taeglich=bh_monatserinnerung)
    tagesmail_uhr_starten()
    kasse_init()
    bh.MAIL_HAKEN.append(kasse_mail_haken)
    tagesabschluss_uhr_starten()
    seed_demo_products()
    print(f"Internes Bestellsystem läuft auf http://localhost:{PORT}")
    print(f"Admin-PIN: {ADMIN_PIN} (vor Onlinebetrieb ändern)")
    print("Standort-Zugänge werden im Adminbereich unter Standorte gepflegt.")
    HTTPServer((HOST, PORT), App).serve_forever()


if __name__ == "__main__":
    main()

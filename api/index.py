import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Vercel setzt VERCEL automatisch. Diese Zeile macht lokale Tests der Function leichter.
os.environ.setdefault("VERCEL", "1")
os.environ.setdefault("HOST", "0.0.0.0")

from app import App, init_db, seed_demo_products

# Bei jedem Cold Start sicherstellen, dass Tabellen vorhanden sind.
init_db()
seed_demo_products()

handler = App

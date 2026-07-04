import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import App, init_db, seed_demo_products

# Datenbank beim Start der Vercel Function vorbereiten.
init_db()
seed_demo_products()

class handler(App):
    pass

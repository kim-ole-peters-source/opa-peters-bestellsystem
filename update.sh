#!/bin/bash
# Aktualisiert das Bestellsystem auf dem Server.
#   ./update.sh            neuen Stand einspielen
#   ./update.sh zurueck    letzten Schritt rückgängig machen
#
# Macht vorher immer eine Sicherung und prüft hinterher, ob der Dienst läuft.

set -u
APP="/opt/opa-peters/app"
DATEN="/var/lib/opa-peters"
SICHER="$DATEN/sicherungen"
DIENST="opa-peters"
BEHALTEN=20
MERKER="$DATEN/.letzter_stand"

rot()  { printf "\033[31m%s\033[0m\n" "$*"; }
gruen(){ printf "\033[32m%s\033[0m\n" "$*"; }
info() { printf "\033[1m%s\033[0m\n" "$*"; }

if [ "$(id -u)" != "0" ]; then rot "Bitte als root ausführen."; exit 1; fi
cd "$APP" || { rot "Ordner $APP nicht gefunden."; exit 1; }

sichern() {
  mkdir -p "$SICHER"
  local stempel; stempel="$(date +%Y-%m-%d_%H-%M-%S)"
  cp "$DATEN/bestellsystem.db" "$SICHER/bestellsystem_$stempel.db" 2>/dev/null
  cp "$DATEN/settings.json"    "$SICHER/settings_$stempel.json"    2>/dev/null
  gruen "Sicherung angelegt: $SICHER/bestellsystem_$stempel.db"
  ls -1t "$SICHER"/bestellsystem_*.db 2>/dev/null | tail -n +$((BEHALTEN+1)) | xargs -r rm -f
  ls -1t "$SICHER"/settings_*.json    2>/dev/null | tail -n +$((BEHALTEN+1)) | xargs -r rm -f
}

starten() {
  systemctl restart "$DIENST"
  sleep 2
  if systemctl is-active --quiet "$DIENST"; then
    gruen "Dienst läuft."
    return 0
  fi
  rot "Dienst läuft NICHT. Die letzten Zeilen aus dem Protokoll:"
  journalctl -u "$DIENST" -n 25 --no-pager
  return 1
}

# ---------- zurück ----------
if [ "${1:-}" = "zurueck" ]; then
  if [ ! -f "$MERKER" ]; then rot "Kein vorheriger Stand gemerkt."; exit 1; fi
  ALT="$(cat "$MERKER")"
  info "Setze zurück auf $ALT"
  sichern
  git reset --hard "$ALT" || { rot "Zurücksetzen fehlgeschlagen."; exit 1; }
  source .venv/bin/activate && pip install -q -r requirements.txt
  starten && gruen "Zurückgesetzt. Alles wieder wie vorher." || exit 1
  exit 0
fi

# ---------- aktualisieren ----------
info "1/5  Lokale Änderungen prüfen"
if [ -n "$(git status --porcelain -- . ':!__pycache__')" ]; then
  rot "Auf dem Server liegen ungesicherte Änderungen:"
  git status --short -- . ':!__pycache__'
  rot "Bitte erst klären, sonst gehen sie verloren. Abbruch."
  exit 1
fi
gruen "Keine offenen Änderungen."

info "2/5  Sicherung"
sichern
git rev-parse HEAD > "$MERKER"

info "3/5  Neuen Stand holen"
if ! git pull --ff-only; then
  rot "Konnte den neuen Stand nicht holen. Nichts verändert."
  exit 1
fi

info "4/5  Pakete und Programmprüfung"
source .venv/bin/activate || { rot "Python-Umgebung nicht gefunden."; exit 1; }
pip install -q -r requirements.txt
if ! python -m py_compile app.py; then
  rot "Das Programm hat einen Fehler und würde nicht starten."
  rot "Ich setze zurück auf den alten Stand."
  git reset --hard "$(cat "$MERKER")"
  starten
  exit 1
fi
gruen "Programm ist in Ordnung."

info "5/5  Neustart"
if starten; then
  echo
  gruen "Fertig. Der neue Stand ist live."
  echo "Eingespielt: $(git log -1 --pretty='%s')"
  echo
  echo "Falls doch etwas nicht stimmt:  $APP/update.sh zurueck"
else
  echo
  rot "Der Dienst startet nicht. Ich setze auf den alten Stand zurück."
  git reset --hard "$(cat "$MERKER")"
  starten && gruen "Alter Stand läuft wieder." || rot "Bitte melden – der Dienst läuft auch mit dem alten Stand nicht."
  exit 1
fi

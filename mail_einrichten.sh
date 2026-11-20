#!/bin/bash
# E-Mail-Versand für das Bestellsystem einrichten (Strato).
# Aufruf auf dem Server:  cd /opt/opa-peters/app && ./mail_einrichten.sh
# Das Passwort wird nur auf dem Server gespeichert (nur für root lesbar).

set -u
if [ "$(id -u)" != "0" ]; then
  echo "Bitte als root ausführen (so wie beim Update)."
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
ZIEL_DIR="/etc/opa-peters"
ZIEL="$ZIEL_DIR/mail.env"

echo ""
echo "=== E-Mail-Versand einrichten ==="
echo "Die Mails werden über dein Strato-Postfach verschickt."
echo ""
read -r -p "Absender-Adresse (ein Strato-Postfach, z. B. bestellung@opapeters.de): " ADRESSE
ADRESSE="$(echo "$ADRESSE" | tr -d '[:space:]')"
if [[ "$ADRESSE" != *@*.* ]]; then
  echo "Das sieht nicht wie eine E-Mail-Adresse aus. Abgebrochen."
  exit 1
fi
read -r -s -p "Passwort dieses Postfachs (man sieht beim Tippen nichts): " PASSWORT
echo ""
if [ -z "$PASSWORT" ]; then
  echo "Kein Passwort eingegeben. Abgebrochen."
  exit 1
fi

echo ""
echo "Prüfe die Anmeldung bei Strato ..."
if ! SMTP_TEST_USER="$ADRESSE" SMTP_TEST_PASSWORD="$PASSWORT" "$PY" - <<'PYEND'
import os, smtplib, sys
try:
    with smtplib.SMTP_SSL("smtp.strato.de", 465, timeout=30) as s:
        s.login(os.environ["SMTP_TEST_USER"], os.environ["SMTP_TEST_PASSWORD"])
except smtplib.SMTPAuthenticationError:
    print("  -> Strato lehnt Adresse oder Passwort ab.")
    sys.exit(1)
except Exception as e:
    print(f"  -> Keine Verbindung zu Strato: {e}")
    sys.exit(1)
print("  -> Anmeldung klappt.")
PYEND
then
  echo ""
  echo "Nichts wurde geändert. Bitte Adresse und Passwort prüfen und noch einmal starten."
  exit 1
fi

# Werte in einfachen Anführungszeichen speichern (Sonderzeichen bleiben, wie sie sind)
zitat() {
  local wert="$1"
  if [[ "$wert" == *"'"* ]]; then
    wert="${wert//\\/\\\\}"; wert="${wert//\"/\\\"}"
    printf '"%s"' "$wert"
  else
    printf "'%s'" "$wert"
  fi
}

mkdir -p "$ZIEL_DIR"
umask 077
{
  echo "SMTP_HOST='smtp.strato.de'"
  echo "SMTP_PORT='465'"
  echo "SMTP_USER=$(zitat "$ADRESSE")"
  echo "SMTP_PASSWORD=$(zitat "$PASSWORT")"
  echo "SMTP_FROM=$(zitat "$ADRESSE")"
} > "$ZIEL"
chmod 600 "$ZIEL"

mkdir -p /etc/systemd/system/opa-peters.service.d
printf '[Service]\nEnvironmentFile=%s\n' "$ZIEL" > /etc/systemd/system/opa-peters.service.d/mail.conf
systemctl daemon-reload
systemctl restart opa-peters
sleep 2

if systemctl is-active --quiet opa-peters; then
  echo ""
  echo "Fertig. Der E-Mail-Versand ist eingerichtet und das Programm läuft."
  echo "Jetzt im Browser: Verwaltung > Einstellungen > 'Test-Mail senden'."
else
  echo ""
  echo "Achtung: Das Programm startet nicht. Ich nehme die Mail-Einstellung wieder heraus ..."
  rm -f /etc/systemd/system/opa-peters.service.d/mail.conf
  systemctl daemon-reload
  systemctl restart opa-peters
  echo "Alter Zustand wiederhergestellt. Bitte die Ausgabe an Claude schicken."
  exit 1
fi

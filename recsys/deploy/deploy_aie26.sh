#!/usr/bin/env bash
#
# Create / refresh the "aie26" deployment — a second instance of this app running
# beside recsys on its own port (5001), URL path (/aie26), systemd unit (aie26),
# and database (aie26). Run ON the VM as azureuser. Idempotent & secret-free:
# every credential is read from the existing recsys/.env, never hardcoded here.
#
#   ssh -i ~/work/ssh-keys/HB-New_key.pem azureuser@20.127.222.82
#   bash /home/azureuser/work/recsys/deploy/deploy_aie26.sh
#
set -euo pipefail

SRC=/home/azureuser/work/recsys
DST=/home/azureuser/work/aie26
PORT=5001
BASE=/aie26
PUBLIC_BASE=https://support.hostbuddy.io/aie26
DB_NAME=aie26                       # separate schema → isolated from the live recsys data

[ -d "$SRC" ]      || { echo "FATAL: $SRC not found"; exit 1; }
[ -f "$SRC/.env" ] || { echo "FATAL: $SRC/.env not found"; exit 1; }

echo "==> 1/7  Sync code  $SRC -> $DST  (excludes secrets, venv, per-instance assets)"
mkdir -p "$DST"
rsync -a --delete \
  --exclude venv --exclude .env --exclude __pycache__ --exclude '*.pyc' \
  --exclude 'static/audio' --exclude 'static/vip_cards' --exclude 'static/vip_logos' \
  --exclude 'static/menu_images' --exclude data \
  "$SRC"/ "$DST"/

echo "==> 2/7  Python venv + dependencies"
[ -d "$DST/venv" ] || python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" -q install --upgrade pip >/dev/null
"$DST/venv/bin/pip" -q install -r "$DST/requirements.txt"

echo "==> 3/7  Google credentials"
[ -f "$SRC/google-credentials.json" ] && cp -f "$SRC/google-credentials.json" "$DST/" || true

echo "==> 4/7  .env  (copied from recsys, with this deployment's overrides)"
# Reuse the SAME DB host/user/pass as recsys, only swap the schema name to aie26.
SRC_DB_URL=$(grep -E '^DB_URL=' "$SRC/.env" | head -1 | cut -d= -f2-)
[ -n "$SRC_DB_URL" ] || SRC_DB_URL="mysql+pymysql://recsys:recsys2026@localhost/recsys"
DST_DB_URL=$(echo "$SRC_DB_URL" | sed -E "s#/[^/?]+(\?|$)#/${DB_NAME}\1#")
if [ ! -f "$DST/.env" ]; then
  grep -vE '^(BASE_PATH|PUBLIC_BASE_URL|DB_URL|APP_HOME)=' "$SRC/.env" > "$DST/.env"
  {
    echo ""
    echo "# ── aie26 deployment overrides ──"
    echo "BASE_PATH=${BASE}"
    echo "PUBLIC_BASE_URL=${PUBLIC_BASE}"
    echo "DB_URL=${DST_DB_URL}"
    echo "APP_HOME=${DST}"
  } >> "$DST/.env"
  echo "    wrote $DST/.env  (DB schema: ${DB_NAME})"
else
  echo "    $DST/.env exists — left as-is"
fi

echo "==> 5/7  Database '${DB_NAME}'  (create schema if missing; tables auto-create on first start)"
# Parse user/pass/host from the DB_URL to create the schema with the same account.
read -r DB_USER DB_PASS DB_HOST <<<"$("$DST/venv/bin/python" - "$DST_DB_URL" <<'PY'
import sys
from urllib.parse import urlparse
u = urlparse(sys.argv[1])
print(u.username or "", u.password or "", u.hostname or "localhost")
PY
)"
if mysql -h"$DB_HOST" -u"$DB_USER" -p"$DB_PASS" \
     -e "CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` CHARACTER SET utf8mb4;" 2>/dev/null; then
  echo "    schema ${DB_NAME} ready"
else
  echo "    WARN: '${DB_USER}' could not CREATE DATABASE. Create it once as root, then re-run:"
  echo "          mysql -uroot -p -e \"CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4; GRANT ALL ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';\""
fi

echo "==> 6/7  systemd service 'aie26'"
sudo cp "$DST/deploy/aie26.service" /etc/systemd/system/aie26.service
sudo systemctl daemon-reload
sudo systemctl enable aie26 >/dev/null 2>&1 || true
sudo systemctl restart aie26

echo "==> 7/7  Seed menu (same source merchant as recsys) + Apache reverse proxy"
( cd "$DST" && APP_HOME="$DST" ./venv/bin/python scripts/migrate_menu.py ) \
  || echo "    menu seed skipped — run manually if the demo needs a menu"
sudo cp "$DST/deploy/aie26.conf" /etc/apache2/conf-available/aie26.conf
sudo a2enconf aie26 >/dev/null 2>&1 || true
sudo apache2ctl configtest && sudo systemctl reload apache2

echo "==> Health check"
sleep 2
if curl -fsS "http://127.0.0.1:${PORT}${BASE}/api/vip/config" >/dev/null; then
  echo "OK  aie26 is up:  http://127.0.0.1:${PORT}${BASE}   →   ${PUBLIC_BASE}/agentstudio"
else
  echo "WARN backend not answering yet — check:  sudo journalctl -u aie26 -n 60 --no-pager"
fi

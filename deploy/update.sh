#!/usr/bin/env bash
# Preuzimanje nove verzije stranice (evidencija i postavke ostaju). Pokretanje kao root:
#   bash /opt/academiadelavida/deploy/update.sh
set -euo pipefail
APP=/opt/academiadelavida
sudo -u adv git -C "$APP" pull -q
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R adv:adv "$APP"
systemctl restart academiadelavida
sleep 2
curl -s -o /dev/null -w "Stranica odgovara: %{http_code}\n" http://127.0.0.1:8077/

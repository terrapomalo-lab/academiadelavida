#!/usr/bin/env bash
# Jednokratno: isprika svima koji su u podsjetniku 28. 11. dobili rečenicu "Uplatu ... još nismo zaprimili".
#   bash /opt/academiadelavida/deploy/isprika.sh
set -euo pipefail
APP=/opt/academiadelavida
ENVF=/etc/academiadelavida.env
DB=/var/lib/academiadelavida/adv.sqlite3
[ "$(id -u)" = 0 ] || { echo "Pokrenite kao root."; exit 1; }

sudo -u adv python3 "$APP/deploy/isprika.py" list "$DB"
echo
read -r -p "Poslati ispriku ovim osobama? Upišite DA i Enter: " OK </dev/tty
[ "$OK" = "DA" ] || { echo "Ništa nije poslano."; exit 0; }
sudo -u adv python3 "$APP/deploy/isprika.py" send "$DB"
sudo -u adv bash -c "cd $APP && set -a && . $ENVF && set +a && venv/bin/flask --app app send-outbox"
echo "Gotovo. Isprike su poslane."

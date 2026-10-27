#!/usr/bin/env bash
# Prikaz korisničkih imena administracije i postavljanje nove lozinke. Pokretanje kao root:
#   bash /opt/academiadelavida/deploy/admin-password.sh
set -euo pipefail
APP=/opt/academiadelavida
ENVF=/etc/academiadelavida.env
DB=/var/lib/academiadelavida/adv.sqlite3
[ "$(id -u)" = 0 ] || { echo "Pokrenite kao root."; exit 1; }
echo "Postojeća korisnička imena za administraciju:"
sudo -u adv python3 -c "
import sqlite3
for (u,) in sqlite3.connect('$DB').execute('SELECT username FROM admins ORDER BY id'): print('  -', u)"
echo
read -r -p "Upišite korisničko ime kojem želite postaviti novu lozinku (ili novo ime): " U </dev/tty
[ -n "$U" ] || { echo "Ništa nije upisano. Ništa nije promijenjeno."; exit 1; }
echo "Nova lozinka: najmanje 12 znakova. Dok tipkate, ništa se ne vidi. Upisuje se dvaput."
sudo -u adv bash -c "set -a; . $ENVF; set +a; cd $APP; venv/bin/flask --app app create-admin '$U'" </dev/tty
sudo -u adv python3 -c "import sqlite3; c=sqlite3.connect('$DB'); c.execute('DELETE FROM login_attempts'); c.commit()" 2>/dev/null || true
echo
echo "Prijava: https://academiadelavida.hr/admin  ·  korisničko ime: $U"

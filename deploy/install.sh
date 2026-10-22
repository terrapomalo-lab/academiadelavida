#!/usr/bin/env bash
# Academia de la Vida – postavljanje na server jednom naredbom (Ubuntu 22.04/24.04 ili Debian 12).
# Pokretanje na serveru (kao root):
#   curl -fsSL https://raw.githubusercontent.com/terrapomalo-lab/academiadelavida/main/deploy/install.sh | bash
# Ponovno pokretanje je sigurno: postojeća evidencija i postavke se ne brišu.
set -euo pipefail

REPO="https://github.com/terrapomalo-lab/academiadelavida.git"
APP=/opt/academiadelavida
DATA=/var/lib/academiadelavida
BACKUPS=/var/backups/academiadelavida
ENVF=/etc/academiadelavida.env
DOMAIN=academiadelavida.hr

say()  { printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mGREŠKA: %s\033[0m\n" "$*"; exit 1; }
ask()  { local v; read -r -p "$1" v </dev/tty; printf "%s" "$v"; }
asks() { local v; read -r -s -p "$1" v </dev/tty; echo >&2; printf "%s" "$v"; }

[ "$(id -u)" = 0 ] || fail "Pokrenite kao root (prijavite se kao root ili ispred naredbe dodajte sudo)."
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) fail "Podržani su Ubuntu i Debian, a ovaj server ima: $PRETTY_NAME";; esac

say "1/8 Provjera servera ($PRETTY_NAME)"
if ss -ltnp 2>/dev/null | grep -E ':(80|443)\s' | grep -qv caddy; then
  ss -ltnp | grep -E ':(80|443)\s' || true
  fail "Na portu 80 ili 443 već radi drugi web poslužitelj (vidi gore). Pošaljite ovaj ispis Claudeu; ništa nije promijenjeno."
fi

say "2/8 Instalacija potrebnih programa (nekoliko minuta)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl sudo iproute2 fonts-dejavu-core ca-certificates gnupg >/dev/null
if ! command -v caddy >/dev/null; then
  if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  fi
fi

say "3/8 Korisnik i mape"
id adv >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin adv
mkdir -p "$DATA" "$BACKUPS"

say "4/8 Preuzimanje stranice"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd || true)"
if [ -n "$SRC" ] && [ -f "$SRC/app.py" ] && [ "$SRC" != "$APP" ]; then
  # Pokrenuto iz raspakiranog zip-a: kopiraj datoteke (venv i evidencija ostaju netaknuti)
  mkdir -p "$APP"
  tar -C "$SRC" --exclude=venv --exclude=.git --exclude=data -cf - . | tar -C "$APP" -xf -
elif [ -d "$APP/.git" ]; then git -C "$APP" pull -q
else rm -rf "$APP"; git clone -q "$REPO" "$APP"; fi
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R adv:adv "$APP" "$DATA" "$BACKUPS"

say "5/8 Postavke i lozinke (ne spremaju se u kod)"
if [ ! -f "$ENVF" ]; then
  cp "$APP/deploy/env.example" "$ENVF"
  SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
  sed -i "s|^ADV_SECRET_KEY=.*|ADV_SECRET_KEY=\"$SECRET\"|" "$ENVF"
  echo "Gmail udruge: zalijepite 16-znamenkastu lozinku za aplikaciju (ne vidi se dok tipkate)."
  echo "Ako je još nemate, samo pritisnite Enter: poruke će čekati u redu dok je ne upišete."
  GPW=$(asks "Lozinka za aplikaciju: " | tr -d ' ')
  if [ -n "$GPW" ]; then sed -i "s|^ADV_SMTP_PASSWORD=.*|ADV_SMTP_PASSWORD=\"$GPW\"|" "$ENVF"; else sed -i "s|^ADV_SMTP_HOST=.*|ADV_SMTP_HOST=\"\"|" "$ENVF"; fi
fi
chown root:adv "$ENVF"; chmod 640 "$ENVF"

run_flask() { sudo -u adv bash -c "set -a; . $ENVF; set +a; cd $APP; venv/bin/flask --app app $*"; }

say "6/8 Evidencija i administrator"
run_flask init-db
NADMIN=$(sudo -u adv python3 -c "import sqlite3;print(sqlite3.connect('$DATA/adv.sqlite3').execute('SELECT COUNT(*) FROM admins').fetchone()[0])" 2>/dev/null || echo 0)
if [ "$NADMIN" = "0" ]; then
  ADMIN=$(ask "Korisničko ime za administraciju (npr. snjezana): ")
  [ -n "$ADMIN" ] || ADMIN="admin"
  sudo -u adv bash -c "set -a; . $ENVF; set +a; cd $APP; venv/bin/flask --app app create-admin '$ADMIN'" </dev/tty
fi

say "7/8 Pokretanje servisa, HTTPS i automatskih poslova"
cp "$APP/deploy/academiadelavida.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now academiadelavida
systemctl restart academiadelavida
cp "$APP/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl enable -q caddy; systemctl reload caddy 2>/dev/null || systemctl restart caddy
crontab -u adv "$APP/deploy/timers.txt"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow OpenSSH >/dev/null; fi

say "8/8 Provjera"
sleep 3
CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8077/ || true)
[ "$CODE" = "200" ] || { journalctl -u academiadelavida -n 30 --no-pager; fail "Stranica ne odgovara (kod $CODE). Pošaljite ispis iznad Claudeu."; }
IP=$(curl -s -4 https://ifconfig.me || hostname -I | awk '{print $1}')
echo
echo "GOTOVO. Aplikacija radi na serveru."
echo "Javna adresa: https://$DOMAIN  (proradi kad domena pokazuje na IP $IP; HTTPS se postavlja sam)"
echo "Administracija: https://$DOMAIN/admin"
grep -q '^ADV_SMTP_HOST=""' "$ENVF" && echo "Napomena: e-pošta još nije postavljena. Kad imate lozinku za aplikaciju, javite se Claudeu."

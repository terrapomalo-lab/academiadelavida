#!/usr/bin/env bash
# Postavljanje slanja e-pošte preko Gmaila udruge (zaporka za aplikaciju). Pokretanje kao root:
#   bash /opt/academiadelavida/deploy/set-mail.sh
# Zaporka se upisuje ovdje i ne vidi se dok se tipka; nigdje se ne ispisuje.
set -euo pipefail
APP=/opt/academiadelavida
ENVF=/etc/academiadelavida.env
DB=/var/lib/academiadelavida/adv.sqlite3

[ "$(id -u)" = 0 ] || { echo "Pokrenite kao root."; exit 1; }
[ -f "$ENVF" ] || { echo "Nema datoteke $ENVF."; exit 1; }

echo "Upišite 16 slova zaporke za aplikaciju (razmaci nisu bitni). Dok tipkate, ništa se ne vidi."
read -r -s -p "Zaporka: " GPW </dev/tty; echo
GPW=$(printf "%s" "$GPW" | tr -d ' ')
if ! printf "%s" "$GPW" | grep -Eq '^[A-Za-z]{16}$'; then
  echo "To nije 16 slova (upisano znakova: ${#GPW}). Ništa nije promijenjeno. Pokrenite naredbu ponovno."
  exit 1
fi

cp -p "$ENVF" "$ENVF.bak"
GPW="$GPW" python3 - "$ENVF" <<'PY'
import os, re, sys
p = sys.argv[1]
s = open(p).read()
vals = {"ADV_SMTP_HOST": "smtp.gmail.com", "ADV_SMTP_PORT": "587", "ADV_SMTP_STARTTLS": "1",
        "ADV_SMTP_USER": "academiadelavidaudruga@gmail.com", "ADV_SMTP_PASSWORD": os.environ["GPW"]}
for k, v in vals.items():
    line = f'{k}="{v}"'
    if re.search(rf"^{k}=.*$", s, flags=re.M):
        s = re.sub(rf"^{k}=.*$", lambda m: line, s, flags=re.M)
    else:
        s += "\n" + line + "\n"
open(p, "w").write(s)
PY
chown root:adv "$ENVF"; chmod 640 "$ENVF"

echo "Provjera prijave u Gmail i probna poruka..."
if ! sudo -u adv bash -c "set -a; . $ENVF; set +a; python3 - " <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
m = EmailMessage()
m["From"] = os.environ.get("ADV_MAIL_FROM", "Academia de la Vida <academiadelavidaudruga@gmail.com>")
m["To"] = "academiadelavidaudruga@gmail.com"
m["Subject"] = "Probna poruka sa stranice academiadelavida.hr"
m.set_content("Ova poruka potvrđuje da stranica može slati e-poštu preko Gmaila udruge.")
s = smtplib.SMTP("smtp.gmail.com", 587, timeout=20)
s.starttls(context=ssl.create_default_context())
s.login(os.environ["ADV_SMTP_USER"], os.environ["ADV_SMTP_PASSWORD"])
s.send_message(m)
s.quit()
PY
then
  mv "$ENVF.bak" "$ENVF"
  echo
  echo "NIJE USPJELO: Gmail nije prihvatio prijavu. Prethodne postavke su vraćene, ništa se nije promijenilo."
  echo "Pošaljite Claudeu ispis iznad (u njemu nema zaporke)."
  exit 1
fi
rm -f "$ENVF.bak"

# Stare poruke iz reda (napisane prije promjene članarina) ne šalju se same; ostaju u administraciji pod „held“.
OLD=$(sudo -u adv python3 -c "
import sqlite3
c = sqlite3.connect('$DB')
n = c.execute(\"UPDATE outbox SET status='held' WHERE status IN ('queued','failed')\").rowcount
c.commit(); print(n)")
systemctl restart academiadelavida
sleep 2
echo
echo "USPJELO. Probna poruka poslana je na academiadelavidaudruga@gmail.com."
echo "Stare poruke koje nisu poslane (zadržane, ne šalju se same): $OLD"
curl -s -o /dev/null -w "Stranica odgovara: %{http_code}\n" http://127.0.0.1:8077/

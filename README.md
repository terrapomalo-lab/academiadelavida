# Academia de la Vida: web stranica i vođenje članstva

Jedna mala aplikacija (Python/Flask) s jednom datotekom evidencije (SQLite). Nema dodataka koje treba ažurirati ni vanjskih servisa osim slanja e-pošte.

- Javna stranica na hrvatskom (`/`) i engleskom (`/en/`), obavijest o privatnosti, statut u PDF-u (`docs/statut.pdf`).
- Pristupnica sprema prijavu i šalje automatsku potvrdu primitka.
- Administracija je na `/admin`.
- Kodovi za plaćanje: HUB3 PDF417 za hrvatske banke i EPC QR za ostale europske banke. Uz njih ide i PDF obavijest za uplatu.

## Što se odvija automatski, a što radite vi

| Korak | Automatski | Vi |
|---|---|---|
| Spremanje pristupnice i potvrda primitka | ✔ | |
| Skupni pregled prijava | ✔ (Prijave) | |
| Odluka o prijemu | | Upravni odbor odlučuje; tajnik označi osobe, upiše datum i oznaku zapisnika i potvrdi |
| Dobrodošlica i obavijest za uplatu (PDF i kod) | ✔ odmah nakon evidentirane odluke | |
| Provjera je li uplata stigla | | Pogledati račun u banci |
| Evidentiranje uplate | Djelomično: uvoz izvoda sam upari uplate po pozivu na broj | Ručna oznaka „Evidentiraj uplatu” ili uvoz izvoda |
| Potvrda uplate članu | ✔ | |
| Podsjetnik za obnovu (30 dana prije isteka) | ✔ svaki dan u 7:00 | |
| Sažetak novih prijava i uplata | ✔ svaki dan u 7:00, samo ako ima novosti | |
| Ponovno slanje neuspjelih poruka | ✔ svakih 5 minuta | |
| Sigurnosna kopija | ✔ svaki dan u 3:00, čuva se zadnjih 30 | Povremeno preuzeti kopiju izvan servera |
| Newsletter | | Napisati, odobriti, poslati |
| Dopuna podataka za osobe bez OIB-a | | Upisati podatke kad stignu |

Prikaz ili preuzimanje koda za plaćanje **nije** dokaz uplate. Članarina postaje „plaćena” tek kad se uplata evidentira.

## Svakodnevno vođenje (oko 10 minuta tjedno)

1. **Jutarnji sažetak** stiže na e-mail udruge kad ima novosti.
2. **Nakon sjednice Upravnog odbora:** Prijave → označite primljene → „Evidentiraj odluku o prijemu…” → upišite datum i oznaku zapisnika → potvrdite. Svima se automatski šalju dobrodošlica i obavijest za uplatu.
3. **Jednom tjedno provjerite uplate:**
   - za pojedinačne uplate: Članovi → član → upišite iznos i datum → „Evidentiraj uplatu”;
   - za više uplata odjednom: Uplate → uvoz izvoda (CSV).

   Nejasne uplate pojavljuju se pod „Za pregled”, gdje ih povežete s članarinom, označite kao donaciju ili zanemarite.
4. **Početna stranica administracije** pokazuje što treba pažnju: nove prijave, članarine nakon roka, uplate za pregled i neposlane poruke.
5. **Izvoz:** popis članova (podaci iz čl. 18. Statuta), članarine i uplate, u Excelu ili CSV-u.

## Postavljanje na Contabo server (jednokratno)

Pretpostavka: Ubuntu 22.04/24.04 ili Debian 12, pristup preko SSH-a s `sudo` ovlastima.

```bash
# 1. Sustav i korisnik
sudo apt update && sudo apt install -y python3-venv fonts-dejavu-core caddy
sudo useradd --system --home /opt/academiadelavida --shell /usr/sbin/nologin adv
sudo mkdir -p /opt/academiadelavida /var/lib/academiadelavida /var/backups/academiadelavida
# 2. Kod (raspakirati isporučeni zip u /opt/academiadelavida)
sudo python3 -m venv /opt/academiadelavida/venv
sudo /opt/academiadelavida/venv/bin/pip install -r /opt/academiadelavida/requirements.txt
sudo chown -R adv:adv /opt/academiadelavida /var/lib/academiadelavida /var/backups/academiadelavida
# 3. Tajne
sudo cp /opt/academiadelavida/deploy/env.example /etc/academiadelavida.env
sudo chown root:adv /etc/academiadelavida.env && sudo chmod 640 /etc/academiadelavida.env && sudo nano /etc/academiadelavida.env
# 4. Evidencija i administratori (po jedan račun za svakog člana UO)
cd /opt/academiadelavida
sudo -u adv bash -c 'set -a; . /etc/academiadelavida.env; set +a; venv/bin/flask --app app init-db; venv/bin/flask --app app create-admin tajnik'
# 5. Servis, HTTPS i automatski poslovi
sudo cp deploy/academiadelavida.service /etc/systemd/system/ && sudo systemctl enable --now academiadelavida
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
sudo crontab -u adv deploy/timers.txt
```

Datoteka s tajnama čitljiva je samo rootu i korisniku `adv` (prava 640).

Ako na serveru već radi nginx ili Apache, Caddy se ne instalira. Umjesto toga u postojeći poslužitelj dodaje se proxy prema `127.0.0.1:8077`, uz zaglavlje `X-Real-IP`.

## Domena (DNS)

Kod registrara domene `academiadelavida.hr` postaviti:

- zapis `A` za `academiadelavida.hr` na IPv4 adresu Contabo servera (i `AAAA` za IPv6, ako postoji);
- zapis `A` (ili `CNAME`) za `www` na isto.

Promjena obično proradi unutar sat vremena, ali može trajati i dulje. HTTPS certifikat Caddy pribavlja sam čim DNS pokazuje na server.

## E-pošta

Adresa `academiadelavidaudruga@gmail.com` ostaje adresa za odgovore. Za automatsko slanje potreban je SMTP pristup.

- **Opcija A (brzo, besplatno):** na Gmailu udruge uključiti dvofaktorsku prijavu, izraditi „lozinku za aplikaciju” i upisati je u `ADV_SMTP_PASSWORD`. Gmail ima dnevno ograničenje broja primatelja (prema pravilima Googlea), što je dovoljno za potvrde i obavijesti, ali je tijesno za veći newsletter. Red poruka zato šalje najviše 40 poruka u 5 minuta.
- **Opcija B (kasnije, za newsletter):** usluga za slanje e-pošte s pošiljateljem na vlastitoj domeni (npr. `info@academiadelavida.hr`), uz SPF i DKIM zapise u DNS-u. Uključuje se nakon vaše potvrde, a mijenjaju se samo vrijednosti `ADV_SMTP_*`.

## Testiranje

```bash
pip install -r requirements.txt pytest zxing-cpp
ADV_COOKIE_SECURE=0 python3 -m pytest -q tests
```

Testovi provjeravaju cijeli postupak:

- prijavu (s OIB-om i bez njega), validaciju, zaštitu od robota i maloljetnike;
- što se događa kad slanje e-pošte zakaže;
- prijavu u administraciju, zaštitu od pogađanja lozinke i CSRF;
- odluku Upravnog odbora i potvrdu odluke;
- iznose 50 € i 20 €;
- sadržaj HUB3 i EPC kodova, koji se dekodiraju i uspoređuju;
- PDF obavijest;
- ručnu uplatu i sprječavanje dvostrukog evidentiranja;
- uvoz izvoda s duplikatima, krivim iznosom i donacijom;
- obnovu i podsjetnik, sažetak, izvoz, sigurnosne kopije, newsletter i odjavu.

## Datoteke

`app.py` (rute), `services.py` (logika), `payments.py` (kodovi i PDF), `mailer.py` (red poruka), `i18n.py` (svi tekstovi HR/EN), `templates/`, `static/`, `docs/statut.pdf`, `deploy/`, `tests/`.

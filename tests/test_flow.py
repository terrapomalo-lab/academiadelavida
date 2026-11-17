"""Cjeloviti test postupka: prijava → odluka UO → obavijest za uplatu → uplata → obnova.
Pokretanje: python3 -m pytest -q tests"""
import email
import io
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import zxingcpp
from PIL import Image
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app  # noqa: E402
import db as DB  # noqa: E402
import mailer  # noqa: E402
import services as S  # noqa: E402

VALID_OIB = "69435151530"   # ispravna kontrolna znamenka (testni broj)
ADULT_DOB = "1980-05-17"


def decode(png_bytes):
    r = zxingcpp.read_barcodes(Image.open(io.BytesIO(png_bytes)))
    assert r, "kod nije pročitan"
    return r[0].format.name, r[0].text


@pytest.fixture()
def env(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path, "DB_PATH": tmp_path / "t.db", "BACKUP_DIR": tmp_path / "b",
                      "MAIL_SUPPRESS_SEND": True, "SESSION_COOKIE_SECURE": False, "BASE_URL": "https://academiadelavida.hr"})
    con = DB.connect(app.config["DB_PATH"])
    con.execute("INSERT INTO admins(username, pw_hash, created_at) VALUES ('tajnik', ?, 'x')",
                (generate_password_hash("dugacka-lozinka-123"),))
    return app, app.test_client(), con


def form(**kw):
    f = {"lang": "hr", "ime": "Ana Horvat", "email": "ana@example.com", "tel": "", "oib": VALID_OIB, "dob": ADULT_DOB,
         "paket": "assoc", "jezik": "hr", "statut": "1", "punoljetnost": "1", "privatnost": "1"}
    f.update(kw)
    return {k: v for k, v in f.items() if v is not None}


def login(c):
    c.get("/admin/prijava")
    with c.session_transaction() as s:
        tok = s["csrf"]
    r = c.post("/admin/prijava", data={"csrf": tok, "username": "tajnik", "password": "dugacka-lozinka-123"})
    assert r.status_code == 302
    with c.session_transaction() as s:   # token se mijenja pri prijavi
        return s["csrf"]


def body(msg):
    for p in msg.walk():
        if p.get_content_type() == "text/plain":
            return p.get_payload(decode=True).decode()


def eml(app, kind):
    d = app.config["DATA_DIR"] / "mail-preview"
    files = sorted(d.glob(f"*-{kind}.eml"))
    return [email.message_from_bytes(f.read_bytes()) for f in files]


# ------------------------------------------------------------------ javne stranice

def test_pages_and_links(env):
    app, c, _ = env
    for url in ["/", "/en/", "/privatnost", "/en/privacy", "/dokumenti/statut.pdf", "/robots.txt",
                "/static/img/logo.webp", "/static/img/vida.webp", "/static/img/hrvoje.webp", "/static/img/snjezana.webp",
                "/static/site.css", "/static/site.js"]:
        assert c.get(url).status_code == 200, url
    html = c.get("/").get_data(as_text=True)
    for href in re.findall(r'href="(/[^"#]*)"', html):
        assert c.get(href).status_code in (200, 301), href
    assert "Postanite član" in html and "Hribarov prilaz 13" in html and "HR1123600001101666226" in html
    en = c.get("/en/").get_data(as_text=True)
    assert "Become a member" in en and "I do not have a Croatian personal identification number (OIB)" in en
    assert "Uskoro" not in html and "Coming soon" not in en
    assert c.get("/admin").status_code == 302
    assert c.get("/p/nepostojeci").status_code == 404


# ------------------------------------------------------------------ prijava

def test_validation(env):
    app, c, con = env
    r = c.post("/api/prijava", data=form(oib="12345678901"))
    assert r.status_code == 400 and "err_oib" in r.json["errors"]
    r = c.post("/api/prijava", data=form(dob=(date.today() - timedelta(days=365 * 15)).isoformat()))
    assert "err_minor" in r.json["errors"]
    r = c.post("/api/prijava", data=form(statut=None, privatnost=None, punoljetnost=None))
    assert {"err_statute", "err_privacy", "err_adult"} <= set(r.json["errors"])
    r = c.post("/api/prijava", data=form(lang="en", oib=""))
    assert "the OIB must have 11 digits" in r.json["message"]
    r = c.post("/api/prijava", data=form(web="spam"))
    assert r.status_code == 400
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_application_saved_and_confirmed(env):
    app, c, con = env
    r = c.post("/api/prijava", data=form(novosti="1"))
    assert r.status_code == 200 and r.json["ok"] and r.json["mail_sent"] is True
    no = r.json["public_no"]
    assert re.match(r"ADV-\d{4}-0001$", no)
    a = con.execute("SELECT * FROM applications").fetchone()
    assert a["app_status"] == "submitted" and a["oib"] == VALID_OIB and a["needs_completion"] == 0
    assert con.execute("SELECT COUNT(*) FROM subscribers WHERE email='ana@example.com'").fetchone()[0] == 1
    m = eml(app, "confirmation")[0]
    assert no in m.get_payload() if not m.is_multipart() else True
    # broj prijave nije zakonski identifikator i različit je od OIB-a
    assert VALID_OIB not in no


def test_application_without_oib_en(env):
    app, c, con = env
    r = c.post("/api/prijava", data=form(lang="en", jezik="en", oib="", bez_oib="1"))
    assert r.status_code == 400 and "err_country" in r.json["errors"]
    r = c.post("/api/prijava", data=form(lang="en", jezik="en", oib="", bez_oib="1", drzava="Slovenia", ime="Janez Novak",
                                          email="janez@example.si", paket="assoc_reduced"))
    assert r.json["ok"]
    a = con.execute("SELECT * FROM applications WHERE email='janez@example.si'").fetchone()
    assert a["oib"] is None and a["no_oib"] == 1 and a["needs_completion"] == 1 and a["lang"] == "en"
    b = body(eml(app, "confirmation")[-1])
    assert "do not have a Croatian OIB" in b and "€20 per year" in b and "pay the membership fee right away" in b


def test_nojs_form_post(env):
    app, c, con = env
    r = c.post("/prijava", data=form(email="nojs@example.com"))
    assert r.status_code == 200 and "Pristupnica je spremljena" in r.get_data(as_text=True)


def test_smtp_failure_keeps_application(env, tmp_path):
    app, c, con = env
    app.config.update(MAIL_SUPPRESS_SEND=False, SMTP_HOST="127.0.0.1", SMTP_PORT=1)  # nedostupno
    r = c.post("/api/prijava", data=form(email="fail@example.com"))
    assert r.status_code == 200 and r.json["ok"] and r.json["mail_sent"] is False
    assert "čim to bude moguće" in " ".join(r.json["paragraphs"])
    o = con.execute("SELECT * FROM outbox").fetchone()
    assert o["status"] == "failed" and o["last_error"]
    app.config.update(MAIL_SUPPRESS_SEND=True)
    s, f = mailer.send_outbox(con, app.config)
    assert (s, f) == (2, 0)   # potvrda prijavitelju + obavijest udruzi
    assert {r[0] for r in con.execute("SELECT status FROM outbox")} == {"sent"}


# ------------------------------------------------------------------ administracija

def test_admin_security(env):
    app, c, con = env
    c.get("/admin/prijava")
    with c.session_transaction() as s:
        tok = s["csrf"]
    for _ in range(5):
        r = c.post("/admin/prijava", data={"csrf": tok, "username": "tajnik", "password": "kriva"})
    r = c.post("/admin/prijava", data={"csrf": tok, "username": "tajnik", "password": "dugacka-lozinka-123"})
    assert "Previše neuspjelih" in r.get_data(as_text=True)
    con.execute("DELETE FROM login_attempts")
    login(c)
    r = c.post("/admin/odluka", data={"kind": "admit", "ids": "1"})  # bez CSRF tokena
    assert r.status_code == 400


def full_admission(app, c, con, **kw):
    c.post("/api/prijava", data=form(**kw))
    tok = login(c)
    aid = con.execute("SELECT id FROM applications ORDER BY id DESC").fetchone()[0]
    r = c.post("/admin/odluka/pregled", data={"csrf": tok, "kind": "admit", "ids": str(aid)})
    assert r.status_code == 200 and "Potvrđujem" in r.get_data(as_text=True)
    r = c.post("/admin/odluka", data={"csrf": tok, "kind": "admit", "ids": str(aid), "decision_date": date.today().isoformat(),
                                       "reference": "Zapisnik 1. sjednice UO", "confirm": "1"})
    assert r.status_code == 302
    return tok, aid


def test_decision_requires_confirmation(env):
    app, c, con = env
    c.post("/api/prijava", data=form())
    tok = login(c)
    r = c.post("/admin/odluka", data={"csrf": tok, "kind": "admit", "ids": "1", "decision_date": date.today().isoformat(),
                                       "reference": "Zapisnik"})
    assert r.status_code == 302
    assert con.execute("SELECT app_status FROM applications").fetchone()[0] == "submitted"
    r = c.post("/admin/odluka", data={"csrf": tok, "kind": "admit", "ids": "1", "confirm": "1",
                                       "decision_date": (date.today() + timedelta(days=3)).isoformat(), "reference": "Z"})
    assert con.execute("SELECT app_status FROM applications").fetchone()[0] == "submitted"


@pytest.mark.parametrize("fee,cents", [("assoc", 5000), ("assoc_reduced", 2000)])
def test_admission_notice_and_codes(env, fee, cents):
    app, c, con = env
    tok, aid = full_admission(app, c, con, paket=fee, ime="Ivana Šimić-Đurić")
    a = con.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone()
    d = con.execute("SELECT d.* FROM dues d JOIN memberships m ON m.id=d.membership_id WHERE m.application_id=?", (aid,)).fetchone()
    assert a["app_status"] == "approved" and d["amount_cents"] == cents and d["status"] == "open"
    assert d["reference"] == f"{a['year']}-{a['seq']:04d}-1"
    start = date.fromisoformat(d["period_start"])
    assert d["period_end"] == (S.add_months(start, 12) - timedelta(days=1)).isoformat()
    dec = con.execute("SELECT * FROM decisions").fetchone()
    assert dec["reference"] == "Zapisnik 1. sjednice UO" and dec["created_by"] == "tajnik"

    # dobrodošlica s privicima
    msg = eml(app, "welcome")[-1]
    parts = {p.get_filename(): p.get_payload(decode=True) for p in msg.walk() if p.get_filename()}
    pdf = [v for k, v in parts.items() if k.endswith(".pdf")][0]
    png = [v for k, v in parts.items() if k.endswith(".png")][0]
    assert pdf[:4] == b"%PDF"
    fmt, text = decode(png)
    lines = text.split("\n")
    assert fmt == "PDF417" and lines[0] == "HRVHUB30" and lines[1] == "EUR"
    assert lines[2] == f"{cents:015d}" and lines[3] == "Ivana Šimić-Đurić"
    assert lines[6] == "Academia de la Vida" and lines[9] == "HR1123600001101666226"
    assert lines[10] == "HR00" and lines[11] == d["reference"] and lines[13] == f"Članarina {a['public_no']}"
    body = [p for p in msg.walk() if p.get_content_type() == "text/plain"][0].get_payload(decode=True).decode()
    assert f"/p/{a['token']}" in body and "Zapisnik" not in body and "Temelj:" in body

    # stranica za uplatu i kodovi
    html = c.get(f"/p/{a['token']}").get_data(as_text=True)
    assert "HR00 " + d["reference"] in html and "HR1123600001101666226" in html
    fmt, text = decode(c.get(f"/p/{a['token']}/hub3.png").data)
    assert text.split("\n")[2] == f"{cents:015d}"
    fmt, text = decode(c.get(f"/p/{a['token']}/epc.png").data)
    assert fmt == "QRCode" and text.startswith("BCD\n002\n1\nSCT\nZABAHR2X\nAcademia de la Vida\nHR1123600001101666226\n")
    assert f"EUR{cents // 100}.00" in text
    r = c.get(f"/p/{a['token']}/hub3.png?dl=1")
    assert "attachment" in r.headers["Content-Disposition"]
    assert c.get(f"/p/{a['token']}/obavijest.pdf").data[:4] == b"%PDF"


def test_manual_payment_and_duplicates(env):
    app, c, con = env
    tok, aid = full_admission(app, c, con)
    d = con.execute("SELECT * FROM dues").fetchone()
    r = c.post(f"/admin/clan/{aid}", data={"csrf": tok, "action": "paid", "due_id": d["id"], "amount": "50,00",
                                           "booked_date": date.today().isoformat()})
    assert con.execute("SELECT status FROM dues").fetchone()[0] == "paid"
    assert len(eml(app, "paid")) == 1
    ok, msg = S.record_manual_payment(con, app.config, "tajnik", d["id"], date.today().isoformat(), 5000)
    assert not ok and "već" in msg
    assert con.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    html = c.get(f"/p/{con.execute('SELECT token FROM applications').fetchone()[0]}").get_data(as_text=True)
    assert "plaćena" in html
    assert c.get(f"/p/{con.execute('SELECT token FROM applications').fetchone()[0]}/hub3.png").status_code == 404


def test_wrong_amount_goes_to_review(env):
    app, c, con = env
    tok, aid = full_admission(app, c, con)
    d = con.execute("SELECT * FROM dues").fetchone()
    ok, msg = S.record_manual_payment(con, app.config, "tajnik", d["id"], date.today().isoformat(), 4000)
    assert ok and "pregled" in msg
    assert con.execute("SELECT status FROM dues").fetchone()[0] == "open"
    assert con.execute("SELECT status FROM payments").fetchone()[0] == "review"


def test_statement_import(env):
    app, c, con = env
    tok, aid = full_admission(app, c, con)
    ref = con.execute("SELECT reference FROM dues").fetchone()[0]
    csv_text = ("datum;iznos;platitelj;poziv_na_broj;opis;id_transakcije\n"
                f"{date.today().strftime('%d.%m.%Y')};50,00;Ana Horvat;HR00 {ref};Članarina;T1\n"
                f"{date.today().strftime('%d.%m.%Y')};50,00;Ana Horvat;HR00 {ref};Članarina;T1\n"   # ista transakcija
                f"{date.today().strftime('%d.%m.%Y')};25,00;Marko M.;;Donacija;T2\n"
                f"{date.today().strftime('%d.%m.%Y')};30,00;Nepoznat;;uplata;T3\n"
                f"{date.today().strftime('%d.%m.%Y')};-12,00;Banka;;naknada;T4\n")
    r = c.post("/admin/uplate", data={"csrf": tok, "action": "import", "file": (io.BytesIO(csv_text.encode()), "izvod.csv")},
               content_type="multipart/form-data")
    page = r.get_data(as_text=True)
    assert "uparenih članarina 1, uplata prije prijema 0, donacija 1, za pregled 1, već evidentiranih (preskočeno) 1" in page
    assert con.execute("SELECT status FROM dues").fetchone()[0] == "paid"
    # ponovni uvoz iste datoteke ne stvara duplikate
    res = S.import_statement(con, app.config, "tajnik", csv_text.encode())
    assert res["duplicates"] == 4 and res["matched"] == 0
    assert con.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 3
    # nejasna uplata: označi kao donaciju
    pid = con.execute("SELECT id FROM payments WHERE status='review'").fetchone()[0]
    c.post("/admin/uplate", data={"csrf": tok, "action": "donation", "payment_id": pid})
    assert con.execute("SELECT kind, status FROM payments WHERE id=?", (pid,)).fetchone()[:] == ("donation", "matched")


def test_review_assign(env):
    app, c, con = env
    tok, aid = full_admission(app, c, con)
    csv_text = f"datum;iznos;platitelj;poziv_na_broj;opis\n{date.today().isoformat()};50.00;A. Horvat;;clanarina Ana\n"
    S.import_statement(con, app.config, "tajnik", csv_text.encode())
    pid = con.execute("SELECT id FROM payments WHERE status='review'").fetchone()[0]
    did = con.execute("SELECT id FROM dues").fetchone()[0]
    c.post("/admin/uplate", data={"csrf": tok, "action": "assign", "payment_id": pid, "due_id": did})
    assert con.execute("SELECT status FROM dues").fetchone()[0] == "paid"


def test_donation_codes(env):
    app, c, con = env
    fmt, text = decode(c.get("/donacija/hub3.png?iznos=25.00").data)
    lines = text.split("\n")
    assert lines[2] == "000000000002500" and lines[10] == "HR99" and lines[11] == "" and lines[12] == "CHAR" and lines[13] == "Donacija"
    fmt, text = decode(c.get("/donacija/epc.png?iznos=25").data)
    assert "EUR25.00" in text and "Donacija" in text
    assert c.get("/donacija/uplatnica.pdf?iznos=10&lang=en").data[:4] == b"%PDF"
    assert c.get("/donacija/hub3.png?iznos=0").status_code == 400
    assert c.get("/donacija/hub3.png?iznos=abc").status_code == 400
    assert con.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0  # prikaz koda nije uplata


def test_renewal_reminder_and_digest(env):
    app, c, con = env
    tok, aid = full_admission(app, c, con)
    d = con.execute("SELECT * FROM dues").fetchone()
    S.record_manual_payment(con, app.config, "tajnik", d["id"], date.today().isoformat(), 5000)
    end = date.fromisoformat(d["period_end"])
    assert S.run_renewals(con, app.config, today=end - timedelta(days=60)) == 0
    assert S.run_renewals(con, app.config, today=end - timedelta(days=29)) == 1
    assert S.run_renewals(con, app.config, today=end - timedelta(days=20)) == 0  # nema dvostrukih podsjetnika
    d2 = con.execute("SELECT * FROM dues WHERE period_index=2").fetchone()
    assert d2["period_start"] == (end + timedelta(days=1)).isoformat() and d2["reference"].endswith("-2")
    mailer.send_outbox(con, app.config)
    assert len(eml(app, "reminder")) == 1
    text = S.digest(con, app.config, "2000-01-01")
    assert "Nove prijave: 1" in text and "Evidentirane uplate: 1" in text


def test_export_backup_newsletter_unsubscribe(env):
    app, c, con = env
    tok, aid = full_admission(app, c, con, novosti="1")
    r = c.get("/admin/izvoz/popis-clanova.xlsx")
    assert r.status_code == 200 and r.data[:2] == b"PK"
    r = c.get("/admin/izvoz/popis-clanova.csv")
    assert VALID_OIB in r.get_data(as_text=True)
    gz = S.backup(app.config["DB_PATH"], app.config["BACKUP_DIR"], 2)
    assert gz.exists() and gz.stat().st_size > 0
    for _ in range(3):
        S.backup(app.config["DB_PATH"], app.config["BACKUP_DIR"], 2)
    assert len(list(app.config["BACKUP_DIR"].glob("*.gz"))) == 2
    # newsletter: bez odobrenja se ne šalje
    c.post("/admin/newsletter", data={"csrf": tok, "action": "save", "subject": "Prvo predavanje", "body": "Pozivamo vas…", "lang": "hr"})
    nid = con.execute("SELECT id FROM newsletters").fetchone()[0]
    c.post("/admin/newsletter", data={"csrf": tok, "action": "send", "id": nid})
    assert con.execute("SELECT status FROM newsletters").fetchone()[0] == "draft"
    c.post("/admin/newsletter", data={"csrf": tok, "action": "send", "id": nid, "approved": "1", "approved_by": "Vida Demarin"})
    assert con.execute("SELECT status, recipients FROM newsletters").fetchone()[:] == ("sent", 1)
    mailer.send_outbox(con, app.config)
    body = eml(app, "newsletter")[0].get_payload(decode=True).decode()
    link = re.search(r"https://academiadelavida.hr(/odjava/\S+)", body).group(1)
    assert "Odjavi me" in c.get(link).get_data(as_text=True)
    c.post(link)
    assert con.execute("SELECT unsubscribed_at FROM subscribers").fetchone()[0] is not None


def test_hub3_cleaning():
    import payments as P
    assert P.hub3_clean("José Müller & Co – Ltd", 30) == "Jose Muller Co - Ltd"
    assert P.hub3_clean("Đurđa Žižić", 30) == "Đurđa Žižić"
    assert len(P.hub3_clean("x" * 50, 25)) == 25
    assert P.amount_field(5000) == "000000000005000"


# ------------------------------------------------------------------ nove funkcije (25. 10.)

def test_consent_links_are_real_links(env):
    app, c, _ = env
    for url, needle in (("/", '<a href="/dokumenti/statut.pdf" target="_blank" rel="noopener">Statut</a>'),
                        ("/", '<a href="/privatnost" target="_blank" rel="noopener">obavijest o privatnosti</a>'),
                        ("/en/", '<a href="/en/privacy" target="_blank" rel="noopener">privacy notice</a>')):
        html = c.get(url).get_data(as_text=True)
        assert needle in html and "&lt;a href" not in html


def test_donation_code_visible_without_click(env):
    app, c, _ = env
    html = c.get("/").get_data(as_text=True)
    assert 'id="don-box" hidden' not in html and "/donacija/hub3.png?iznos=20.00" in html
    fmt, text = decode(c.get("/donacija/hub3.png?iznos=20.00").data)
    assert text.split("\n")[2] == "000000000002000"


@pytest.mark.parametrize("fee,cents", [("assoc", 5000), ("assoc_reduced", 2000)])
def test_immediate_payment_after_application(env, fee, cents):
    app, c, con = env
    r = c.post("/api/prijava", data=form(paket=fee))
    pay = r.json["pay"]
    assert pay["rows"][0][2] == f"{cents // 100},00"
    a = con.execute("SELECT * FROM applications").fetchone()
    ref = f"{a['year']}-{a['seq']:04d}-1"
    assert pay["rows"][3][1] == "HR00 " + ref
    fmt, text = decode(c.get(pay["hub3"]).data)
    lines = text.split("\n")
    assert lines[2] == f"{cents:015d}" and lines[11] == ref
    assert c.get(pay["pdf"]).data[:4] == b"%PDF"
    page = c.get(f"/p/{a['token']}").get_data(as_text=True)
    assert "HR00 " + ref in page and "uplatu vraćamo" in page
    m = eml(app, "confirmation")[-1]
    names = [p.get_filename() for p in m.walk() if p.get_filename()]
    assert any(n.endswith(".pdf") for n in names) and any(n.endswith(".png") for n in names)
    assert ref in body(m)


def test_prepayment_matched_on_admission_via_import(env):
    app, c, con = env
    c.post("/api/prijava", data=form())
    a = con.execute("SELECT * FROM applications").fetchone()
    ref = f"{a['year']}-{a['seq']:04d}-1"
    csv_text = f"datum;iznos;platitelj;poziv_na_broj;opis;id_transakcije\n{date.today().isoformat()};50,00;Ana Horvat;HR00 {ref};Članarina;X1\n"
    res = S.import_statement(con, app.config, "tajnik", csv_text.encode())
    assert res["prepaid"] == 1
    assert c.get(f"/p/{a['token']}/hub3.png").status_code == 404   # već plaćeno: kod se više ne nudi
    tok = login(c)
    html = c.get("/admin/prijave").get_data(as_text=True)
    assert "uplaćeno" in html
    c.post("/admin/odluka", data={"csrf": tok, "kind": "admit", "ids": str(a["id"]), "decision_date": date.today().isoformat(),
                                  "reference": "Zapisnik 2. sjednice UO", "confirm": "1"})
    d = con.execute("SELECT * FROM dues").fetchone()
    assert d["status"] == "paid" and d["reference"] == ref
    assert con.execute("SELECT status, due_id FROM payments").fetchone()[:] == ("matched", d["id"])
    w = body(eml(app, "welcome")[-1])
    assert "uplata članarine je zaprimljena" in w and "HR00" not in w   # bez ponovnog traženja uplate


def test_manual_prepayment_and_rejection_warning(env):
    app, c, con = env
    c.post("/api/prijava", data=form())
    a = con.execute("SELECT * FROM applications").fetchone()
    tok = login(c)
    c.post(f"/admin/clan/{a['id']}", data={"csrf": tok, "action": "prepaid", "amount": "50,00", "booked_date": date.today().isoformat()})
    assert con.execute("SELECT status FROM payments").fetchone()[0] == "prepaid"
    r = c.post(f"/admin/clan/{a['id']}", data={"csrf": tok, "action": "prepaid", "amount": "50,00", "booked_date": date.today().isoformat()})
    assert con.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    html = c.post("/admin/odluka/pregled", data={"csrf": tok, "kind": "reject", "ids": str(a["id"])}).get_data(as_text=True)
    assert "uplatu treba vratiti" in html


def test_prepayment_wrong_amount_goes_to_review_on_admission(env):
    app, c, con = env
    c.post("/api/prijava", data=form(paket="assoc_reduced"))
    a = con.execute("SELECT * FROM applications").fetchone()
    S.record_prepayment(con, app.config, "tajnik", a, date.today().isoformat(), 5000)
    S.record_decision(con, app.config, "tajnik", "admit", date.today().isoformat(), "Z", [a["id"]])
    assert con.execute("SELECT status FROM dues").fetchone()[0] == "open"
    assert con.execute("SELECT status FROM payments").fetchone()[0] == "review"


def test_fee_packages_and_legacy_price(env):
    app, c, con = env
    r = c.post("/api/prijava", data=form(paket="nepostojece"))
    assert r.status_code == 400 and "err_fee" in r.json["errors"]
    import services as S2
    d, errs = S2.validate_application({**form(), "paket": "", "vrsta": "regular", "clanarina": "student"}, "hr")
    assert "err_reduced" in errs
    # prijava prije nove odluke plaća po prethodnoj cijeni
    app.config["FEES_V2_FROM"] = "2026-10-25"
    old = {"created_at": "2026-10-23T10:00:00+02:00", "member_type": "regular", "fee_cat": "standard"}
    new = {"created_at": "2026-10-26T10:00:00+01:00", "member_type": "regular", "fee_cat": "standard"}
    assert S2.fee_cents(app.config, old) == 5000 and S2.fee_cents(app.config, new) == 15000
    assert S2.fee_cents(app.config, old, current=True) == 15000   # obnova po važećoj odluci
    html = c.get("/").get_data(as_text=True)
    assert "150 €" not in html and "Pridruženo članstvo – studenti i umirovljenici" in html
    assert 'value="regular"' not in html and "mailto:" in html
    # redovito članstvo ne može se prijaviti preko stranice
    r = c.post("/api/prijava", data=form(paket="regular"))
    assert r.status_code == 400 and "err_regular_email" in r.json["errors"]


def test_social_links_hidden_until_set(env):
    app, c, _ = env
    assert "Pratite nas" not in c.get("/").get_data(as_text=True)
    app.config["SOCIAL_LINKS"] = [("YouTube", "https://www.youtube.com/@primjer"), ("Facebook", "")]
    html = c.get("/").get_data(as_text=True)
    assert "Pratite nas" in html and "https://www.youtube.com/@primjer" in html and ">Facebook<" not in html



def test_admin_change_package(env):
    app, c, con = env
    tok, aid = full_admission(app, c, con, paket="assoc")
    a = con.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone()
    data = {"csrf": tok, "action": "update", "full_name": a["full_name"], "email": a["email"], "oib": a["oib"],
            "dob": a["dob"], "lang": "hr", "paket": "regular"}
    r = c.post(f"/admin/clan/{aid}", data=data)
    assert r.status_code == 302
    a = con.execute("SELECT member_type, fee_cat FROM applications WHERE id=?", (aid,)).fetchone()
    assert tuple(a) == ("regular", "standard")
    assert con.execute("SELECT amount_cents FROM dues WHERE status='open'").fetchone()[0] == 15000


def test_old_applications_pay_current_fees(env):
    app, c, con = env
    c.post("/api/prijava", data=form(paket="regular"))
    c.post("/api/prijava", data=form())
    con.execute("UPDATE applications SET member_type='regular'")
    con.execute("UPDATE applications SET created_at='2026-10-23T10:00:00'")
    a = con.execute("SELECT * FROM applications").fetchone()
    assert S.fee_cents(app.config, a) == 15000


def book_form(**kw):
    d = {"lang": "hr", "ime": "Ana Anić", "email": "ana@example.com", "mobitel": "091 234 5678",
         "paketomat": "BOX NOW Zagreb, Ilica 1", "kolicina": "1", "iznos": "42,90", "privatnost": "1"}
    d.update(kw)
    return d


def test_book_order_flow(env):
    app, c, con = env
    html = c.get("/").get_data(as_text=True)
    assert "Knjiga na dar uz donaciju" in html and "42,90 €" in html
    r = c.post("/api/knjiga", data=book_form(kolicina="2", iznos="50"))
    assert r.status_code == 400 and "err_book_amount" in r.json["errors"]
    assert "82,80 €" in r.json["message"]
    assert S.book_min_cents(app.config, 3) == 12270 and S.book_min_cents(app.config, 4) == 16560
    r = c.post("/api/knjiga", data=book_form(iznos="45,00"))
    assert r.status_code == 200 and r.json["ok"]
    o = con.execute("SELECT * FROM book_orders").fetchone()
    assert o["reference"].startswith("7-") and o["amount_cents"] == 4500 and o["status"] == "pending"
    assert c.get(r.json["pay"]["hub3"]).status_code == 200
    assert c.get(r.json["pay"]["epc"]).status_code == 200
    mail = con.execute("SELECT * FROM outbox WHERE kind='book_confirmation'").fetchone()
    assert o["reference"] in mail["body_text"] and "BOX NOW" in mail["body_text"]
    # izvod: uplata s pozivom na broj automatski se povezuje
    csv_ = f"datum;iznos;platitelj;poziv_na_broj;opis;id_transakcije\n2026-11-16;45,00;Ana Anić;HR00 {o['reference']};Donacija;T-B1\n"
    res = S.import_statement(con, app.config, "test", csv_.encode())
    assert res.get("books") == 1 and res["review"] == 0
    o = con.execute("SELECT * FROM book_orders").fetchone()
    assert o["status"] == "paid" and o["paid_date"] == "2026-11-16"
    assert con.execute("SELECT COUNT(*) FROM outbox WHERE kind='book_paid'").fetchone()[0] == 1
    # ista transakcija ponovno: duplikat, bez nove zahvale
    res = S.import_statement(con, app.config, "test", csv_.encode())
    assert res["duplicates"] == 1
    # slanje
    tok = login(c)
    r = c.post("/admin/knjige", data={"csrf": tok, "order_id": o["id"], "action": "shipped", "tracking": "1234567890"})
    assert r.status_code == 302
    o = con.execute("SELECT * FROM book_orders").fetchone()
    assert o["status"] == "shipped" and o["tracking"] == "1234567890"
    assert "1234567890" in con.execute("SELECT body_text FROM outbox WHERE kind='book_shipped'").fetchone()[0]
    assert "Knjige" in c.get("/admin/knjige").get_data(as_text=True)
    assert S.digest(con, app.config, "2000-01-01") and "knjige" in S.digest(con, app.config, "2000-01-01").lower()


def test_book_manual_payment_and_short_amount(env):
    app, c, con = env
    c.post("/api/knjiga", data=book_form())
    o = con.execute("SELECT * FROM book_orders").fetchone()
    csv_ = f"datum;iznos;platitelj;poziv_na_broj;opis;id_transakcije\n2026-11-16;20,00;Ana;HR00 {o['reference']};;T-B2\n"
    res = S.import_statement(con, app.config, "test", csv_.encode())
    assert res["review"] == 1 and con.execute("SELECT status FROM book_orders").fetchone()[0] == "pending"
    tok = login(c)
    c.post("/admin/knjige", data={"csrf": tok, "order_id": o["id"], "action": "paid", "date": "2026-11-17", "amount": "42,90"})
    assert con.execute("SELECT status FROM book_orders").fetchone()[0] == "paid"


def test_book_offer_ends(env):
    app, c, con = env
    app.config["BOOK_OFFER_UNTIL"] = "2000-01-01"
    assert "Knjiga na dar" not in c.get("/").get_data(as_text=True)
    assert c.post("/api/knjiga", data=book_form()).status_code == 410


def test_book_page(env):
    app, c, con = env
    r = c.get("/knjiga")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="book-form"' in html and "knjiga-naslovnica.jpg" in html
    assert 'href="/knjiga"' in c.get("/").get_data(as_text=True)
    assert c.get("/en/book").status_code == 200


def test_admin_notices(env):
    app, c, con = env
    c.post("/api/prijava", data=form())
    c.post("/api/knjiga", data=book_form())
    rows = con.execute("SELECT to_addr, subject FROM outbox WHERE kind='notice' ORDER BY id").fetchall()
    assert len(rows) == 2 and all(r[0] == app.config["NOTIFY_TO"] for r in rows)
    assert rows[0][1].startswith("Nova prijava ADV-") and rows[1][1].startswith("Nova narudžba knjige 7-")


# ------------------------------------------------------------------ predstavljanje knjige
import events as E
from datetime import date as _date, timedelta as _td
from werkzeug.datastructures import MultiDict


def ev_setup(app, **kw):
    e = dict(app.config["EVENT"])
    t = _date.today()
    e.update(date=(t + _td(days=10)).isoformat(), reg_until=(t + _td(days=5)).isoformat(),
             book_until=(t + _td(days=4)).isoformat(), book_cents=3990)
    e.update(kw)
    app.config["EVENT"] = e
    return e


def ev_form(names=(), **kw):
    d = MultiDict({"ime": "Iva Ivić", "email": "iva@example.com", "mobitel": "091 111 2222", "dolazak": "1",
                   "primjerci": str(len(names)), "privatnost": "1"})
    for n in names:
        d.add("posveta", n)
    for k, v in kw.items():
        d[k] = v
    return d


def test_event_registration_with_books(env):
    app, c, con = env
    ev_setup(app)
    assert "Prijava dolaska" in c.get("/predstavljanje").get_data(as_text=True)
    r = c.post("/api/predstavljanje", data=ev_form(names=["Mama", "Ana"]))
    assert r.status_code == 200 and r.json["status"] == "confirmed" and "pay" in r.json
    reg = con.execute("SELECT * FROM event_regs").fetchone()
    assert reg["reference"].startswith("8-") and reg["book_cents"] == 7980 and reg["book_status"] == "pending"
    assert [x[0] for x in con.execute("SELECT name FROM event_dedications ORDER BY pos")] == ["Mama", "Ana"]
    for u in ("hub3", "epc", "pdf"):
        assert c.get(r.json["pay"][u]).status_code == 200
    body = con.execute("SELECT body_text FROM outbox WHERE kind='event_confirmation'").fetchone()[0]
    assert "/predstavljanje/odjava/" in body and reg["reference"] in body and "Mama" in body
    assert con.execute("SELECT COUNT(*) FROM outbox WHERE kind='notice'").fetchone()[0] == 1
    # dvostruka prijava istim e-mailom
    assert c.post("/api/predstavljanje", data=ev_form()).status_code == 409
    # prazno ime = knjiga bez posvete
    r = c.post("/api/predstavljanje", data=ev_form(names=["", "Ivo"], email="b@example.com"))
    assert r.status_code == 200
    rid = con.execute("SELECT id FROM event_regs WHERE email='b@example.com'").fetchone()[0]
    assert E.dedications(con, rid) == ["bez posvete", "Ivo"]
    # uplata s izvoda
    csv_ = f"datum;iznos;platitelj;poziv_na_broj;opis;id_transakcije\n2026-11-20;79,80;Iva;HR00 {reg['reference']};;T-E1\n"
    res = S.import_statement(con, app.config, "test", csv_.encode())
    assert res.get("event_books") == 1
    assert con.execute("SELECT book_status FROM event_regs WHERE id=?", (reg["id"],)).fetchone()[0] == "paid"
    assert con.execute("SELECT COUNT(*) FROM outbox WHERE kind='event_book_paid'").fetchone()[0] == 1


def test_event_capacity_waitlist_and_promotion(env):
    app, c, con = env
    ev_setup(app, capacity=3)
    a = c.post("/api/predstavljanje", data=ev_form(email="a@example.com", dolazak="2")).json
    b = c.post("/api/predstavljanje", data=ev_form(email="b@example.com", dolazak="2")).json
    d = c.post("/api/predstavljanje", data=ev_form(email="d@example.com")).json
    assert (a["status"], b["status"], d["status"]) == ("confirmed", "waitlist", "waitlist")
    assert E.seats_taken(con, app.config) == 2
    tok = con.execute("SELECT token FROM event_regs WHERE email='a@example.com'").fetchone()[0]
    assert "Da, odjavljujem se" in c.get(f"/predstavljanje/odjava/{tok}").get_data(as_text=True)
    c.post(f"/predstavljanje/odjava/{tok}")
    st = dict(con.execute("SELECT email, status FROM event_regs").fetchall())
    assert st == {"a@example.com": "cancelled", "b@example.com": "confirmed", "d@example.com": "confirmed"}
    assert con.execute("SELECT COUNT(*) FROM outbox WHERE kind='event_promoted'").fetchone()[0] == 2
    assert E.seats_taken(con, app.config) == 3


def test_event_reminder_deadline_and_admin(env):
    app, c, con = env
    e = ev_setup(app)
    c.post("/api/predstavljanje", data=ev_form(names=["Teta Mira"]))
    assert E.run_reminders(con, app.config, today=_date.fromisoformat(e["date"]) - _td(days=3)) == 0
    assert E.run_reminders(con, app.config, today=_date.fromisoformat(e["date"]) - _td(days=2)) == 1
    assert E.run_reminders(con, app.config, today=_date.fromisoformat(e["date"]) - _td(days=2)) == 0
    tok = login(c)
    html = c.get("/admin/predstavljanje").get_data(as_text=True)
    assert "Iva Ivić" in html and "Teta Mira" in html and "Zauzeto mjesta: 1 / 100" in html
    rid = con.execute("SELECT id FROM event_regs").fetchone()[0]
    c.post("/admin/predstavljanje", data={"csrf": tok, "reg_id": rid, "action": "paid", "date": "2026-11-21", "amount": "39,90"})
    assert con.execute("SELECT book_status FROM event_regs").fetchone()[0] == "paid"
    for w in ("ulaz", "knjige", "cekanje"):
        assert c.get(f"/admin/predstavljanje/ispis/{w}").status_code == 200
    x = c.get("/admin/predstavljanje/izvoz.xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    assert c.get("/admin/predstavljanje/qr.png").data[:4] == b"\x89PNG"
    # rokovi
    ev_setup(app, book_until=(_date.today() - _td(days=1)).isoformat())
    r = c.post("/api/predstavljanje", data=ev_form(names=["Netko"], email="late@example.com"))
    assert r.status_code == 200 and "pay" not in r.json
    ev_setup(app, reg_until=(_date.today() - _td(days=1)).isoformat())
    assert c.post("/api/predstavljanje", data=ev_form(email="x@example.com")).status_code == 410
    assert E.purge_old(con, app.config, today=_date.fromisoformat(e["date"]) + _td(days=91)) == 2


def test_event_promo_on_homepage(env):
    app, c, con = env
    ev_setup(app)
    html = c.get("/").get_data(as_text=True)
    assert 'href="/predstavljanje"' in html and "Predstavljanje knjige POMALO" in html
    assert 'href="/predstavljanje"' in c.get("/knjiga").get_data(as_text=True)


def test_email_has_inline_payment_code(env):
    import mailer
    app, c, con = env
    ev_setup(app)
    c.post("/api/predstavljanje", data=ev_form(names=["Mama"]))
    row = con.execute("SELECT * FROM outbox WHERE kind='event_confirmation'").fetchone()
    msg = mailer.build_message(con, app.config, row)
    htmlpart = msg.get_body(("html",))
    html = htmlpart.get_content()
    assert "cid:" in html and html.index("cid:") < html.index("Iznos:")
    assert any(p.get_content_type() == "image/png" for p in htmlpart.iter_parts()) or \
        any(p.get_content_type() == "image/png" for p in msg.walk())
    assert msg.get_body(("plain",)).get_content().count("Iznos:") == 1
    assert any(p.get_filename() and p.get_filename().endswith(".pdf") for p in msg.iter_attachments())

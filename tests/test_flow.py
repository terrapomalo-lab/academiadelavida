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
         "vrsta": "regular", "clanarina": "standard", "jezik": "hr", "statut": "1", "punoljetnost": "1", "privatnost": "1"}
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
    assert "knjig" not in html.lower()
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
                                          email="janez@example.si", clanarina="student", vrsta="associate"))
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
    assert (s, f) == (1, 0)
    assert con.execute("SELECT status FROM outbox").fetchone()[0] == "sent"


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


@pytest.mark.parametrize("fee,cents", [("standard", 5000), ("student", 2000)])
def test_admission_notice_and_codes(env, fee, cents):
    app, c, con = env
    tok, aid = full_admission(app, c, con, clanarina=fee, ime="Ivana Šimić-Đurić")
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


@pytest.mark.parametrize("fee,cents", [("standard", 5000), ("student", 2000)])
def test_immediate_payment_after_application(env, fee, cents):
    app, c, con = env
    r = c.post("/api/prijava", data=form(clanarina=fee))
    pay = r.json["pay"]
    assert pay["rows"][0][2] == ("50,00" if cents == 5000 else "20,00")
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
    c.post("/api/prijava", data=form(clanarina="student"))
    a = con.execute("SELECT * FROM applications").fetchone()
    S.record_prepayment(con, app.config, "tajnik", a, date.today().isoformat(), 5000)
    S.record_decision(con, app.config, "tajnik", "admit", date.today().isoformat(), "Z", [a["id"]])
    assert con.execute("SELECT status FROM dues").fetchone()[0] == "open"
    assert con.execute("SELECT status FROM payments").fetchone()[0] == "review"

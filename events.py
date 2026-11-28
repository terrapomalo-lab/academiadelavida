"""Predstavljanje knjige POMALO: prijave dolaska, lista čekanja, predbilježba knjige s posvetom."""
import re
import secrets
from datetime import date, timedelta

import i18n
import payments as P
import services as S
from db import now_iso, audit

NO_DEDICATION = "bez posvete"
EVENT_RE = re.compile(r"(?<![\d-])8-(\d{4})-(\d{4})(?![\d-])")
PHONE_RE = re.compile(r"^\+?[\d\s/()-]{8,20}$")

SEATING_NOTE = ("Broj sjedećih mjesta u dvorani je ograničen i mjesta se ne rezerviraju unaprijed; prednost pri sjedenju imaju starije osobe. "
                "Ako se sjedeća mjesta popune, moguće je da ćete stajati.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_regs (
  id INTEGER PRIMARY KEY,
  event TEXT NOT NULL,
  seq INTEGER NOT NULL,
  reference TEXT UNIQUE NOT NULL,            -- poziv na broj HR00 8-GODINA-REDNI BROJ (za uplatu knjige)
  token TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  full_name TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT NOT NULL,
  seats INTEGER NOT NULL DEFAULT 1,          -- 1 = sam/a, 2 = s jednom osobom
  status TEXT NOT NULL,                      -- confirmed | waitlist | cancelled
  status_at TEXT,
  reminded_at TEXT,
  book_qty INTEGER NOT NULL DEFAULT 0,
  book_cents INTEGER NOT NULL DEFAULT 0,
  book_status TEXT NOT NULL DEFAULT 'none',  -- none | pending | paid | cancelled
  book_paid_date TEXT,
  payment_id INTEGER REFERENCES payments(id),
  ip TEXT,
  UNIQUE(event, seq)
);
CREATE TABLE IF NOT EXISTS event_dedications (
  id INTEGER PRIMARY KEY,
  reg_id INTEGER NOT NULL REFERENCES event_regs(id),
  pos INTEGER NOT NULL,
  name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_event_regs_status ON event_regs(event, status);
"""


def ensure_schema(con):
    con.executescript(SCHEMA)


def ev(cfg):
    return cfg["EVENT"]


def reg_open(cfg, today=None):
    return (today or date.today()).isoformat() <= ev(cfg)["reg_until"]


def books_open(cfg, today=None):
    return (today or date.today()).isoformat() <= ev(cfg)["book_until"]


def rate_limited(con, ip, limit):
    from datetime import datetime
    since = (datetime.now() - timedelta(hours=1)).astimezone().isoformat()
    return con.execute("SELECT COUNT(*) FROM event_regs WHERE ip=? AND created_at>=?", (ip, since)).fetchone()[0] >= limit


def seats_taken(con, cfg):
    return con.execute("SELECT COALESCE(SUM(seats),0) FROM event_regs WHERE event=? AND status='confirmed'",
                       (ev(cfg)["key"],)).fetchone()[0]


def seats_left(con, cfg):
    return max(0, ev(cfg)["capacity"] - seats_taken(con, cfg))


# ------------------------------------------------------------------ obrazac

def validate(form, cfg, today=None):
    errs, d = [], {}
    d["full_name"] = " ".join(form.get("ime", "").split())[:120]
    if len(d["full_name"]) < 3 or " " not in d["full_name"]:
        errs.append("upišite ime i prezime")
    d["email"] = form.get("email", "").strip().lower()[:200]
    if not S.EMAIL_RE.match(d["email"]):
        errs.append("upišite ispravnu e-mail adresu")
    d["phone"] = " ".join(form.get("mobitel", "").split())[:30]
    if not PHONE_RE.match(d["phone"]):
        errs.append("upišite broj mobitela")
    d["seats"] = 2 if form.get("dolazak") == "2" else 1
    try:
        qty = int(form.get("primjerci", "0") or 0)
    except ValueError:
        qty = 0
    names = [" ".join(n.split())[:80] for n in form.getlist("posveta")][:qty] if hasattr(form, "getlist") else []
    if qty and not books_open(cfg, today):
        qty, names = 0, []
    if not 0 <= qty <= ev(cfg)["max_copies"]:
        errs.append("odaberite broj primjeraka")
        qty = 0
    # prazno polje = primjerak bez posvete
    names = (names + [""] * qty)[:qty]
    if any(0 < len(n) < 2 for n in names):
        errs.append("ime za posvetu je prekratko")
    d["book_qty"], d["names"] = qty, [n or NO_DEDICATION for n in names]
    if not form.get("privatnost"):
        errs.append("potvrdite da ste pročitali obavijest o privatnosti")
    return d, errs


def create(con, cfg, d, ip=None):
    """Sprema prijavu; mjesto ili lista čekanja određuje se unutar iste transakcije."""
    e = ev(cfg)
    con.execute("BEGIN IMMEDIATE")
    try:
        dup = con.execute("SELECT id FROM event_regs WHERE event=? AND email=? AND status!='cancelled'",
                          (e["key"], d["email"])).fetchone()
        if dup:
            con.execute("ROLLBACK")
            return None
        promote(con, cfg)   # ako je granica podignuta, lista čekanja dobiva mjesta prije novih prijava
        before = seats_taken(con, cfg)
        status = "confirmed" if seats_left(con, cfg) >= d["seats"] and not _waitlist_nonempty(con, cfg) else "waitlist"
        seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM event_regs WHERE event=?", (e["key"],)).fetchone()[0]
        ref = f"8-{e['date'][:4]}-{seq:04d}"
        now = now_iso()
        con.execute("""INSERT INTO event_regs(event, seq, reference, token, created_at, full_name, email, phone, seats, status,
                       status_at, book_qty, book_cents, book_status, ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (e["key"], seq, ref, secrets.token_urlsafe(24), now, d["full_name"], d["email"], d["phone"], d["seats"],
                     status, now, d["book_qty"], d["book_qty"] * e["book_cents"], "pending" if d["book_qty"] else "none", ip))
        rid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        for i, n in enumerate(d["names"], 1):
            con.execute("INSERT INTO event_dedications(reg_id, pos, name) VALUES (?,?,?)", (rid, i, n))
        r = get(con, rid)
        after = seats_taken(con, cfg)
        lim = e.get("notify_at")
        if lim and before <= lim < after and cfg.get("NOTIFY_TO"):
            S.queue_email(con, "notice", cfg["NOTIFY_TO"], f"Predstavljanje: prijavljeno više od {lim} osoba",
                          f"Broj prijavljenih osoba za predstavljanje knjige prešao je {lim} (sada: {after}; najviše {e['capacity']}).\n\n"
                          f"Administracija: {cfg['BASE_URL']}/admin/predstavljanje")
        queue_confirmation(con, cfg, r)
        queue_admin_notice(con, cfg, r)
        audit(con, "web", "event_reg", f"{ref} {status}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(con, rid)


def _waitlist_nonempty(con, cfg):
    return con.execute("SELECT 1 FROM event_regs WHERE event=? AND status='waitlist' LIMIT 1", (ev(cfg)["key"],)).fetchone() is not None


def get(con, rid):
    return con.execute("SELECT * FROM event_regs WHERE id=?", (rid,)).fetchone()


def by_token(con, token):
    return con.execute("SELECT * FROM event_regs WHERE token=?", (token,)).fetchone()


def dedications(con, rid):
    return [r["name"] for r in con.execute("SELECT name FROM event_dedications WHERE reg_id=? ORDER BY pos", (rid,))]


# ------------------------------------------------------------------ odjava i lista čekanja

def cancel(con, cfg, r, actor):
    """Odjava; oslobođena mjesta odmah dobivaju prvi s liste čekanja. Pozivatelj vodi transakciju."""
    if r["status"] == "cancelled":
        return []
    con.execute("UPDATE event_regs SET status='cancelled', status_at=? WHERE id=?", (now_iso(), r["id"]))
    if r["book_status"] == "pending":
        con.execute("UPDATE event_regs SET book_status='cancelled' WHERE id=?", (r["id"],))
    audit(con, actor, "event_cancel", r["reference"])
    return promote(con, cfg) if r["status"] == "confirmed" else []


def promote(con, cfg):
    """Redom s liste čekanja dodjeljuje mjesta dok ih ima (preskače onoga za koga nema dovoljno mjesta)."""
    promoted = []
    for w in con.execute("SELECT * FROM event_regs WHERE event=? AND status='waitlist' ORDER BY id", (ev(cfg)["key"],)).fetchall():
        if seats_left(con, cfg) < w["seats"]:
            continue
        con.execute("UPDATE event_regs SET status='confirmed', status_at=? WHERE id=?", (now_iso(), w["id"]))
        w = get(con, w["id"])
        s, b = email_promoted(cfg, w)
        S.queue_email(con, "event_promoted", w["email"], s, b)
        audit(con, "system", "event_promote", w["reference"])
        promoted.append(w)
    return promoted


# ------------------------------------------------------------------ uplata za knjigu

def payment(cfg, r):
    desc = "Donacija – knjiga s posvetom"
    data = {"kind": "donation", "amount_cents": r["book_cents"], "payer_name": r["full_name"],
            "recipient_name": cfg["ORG_NAME"], "recipient_street": cfg["ORG_STREET"], "recipient_city": cfg["ORG_POSTCODE_CITY"],
            "iban": cfg["ORG_IBAN"], "bic": cfg["ORG_BIC"], "model": "HR00", "reference": r["reference"],
            "purpose": "CHAR", "description": desc}
    data["hub3"] = P.hub3_payload(**{k: data[k] for k in ("amount_cents", "payer_name", "recipient_name", "recipient_street",
                                                           "recipient_city", "iban", "model", "reference", "purpose", "description")})
    data["epc"] = P.epc_payload(amount_cents=r["book_cents"], recipient_name=cfg["ORG_NAME"], iban=cfg["ORG_IBAN"],
                                bic=cfg["ORG_BIC"], remittance=f"HR00 {r['reference']} {desc}", purpose="CHAR")
    return data


def pay_rows(cfg, r):
    t = i18n.T["hr"]
    pay = payment(cfg, r)
    return [[t["pay_amount"], P.fmt_eur(r["book_cents"], "hr"), "%d,%02d" % divmod(r["book_cents"], 100)],
            [t["pay_recipient"], f"{cfg['ORG_NAME']}, {cfg['ORG_STREET']}, {cfg['ORG_POSTCODE_CITY']}", None],
            [t["pay_iban"], cfg["ORG_IBAN"], None],
            [t["pay_model_ref"], f"HR00 {r['reference']}", None],
            [t["pay_desc"], pay["description"], None],
            ["Rok za uplatu", P.fmt_date(ev(cfg)["book_until"], "hr"), None]]


def pdf(cfg, r):
    return P.notice_pdf(cfg, payment(cfg, r), "hr", member_name=r["full_name"], public_no=r["reference"],
                        due_date=P.fmt_date(ev(cfg)["book_until"], "hr"),
                        title="Predbilježba knjige s posvetom", name_label="Donator", no_label="Predbilježba")


def mark_paid(con, cfg, actor, r, booked_date, amount_cents, payment_id=None, source_txn=None):
    if r["book_status"] != "pending":
        return False
    if payment_id is None:
        fp = S._fingerprint(source_txn, booked_date, amount_cents, r["reference"], r["full_name"], f"predstavljanje {r['id']} ručno")
        con.execute("""INSERT INTO payments(source, fingerprint, booked_date, amount_cents, payer, reference, description,
                       kind, status, created_at, created_by) VALUES ('manual',?,?,?,?,?,?,'donation','matched',?,?)""",
                    (fp, booked_date, amount_cents, r["full_name"], r["reference"], "Donacija – knjiga s posvetom", now_iso(), actor))
        payment_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("UPDATE event_regs SET book_status='paid', book_paid_date=?, payment_id=? WHERE id=?", (booked_date, payment_id, r["id"]))
    r = get(con, r["id"])
    s, b = email_book_paid(cfg, r, dedications(con, r["id"]))
    S.queue_email(con, "event_book_paid", r["email"], s, b)
    audit(con, actor, "event_book_paid", r["reference"])
    return True


def match_statement(con, ref, desc, amt):
    """Za uvoz izvoda: vraća (reg, razlog). reg je postavljen samo kad se uplata smije povezati."""
    m = EVENT_RE.search(ref) or EVENT_RE.search(desc)
    if not m:
        return None, None, False
    r = con.execute("SELECT * FROM event_regs WHERE reference=?", (m.group(0),)).fetchone()
    if not r or r["book_status"] == "none":
        return None, f"Poziv na broj {m.group(0)} ne odgovara nijednoj predbilježbi knjige.", True
    if r["book_status"] == "paid":
        return None, f"Predbilježba {m.group(0)} već je plaćena (moguća dvostruka uplata).", True
    if amt < r["book_cents"]:
        return None, f"Iznos {P.fmt_eur(amt)} manji je od {P.fmt_eur(r['book_cents'])} za predbilježbu {m.group(0)}.", True
    return r, None, True


# ------------------------------------------------------------------ podsjetnik

def run_reminders(con, cfg, today=None):
    e = ev(cfg)
    today = today or date.today()
    if today.isoformat() != (date.fromisoformat(e["date"]) - timedelta(days=e["remind_days_before"])).isoformat():
        return 0
    n = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for r in con.execute("SELECT * FROM event_regs WHERE event=? AND status='confirmed' AND reminded_at IS NULL",
                             (e["key"],)).fetchall():
            s, b = email_reminder(cfg, r, dedications(con, r["id"]))
            S.queue_email(con, "event_reminder", r["email"], s, b)
            con.execute("UPDATE event_regs SET reminded_at=? WHERE id=?", (now_iso(), r["id"]))
            n += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def purge_old(con, cfg, today=None):
    """Tri mjeseca nakon događaja briše osobne podatke prijava (uplate ostaju u evidenciji uplata)."""
    e = ev(cfg)
    if (today or date.today()) < date.fromisoformat(e["date"]) + timedelta(days=90):
        return 0
    n = con.execute("""UPDATE event_regs SET full_name='(obrisano)', email='', phone='', ip=NULL
                       WHERE event=? AND full_name!='(obrisano)'""", (e["key"],)).rowcount
    con.execute("""UPDATE event_dedications SET name='(obrisano)' WHERE reg_id IN (SELECT id FROM event_regs WHERE event=?)""", (e["key"],))
    con.commit() if con.in_transaction else None
    return n


# ------------------------------------------------------------------ e-poruke (hrvatski)

def _when(cfg):
    e = ev(cfg)
    return f"{e['weekday']}, {P.fmt_date(e['date'], 'hr')} u {e['time']}, {e['place']}"


def _cancel_link(cfg, r):
    return f"{cfg['BASE_URL']}/predstavljanje/odjava/{r['token']}"


def _book_txt(cfg, r, names):
    if not r["book_qty"]:
        return ""
    lines = "\n".join(f"  {i}. {n}" for i, n in enumerate(names, 1))
    rows = "\n".join(f"  {x[0]}: {x[1]}" for x in pay_rows(cfg, r))
    return (f"\n\nPREDBILJEŽBA KNJIGE S POSVETOM ({r['book_qty']} kom.)\nPosvete:\n{lines}\n\n"
            f"Podaci za uplatu donacije:\n{rows}\n\n"
            "Upišite točno ovaj poziv na broj, da bismo uplatu povezali s vašom predbilježbom. "
            "Knjigu s posvetom pripremamo kad uplata stigne, a preuzimate je na predstavljanju.")


def queue_confirmation(con, cfg, r):
    names = dedications(con, r["id"])
    e = ev(cfg)
    who = "Prijavili ste dolazak" + (" za sebe i još jednu osobu" if r["seats"] == 2 else "") + "."
    if r["status"] == "confirmed":
        s = "Potvrda prijave: predstavljanje knjige POMALO"
        head = f"{who} Vaše mjesto je potvrđeno.\n\n{SEATING_NOTE}"
    else:
        s = "Lista čekanja: predstavljanje knjige POMALO"
        head = (f"{who} Sva su mjesta trenutačno popunjena pa ste na listi čekanja. "
                "Ako se netko odjavi, mjesto automatski dobivate redom prijave i javljamo vam e-mailom.")
    b = (f"Dobar dan, {r['full_name']},\n\nhvala na prijavi za predstavljanje knjige „{e['title']}”.\n{head}\n\n"
         f"Kada i gdje: {_when(cfg)}.\n"
         f"Broj vaše prijave: {r['reference']}"
         + _book_txt(cfg, r, names) +
         f"\n\nAko ne možete doći, odjavite se ovdje, da mjesto dobije netko s liste čekanja:\n{_cancel_link(cfg, r)}"
         + i18n._sig("hr"))
    att = [{"kind": "event_hub3_png", "reg_id": r["id"]}] if r["book_qty"] else None
    return S.queue_email(con, "event_confirmation", r["email"], s, b, att)


def queue_admin_notice(con, cfg, r):
    if not cfg.get("NOTIFY_TO"):
        return None
    names = dedications(con, r["id"])
    b = (f"Nova prijava za predstavljanje knjige.\n\nBroj: {r['reference']}\nIme i prezime: {r['full_name']}\n"
         f"E-mail: {r['email']}\nMobitel: {r['phone']}\nOsoba: {r['seats']}\n"
         f"Status: {'mjesto potvrđeno' if r['status'] == 'confirmed' else 'LISTA ČEKANJA'}\n"
         + (f"Knjige s posvetom: {r['book_qty']} ({', '.join(names)}) · {P.fmt_eur(r['book_cents'])}\n" if r["book_qty"] else "")
         + f"Zauzeto mjesta: {seats_taken(con, cfg)} / {ev(cfg)['capacity']}\n\n"
         f"Administracija: {cfg['BASE_URL']}/admin/predstavljanje")
    return S.queue_email(con, "notice", cfg["NOTIFY_TO"], f"Predstavljanje: {r['full_name']} ({r['reference']})", b)


def email_promoted(cfg, r):
    s = "Dobili ste mjesto: predstavljanje knjige POMALO"
    b = (f"Dobar dan, {r['full_name']},\n\noslobodilo se mjesto i vaša je prijava ({r['reference']}) sada potvrđena"
         + (" za vas i još jednu osobu" if r["seats"] == 2 else "") + f".\n\nKada i gdje: {_when(cfg)}.\n\n"
         f"Ako ipak ne možete doći, odjavite se ovdje:\n{_cancel_link(cfg, r)}" + i18n._sig("hr"))
    return s, b


def email_reminder(cfg, r, names):
    s = "Podsjetnik: predstavljanje knjige POMALO"
    book = ""
    if r["book_qty"] and r["book_status"] == "paid":
        book = f"\nKnjigu s posvetom ({r['book_qty']} kom.) preuzimate na predstavljanju.\n"
    elif r["book_qty"] and r["book_status"] == "pending":
        # uplate se uvoze ručno s izvoda, pa podsjetnik ne smije tvrditi da uplata nije stigla
        book = f"\nKnjigu s posvetom ({r['book_qty']} kom.) preuzimate na predstavljanju.\n"
    b = (f"Dobar dan, {r['full_name']},\n\npodsjećamo vas na predstavljanje knjige „{ev(cfg)['title']}”.\n\n"
         f"Kada i gdje: {_when(cfg)}.\n\n{SEATING_NOTE}\n" + book +
         f"\nAko ne možete doći, molimo vas da se odjavite, kako bi mjesto dobio netko s liste čekanja:\n{_cancel_link(cfg, r)}"
         + i18n._sig("hr"))
    return s, b


def email_book_paid(cfg, r, names):
    s = "Uplata zaprimljena: knjiga s posvetom"
    b = (f"Dobar dan, {r['full_name']},\n\nzaprimili smo vašu donaciju za predbilježbu {r['reference']}. Hvala vam!\n"
         f"Knjigu s posvetom ({', '.join(names)}) preuzimate na predstavljanju: {_when(cfg)}." + i18n._sig("hr"))
    return s, b

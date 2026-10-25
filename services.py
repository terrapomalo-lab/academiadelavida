"""Poslovna logika: prijave, odluke, članarine, uplate, podsjetnici, sažetak, sigurnosne kopije."""
import csv
import gzip
import hashlib
import io
import json
import re
import secrets
import shutil
import sqlite3
from datetime import date, datetime, timedelta

from db import now_iso, audit
import i18n
import payments as P

OIB_RE = re.compile(r"^\d{11}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
REF_RE = re.compile(r"(\d{4})-(\d{4})-(\d{1,3})")


# ------------------------------------------------------------------ pomoćne

def oib_valid(oib):
    if not oib or not OIB_RE.match(oib):
        return False
    a = 10
    for ch in oib[:10]:
        a = (a + int(ch)) % 10
        if a == 0:
            a = 10
        a = (a * 2) % 11
    k = 11 - a
    if k == 10:
        k = 0
    return k == int(oib[10])


def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def period_for(start):
    end = add_months(start, 12) - timedelta(days=1)
    return start, end


def age_on(dob, today):
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def fee_cents(cfg, a, current=False):
    """Članarina u centima za prijavu/člana. Prijave prije FEES_V2_FROM plaćaju po prethodnoj odluci
    (osim kod obnove, current=True, kada vrijedi važeća odluka)."""
    legacy = (not current) and cfg.get("FEES_V2_FROM") and str(a["created_at"])[:10] < cfg["FEES_V2_FROM"]
    if legacy:
        return (cfg["FEE_STUDENT_EUR"] if a["fee_cat"] == "student" else cfg["FEE_STANDARD_EUR"]) * 100
    if a["member_type"] == "regular":
        return cfg["FEE_REGULAR_EUR"] * 100
    return (cfg["FEE_REDUCED_EUR"] if a["fee_cat"] == "student" else cfg["FEE_ASSOC_EUR"]) * 100


def fee_text(cfg, a, lang, current=False):
    cents = fee_cents(cfg, a, current)
    if cents % 100 == 0:
        eur = f"{cents // 100} €" if lang == "hr" else f"€{cents // 100}"
    else:
        eur = P.fmt_eur(cents, lang)
    return f"{eur} godišnje" if lang == "hr" else f"{eur} per year"


PACKAGES = {"assoc": ("associate", "standard"), "assoc_reduced": ("associate", "student"), "regular": ("regular", "standard")}


def labels(lang):
    if lang == "hr":
        return {"regular": "redovito", "associate": "pridruženo",
                "standard": "50 € godišnje", "student": "20 € godišnje (studenti i umirovljenici)"}
    return {"regular": "full", "associate": "associate",
            "standard": "€50 per year", "student": "€20 per year (students and retirees)"}


def period_text(start_iso, end_iso, lang):
    return f"{P.fmt_date(start_iso, lang)} – {P.fmt_date(end_iso, lang)}"


# ------------------------------------------------------------------ prijava

def validate_application(form, lang, today=None):
    """Vraća (podaci, popis ključeva pogrešaka). Poslužitelj je mjerodavan; isto provjerava i preglednik."""
    today = today or date.today()
    g = lambda k: (form.get(k) or "").strip()
    errs = []
    d = {
        "full_name": re.sub(r"\s+", " ", g("ime"))[:120],
        "email": g("email")[:200].lower(),
        "phone": g("tel")[:40] or None,
        "no_oib": 1 if form.get("bez_oib") in ("1", "on", "true") else 0,
        "oib": re.sub(r"\D", "", g("oib")) or None,
        "country": g("drzava")[:80] or None,
        "dob": g("dob"),
        "member_type": g("vrsta"),
        "fee_cat": g("clanarina"),
        "_paket": g("paket"),
        "interests": g("podrucje")[:300] or None,
        "lang": g("jezik") if g("jezik") in ("hr", "en") else lang,
        "newsletter": 1 if form.get("novosti") in ("1", "on", "true") else 0,
    }
    if len(d["full_name"]) < 3 or " " not in d["full_name"]:
        errs.append("err_name")
    if not EMAIL_RE.match(d["email"]):
        errs.append("err_email")
    if d["no_oib"]:
        d["oib"] = None
        if not d["country"]:
            errs.append("err_country")
    else:
        d["country"] = d["country"] or None
        if not oib_valid(d["oib"]):
            errs.append("err_oib")
    try:
        dob = date.fromisoformat(d["dob"])
        if dob > today or dob.year < 1900:
            raise ValueError
        if age_on(dob, today) < 18:
            errs.append("err_minor")
    except ValueError:
        errs.append("err_dob")
    paket = d.pop("_paket")
    if paket:
        if paket in PACKAGES:
            d["member_type"], d["fee_cat"] = PACKAGES[paket]
        else:
            errs.append("err_fee")
    else:
        if d["member_type"] not in ("regular", "associate"):
            errs.append("err_type")
        if d["fee_cat"] not in ("standard", "student"):
            errs.append("err_fee")
        elif d["member_type"] == "regular" and d["fee_cat"] == "student":
            errs.append("err_reduced")
    if not form.get("statut"):
        errs.append("err_statute")
    if not form.get("punoljetnost"):
        errs.append("err_adult")
    if not form.get("privatnost"):
        errs.append("err_privacy")
    return d, errs


def create_application(con, d, ip=None, source="web", created_at=None):
    """Sprema prijavu u jednoj transakciji. Vraća redak prijave."""
    created_at = created_at or now_iso()
    year = int(created_at[:4])
    con.execute("BEGIN IMMEDIATE")
    try:
        seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM applications WHERE year=?", (year,)).fetchone()[0]
        public_no = f"ADV-{year}-{seq:04d}"
        token = secrets.token_urlsafe(24)
        con.execute(
            """INSERT INTO applications (public_no, year, seq, token, created_at, lang, full_name, email, phone,
               oib, no_oib, country, dob, member_type, fee_cat, interests, statute_accepted_at, privacy_ack_at,
               needs_completion, source, ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (public_no, year, seq, token, created_at, d["lang"], d["full_name"], d["email"], d["phone"],
             d["oib"], d["no_oib"], d["country"], d["dob"], d["member_type"], d["fee_cat"], d["interests"],
             created_at, created_at, 1 if d["no_oib"] else 0, source, ip))
        app_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        if d.get("newsletter"):
            subscribe(con, d["email"], d["full_name"], d["lang"], "pristupnica")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()


def subscribe(con, email, name, lang, source):
    row = con.execute("SELECT * FROM subscribers WHERE email=?", (email,)).fetchone()
    if row:
        con.execute("UPDATE subscribers SET unsubscribed_at=NULL, consent_at=?, lang=?, name=COALESCE(?,name) WHERE id=?",
                    (now_iso(), lang, name, row["id"]))
    else:
        con.execute("INSERT INTO subscribers(email, name, lang, consent_at, source, unsub_token) VALUES (?,?,?,?,?,?)",
                    (email, name, lang, now_iso(), source, secrets.token_urlsafe(18)))


def rate_limited(con, ip, limit):
    since = (datetime.now() - timedelta(hours=1)).astimezone().isoformat()
    n = con.execute("SELECT COUNT(*) FROM applications WHERE ip=? AND created_at>=?", (ip, since)).fetchone()[0]
    return n >= limit


# ------------------------------------------------------------------ uplata (prije i nakon prijema)

def prepay_due(cfg, a):
    """Podaci za uplatu prve članarine odmah nakon prijave, prije odluke o prijemu.
    Poziv na broj jednak je onome koji dobiva prva članarina nakon prijema, pa se uplata kasnije sama poveže."""
    return {"amount_cents": fee_cents(cfg, a), "reference": P.membership_reference(a, 1),
            "period_start": None, "period_end": None, "due_date": None, "status": "open"}


def prepayment(con, a):
    return con.execute("SELECT * FROM payments WHERE status='prepaid' AND reference=? ORDER BY id LIMIT 1",
                       (P.membership_reference(a, 1),)).fetchone()


def payable(con, cfg, a, lang):
    """Stanje plaćanja za stranicu s uplatom. state: prepay | prepaid | open | paid | none"""
    if a["app_status"] == "submitted":
        if prepayment(con, a):
            return {"state": "prepaid"}
        due = prepay_due(cfg, a)
        return {"state": "prepay", "due": due, "pay": P.membership_payment(cfg, a, due, lang),
                "period": ("12 mjeseci od datuma prijema u članstvo" if lang == "hr"
                           else "12 months from the date of admission"), "due_text": None}
    due = con.execute("""SELECT d.* FROM dues d JOIN memberships m ON m.id=d.membership_id
        WHERE m.application_id=? ORDER BY (d.status='open') DESC, d.period_index DESC LIMIT 1""", (a["id"],)).fetchone()
    if not due:
        return {"state": "none"}
    st = "open" if due["status"] == "open" else ("paid" if due["status"] == "paid" else "none")
    return {"state": st, "due": due, "pay": P.membership_payment(cfg, a, due, lang),
            "period": period_text(due["period_start"], due["period_end"], lang),
            "due_text": P.fmt_date(due["due_date"], lang)}


def record_prepayment(con, cfg, actor, a, booked_date, amount_cents, note=""):
    """Ručna oznaka uplate primljene prije odluke o prijemu."""
    if a["app_status"] != "submitted":
        return False, "Prijava više ne čeka odluku; uplatu evidentirajte kod članarine."
    if prepayment(con, a):
        return False, "Uplata prije prijema već je evidentirana."
    try:
        con.execute("""INSERT INTO payments(source, fingerprint, booked_date, amount_cents, reference, description, kind,
                       status, review_reason, created_at, created_by) VALUES ('manual',?,?,?,?,?,'membership','prepaid',?,?,?)""",
                    (f"prepaid:{a['id']}", booked_date, amount_cents, P.membership_reference(a, 1), note,
                     "Uplata prije odluke o prijemu – povezuje se automatski nakon prijema", now_iso(), actor))
    except sqlite3.IntegrityError:
        return False, "Uplata prije prijema već je evidentirana."
    audit(con, actor, "prepayment_manual", f"{a['public_no']} {booked_date} {amount_cents}")
    return True, "Uplata je evidentirana. Nakon odluke o prijemu automatski će se povezati s članarinom."


# ------------------------------------------------------------------ e-poruke (red)

def queue_email(con, kind, to_addr, subject, body, attachments=None, app_id=None, status="queued"):
    con.execute(
        "INSERT INTO outbox(kind, to_addr, subject, body_text, attachments, related_app_id, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (kind, to_addr, subject, body, json.dumps(attachments or []), app_id, status, now_iso()))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def queue_confirmation(con, a, cfg=None):
    lab = labels(a["lang"])
    pay_txt = link = None
    att = []
    if cfg is not None:
        due = prepay_due(cfg, a)
        pay_txt = pay_lines(cfg, a, due, a["lang"])
        link = f"{cfg['BASE_URL']}/p/{a['token']}"
        att = [{"kind": "app_notice_pdf", "app_id": a["id"]}, {"kind": "app_hub3_png", "app_id": a["id"]}]
    fee_txt = fee_text(cfg, a, a["lang"]) if cfg is not None else lab[a["fee_cat"]]
    s, b = i18n.email_confirmation(a["lang"], a, fee_txt, lab[a["member_type"]], pay_txt, link)
    return queue_email(con, "confirmation", a["email"], s, b, att, app_id=a["id"])


def pay_lines(cfg, a, due, lang):
    hr = lang == "hr"
    lines = [
        (("Iznos" if hr else "Amount"), P.fmt_eur(due["amount_cents"], lang)),
        (("Razdoblje" if hr else "Period"), period_text(due["period_start"], due["period_end"], lang) if due["period_start"]
         else ("12 mjeseci od datuma prijema u članstvo" if hr else "12 months from the date of admission")),
        (("Rok za uplatu" if hr else "Due by"), P.fmt_date(due["due_date"], lang) if due["due_date"] else None),
        (("Primatelj" if hr else "Recipient"), f"{cfg['ORG_NAME']}, {cfg['ORG_STREET']}, {cfg['ORG_POSTCODE_CITY']}"),
        ("IBAN", cfg["ORG_IBAN"]),
        ("BIC/SWIFT", cfg["ORG_BIC"]),
        (("Model i poziv na broj" if hr else "Model and reference"), f"HR00 {due['reference']}"),
        (("Opis plaćanja" if hr else "Payment description"), f"Članarina {a['public_no']}"),
    ]
    return "\n".join(f"{k}: {v}" for k, v in lines if v)


# ------------------------------------------------------------------ odluka Upravnog odbora

def record_decision(con, cfg, actor, kind, decision_date, reference, app_ids, note=""):
    """Evidentira stvarno donesenu odluku i, za prijem, otvara članstvo i članarinu te šalje dobrodošlicu."""
    dd = date.fromisoformat(decision_date)
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("INSERT INTO decisions(kind, decision_date, reference, note, created_at, created_by) VALUES (?,?,?,?,?,?)",
                    (kind, decision_date, reference, note, now_iso(), actor))
        dec_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        done = []
        for aid in app_ids:
            a = con.execute("SELECT * FROM applications WHERE id=? AND app_status='submitted'", (aid,)).fetchone()
            if not a:
                continue
            if kind == "reject":
                con.execute("UPDATE applications SET app_status='rejected', decision_id=? WHERE id=?", (dec_id, aid))
                done.append(a["public_no"])
                continue
            con.execute("UPDATE applications SET app_status='approved', decision_id=? WHERE id=?", (dec_id, aid))
            con.execute("INSERT INTO memberships(application_id, admitted_date) VALUES (?,?)", (aid, decision_date))
            mid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            start, end = period_for(dd)
            due_date = max(date.today(), dd) + timedelta(days=cfg["PAYMENT_DUE_DAYS"])
            ref = P.membership_reference(a, 1)
            con.execute("""INSERT INTO dues(membership_id, period_index, period_start, period_end, amount_cents, reference,
                           due_date, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (mid, 1, start.isoformat(), end.isoformat(), fee_cents(cfg, a), ref,
                         due_date.isoformat(), now_iso()))
            due = con.execute("SELECT * FROM dues WHERE membership_id=? AND period_index=1", (mid,)).fetchone()
            pre = con.execute("SELECT * FROM payments WHERE status='prepaid' AND reference=? ORDER BY id LIMIT 1",
                              (due["reference"],)).fetchone()
            if pre and pre["amount_cents"] == due["amount_cents"]:
                con.execute("UPDATE payments SET status='matched', due_id=?, review_reason=NULL WHERE id=?", (due["id"], pre["id"]))
                con.execute("UPDATE dues SET status='paid', paid_at=?, payment_id=? WHERE id=?", (pre["booked_date"], pre["id"], due["id"]))
                lang = a["lang"]
                subj, body = i18n.email_welcome_paid(lang, a, P.fmt_date(decision_date, lang),
                                                     period_text(due["period_start"], due["period_end"], lang))
                queue_email(con, "welcome", a["email"], subj, body, app_id=a["id"])
            else:
                if pre:
                    con.execute("UPDATE payments SET status='review', due_id=?, review_reason=? WHERE id=?",
                                (due["id"], f"Uplata prije prijema ({P.fmt_eur(pre['amount_cents'])}) razlikuje se od članarine ({P.fmt_eur(due['amount_cents'])}).", pre["id"]))
                queue_welcome(con, cfg, a, due, decision_date)
            done.append(a["public_no"])
        audit(con, actor, f"decision_{kind}", f"{decision_date} | {reference} | {', '.join(done)}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return dec_id, done


def queue_welcome(con, cfg, a, due, decision_date):
    lang = a["lang"]
    link = f"{cfg['BASE_URL']}/p/{a['token']}"
    ref = cfg["DECISION_REF_HR"] if lang == "hr" else cfg["DECISION_REF_EN"]
    s, b = i18n.email_welcome(lang, a, due, P.fmt_date(decision_date, lang), pay_lines(cfg, a, due, lang), link, ref)
    att = [{"kind": "notice_pdf", "due_id": due["id"]}, {"kind": "hub3_png", "due_id": due["id"]}]
    queue_email(con, "welcome", a["email"], s, b, att, a["id"])
    con.execute("UPDATE dues SET notice_sent_at=? WHERE id=?", (now_iso(), due["id"]))


# ------------------------------------------------------------------ uplate

def _fingerprint(bank_txn_id, booked_date, amount_cents, reference, payer, description):
    if bank_txn_id:
        return "txn:" + bank_txn_id.strip()
    raw = "|".join(str(x or "").strip().lower() for x in (booked_date, amount_cents, reference, payer, description))
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def mark_due_paid(con, cfg, due_id, payment_id, paid_date):
    due = con.execute("SELECT * FROM dues WHERE id=?", (due_id,)).fetchone()
    con.execute("UPDATE dues SET status='paid', paid_at=?, payment_id=? WHERE id=?", (paid_date, payment_id, due_id))
    a = con.execute("""SELECT a.* FROM applications a JOIN memberships m ON m.application_id=a.id WHERE m.id=?""",
                    (due["membership_id"],)).fetchone()
    lang = a["lang"]
    s, b = i18n.email_paid(lang, a, P.fmt_eur(due["amount_cents"], lang),
                           period_text(due["period_start"], due["period_end"], lang), P.fmt_date(paid_date, lang))
    queue_email(con, "paid", a["email"], s, b, app_id=a["id"])


def record_manual_payment(con, cfg, actor, due_id, booked_date, amount_cents, note=""):
    """Ručna oznaka nakon provjere banke. Vraća (ok, poruka)."""
    due = con.execute("SELECT * FROM dues WHERE id=?", (due_id,)).fetchone()
    if not due:
        return False, "Članarina ne postoji."
    if due["status"] == "paid":
        return False, "Ova članarina je već evidentirana kao plaćena."
    fp = f"manual:{due_id}"
    con.execute("BEGIN IMMEDIATE")
    try:
        try:
            con.execute("""INSERT INTO payments(source, fingerprint, booked_date, amount_cents, reference, description, kind,
                           status, due_id, created_at, created_by) VALUES ('manual',?,?,?,?,?,'membership',?,?,?,?)""",
                        (fp, booked_date, amount_cents, due["reference"], note,
                         "matched" if amount_cents == due["amount_cents"] else "review", due_id, now_iso(), actor))
        except sqlite3.IntegrityError:
            con.execute("ROLLBACK")
            return False, "Uplata za ovu članarinu već je evidentirana."
        pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        if amount_cents == due["amount_cents"]:
            mark_due_paid(con, cfg, due_id, pid, booked_date)
            msg = "Uplata je evidentirana i potvrda je poslana u red za slanje."
        else:
            con.execute("UPDATE payments SET review_reason=? WHERE id=?",
                        (f"Iznos {P.fmt_eur(amount_cents)} razlikuje se od članarine {P.fmt_eur(due['amount_cents'])}", pid))
            msg = "Iznos se razlikuje od članarine: uplata je izdvojena za pregled, članarina nije označena kao plaćena."
        audit(con, actor, "payment_manual", f"due {due_id} {booked_date} {amount_cents}")
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return True, msg


def parse_amount(s):
    s = (s or "").strip().replace("€", "").replace("EUR", "").replace(" ", "").replace(" ", "")
    if not s:
        raise ValueError("prazan iznos")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    val = round(float(s) * 100)
    return int(val)


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y.", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"nepoznat format datuma: {s}")


COLS = {
    "date": ("datum", "date", "datum knjiženja", "datum valute", "booking date"),
    "amount": ("iznos", "amount", "uplata", "potražuje", "credit"),
    "payer": ("platitelj", "payer", "naziv platitelja", "uplatitelj"),
    "reference": ("poziv_na_broj", "poziv na broj", "reference", "poziv na broj primatelja", "referenca"),
    "description": ("opis", "description", "opis plaćanja", "svrha"),
    "txn": ("id_transakcije", "id transakcije", "transaction id", "referenca banke", "broj transakcije"),
}


def import_statement(con, cfg, actor, file_bytes):
    """Uvoz izvoda u općem CSV obliku (stupci: datum, iznos, platitelj, poziv_na_broj, opis, id_transakcije).
    Stvarni format banke mapira se kad stigne primjer izvoza."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    sample = text[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return {"error": "Datoteka je prazna."}
    head = [h.strip().lower() for h in rows[0]]
    idx = {}
    for key, names in COLS.items():
        for i, h in enumerate(head):
            if h in names:
                idx[key] = i
                break
    missing = [k for k in ("date", "amount") if k not in idx]
    if missing:
        return {"error": "Nedostaju stupci: " + ", ".join(missing) + ". Prvi redak mora sadržavati nazive stupaca."}
    res = {"matched": 0, "donations": 0, "prepaid": 0, "review": 0, "duplicates": 0, "skipped": 0, "errors": []}
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        get = lambda k: r[idx[k]].strip() if k in idx and idx[k] < len(r) else ""
        try:
            bdate = parse_date(get("date"))
            amt = parse_amount(get("amount"))
        except ValueError as e:
            res["errors"].append(f"redak {n}: {e}")
            continue
        if amt <= 0:
            res["skipped"] += 1  # isplate se ne uvoze
            continue
        payer, ref, desc, txn = get("payer"), get("reference"), get("description"), get("txn")
        fp = _fingerprint(txn, bdate, amt, ref, payer, desc)
        if con.execute("SELECT 1 FROM payments WHERE fingerprint=?", (fp,)).fetchone():
            res["duplicates"] += 1
            continue
        kind, status, due_id, reason = "unknown", "review", None, None
        m = REF_RE.search(ref) or REF_RE.search(desc)
        if m:
            due = con.execute("SELECT * FROM dues WHERE reference=?", (m.group(0),)).fetchone()
            if not due:
                y, q, idx = m.groups()
                app_row = con.execute("SELECT * FROM applications WHERE year=? AND seq=? AND app_status='submitted'",
                                      (int(y), int(q))).fetchone()
                if app_row and idx == "1" and not prepayment(con, app_row):
                    kind, status = "membership", "prepaid"
                    reason = "Uplata prije odluke o prijemu – povezuje se automatski nakon prijema"
                    ref = m.group(0)  # normalizirani poziv na broj za kasnije uparivanje
                elif app_row:
                    reason = "Uplata prije prijema za ovu prijavu već je evidentirana (moguća dvostruka evidencija)."
                else:
                    reason = f"Poziv na broj {m.group(0)} ne odgovara nijednoj članarini."
            elif due["status"] == "paid":
                reason = "Članarina s ovim pozivom na broj već je plaćena (moguća dvostruka uplata)."
                due_id = due["id"]
            elif due["amount_cents"] != amt:
                reason = f"Iznos {P.fmt_eur(amt)} razlikuje se od članarine {P.fmt_eur(due['amount_cents'])}."
                due_id = due["id"]
            else:
                kind, status, due_id = "membership", "matched", due["id"]
        elif "donac" in (desc + " " + ref).lower() or "donation" in desc.lower():
            kind, status = "donation", "matched"
        else:
            reason = "Uplata nema prepoznatljiv poziv na broj."
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("""INSERT INTO payments(source, fingerprint, bank_txn_id, booked_date, amount_cents, payer, reference,
                           description, kind, status, due_id, review_reason, created_at, created_by)
                           VALUES ('import',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (fp, txn or None, bdate, amt, payer, ref, desc, kind, status, due_id, reason, now_iso(), actor))
            pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            if kind == "membership" and status == "matched":
                mark_due_paid(con, cfg, due_id, pid, bdate)
                res["matched"] += 1
            elif kind == "donation":
                res["donations"] += 1
            elif status == "prepaid":
                res["prepaid"] += 1
            else:
                res["review"] += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    audit(con, actor, "import_statement", json.dumps(res, ensure_ascii=False))
    return res


def resolve_review(con, cfg, actor, payment_id, action, due_id=None):
    p = con.execute("SELECT * FROM payments WHERE id=? AND status='review'", (payment_id,)).fetchone()
    if not p:
        return False, "Uplata nije na popisu za pregled."
    con.execute("BEGIN IMMEDIATE")
    try:
        if action == "donation":
            con.execute("UPDATE payments SET kind='donation', status='matched', due_id=NULL WHERE id=?", (payment_id,))
            msg = "Uplata je evidentirana kao donacija."
        elif action == "ignore":
            con.execute("UPDATE payments SET status='ignored' WHERE id=?", (payment_id,))
            msg = "Uplata je označena kao zanemarena (ostaje u evidenciji)."
        elif action == "assign":
            due = con.execute("SELECT * FROM dues WHERE id=?", (due_id,)).fetchone()
            if not due or due["status"] != "open":
                con.execute("ROLLBACK")
                return False, "Odabrana članarina nije otvorena."
            con.execute("UPDATE payments SET kind='membership', status='matched', due_id=? WHERE id=?", (due_id, payment_id))
            mark_due_paid(con, cfg, due_id, payment_id, p["booked_date"])
            msg = "Uplata je povezana s članarinom i potvrda je u redu za slanje."
        else:
            con.execute("ROLLBACK")
            return False, "Nepoznata radnja."
        audit(con, actor, f"payment_review_{action}", f"payment {payment_id} due {due_id}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return True, msg


# ------------------------------------------------------------------ obnova i podsjetnici

def run_renewals(con, cfg, today=None):
    """Za aktivna članstva čije plaćeno razdoblje uskoro završava otvara sljedeće razdoblje i šalje podsjetnik."""
    today = today or date.today()
    limit = (today + timedelta(days=cfg["REMINDER_DAYS_BEFORE_END"])).isoformat()
    rows = con.execute("""
        SELECT d.*, m.application_id FROM dues d JOIN memberships m ON m.id=d.membership_id
        WHERE m.status='active' AND d.status='paid' AND d.period_end<=?
          AND d.period_index = (SELECT MAX(period_index) FROM dues d2 WHERE d2.membership_id=d.membership_id)
    """, (limit,)).fetchall()
    created = 0
    for d in rows:
        a = con.execute("SELECT * FROM applications WHERE id=?", (d["application_id"],)).fetchone()
        start = date.fromisoformat(d["period_end"]) + timedelta(days=1)
        s, e = period_for(start)
        idx = d["period_index"] + 1
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("""INSERT INTO dues(membership_id, period_index, period_start, period_end, amount_cents, reference,
                           due_date, created_at, reminder_sent_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (d["membership_id"], idx, s.isoformat(), e.isoformat(), fee_cents(cfg, a, current=True),
                         P.membership_reference(a, idx), max(s, today + timedelta(days=cfg["PAYMENT_DUE_DAYS"])).isoformat(),
                         now_iso(), now_iso()))
            due = con.execute("SELECT * FROM dues WHERE membership_id=? AND period_index=?", (d["membership_id"], idx)).fetchone()
            lang = a["lang"]
            subj, body = i18n.email_reminder(lang, a, period_text(due["period_start"], due["period_end"], lang),
                                             pay_lines(cfg, a, due, lang), f"{cfg['BASE_URL']}/p/{a['token']}")
            queue_email(con, "reminder", a["email"], subj, body,
                        [{"kind": "notice_pdf", "due_id": due["id"]}, {"kind": "hub3_png", "due_id": due["id"]}], a["id"])
            con.execute("COMMIT")
            created += 1
        except Exception:
            con.execute("ROLLBACK")
            raise
    return created


def digest(con, cfg, since_iso):
    apps = con.execute("SELECT public_no, full_name, member_type, no_oib FROM applications WHERE created_at>=? ORDER BY id",
                       (since_iso,)).fetchall()
    pays = con.execute("SELECT booked_date, amount_cents, payer, kind, status FROM payments WHERE created_at>=? ORDER BY id",
                       (since_iso,)).fetchall()
    failed = con.execute("SELECT COUNT(*) FROM outbox WHERE status='failed'").fetchone()[0]
    review = con.execute("SELECT COUNT(*) FROM payments WHERE status='review'").fetchone()[0]
    pending = con.execute("SELECT COUNT(*) FROM applications WHERE app_status='submitted'").fetchone()[0]
    if not apps and not pays and not failed and not review:
        return None
    lines = [f"Sažetak – Academia de la Vida ({date.today().strftime('%d. %m. %Y.')})", ""]
    lines.append(f"Nove prijave: {len(apps)}")
    for a in apps:
        extra = " – bez OIB-a, treba dopuna" if a["no_oib"] else ""
        lines.append(f"  · {a['public_no']} {a['full_name']} ({'redovito' if a['member_type']=='regular' else 'pridruženo'}){extra}")
    lines.append(f"Evidentirane uplate: {len(pays)}")
    for p in pays:
        k = {"membership": "članarina", "donation": "donacija"}.get(p["kind"], "nepoznato")
        lines.append(f"  · {p['booked_date']} {P.fmt_eur(p['amount_cents'])} {p['payer'] or ''} ({k}, {p['status']})")
    lines += ["", f"Prijave koje čekaju odluku Upravnog odbora: {pending}",
              f"Uplate za pregled: {review}", f"Neposlane poruke: {failed}", "",
              f"Administracija: {cfg['BASE_URL']}/admin"]
    return "\n".join(lines)


# ------------------------------------------------------------------ sigurnosna kopija

def backup(db_path, backup_dir, keep):
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    tmp = backup_dir / f"adv-{stamp}.sqlite3"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(tmp))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    chk = sqlite3.connect(str(tmp))
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    chk.close()
    if ok != "ok":
        raise RuntimeError("Provjera kopije nije uspjela: " + ok)
    gz = tmp.with_suffix(".sqlite3.gz")
    with open(tmp, "rb") as f, gzip.open(gz, "wb") as g:
        shutil.copyfileobj(f, g)
    tmp.unlink()
    files = sorted(backup_dir.glob("adv-*.sqlite3.gz"))
    for old in files[:-keep]:
        old.unlink()
    return gz

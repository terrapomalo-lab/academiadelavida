"""Academia de la Vida – web stranica, pristupnica, uplate i administracija.
Pokretanje (razvoj):  flask --app app run    |    Server: gunicorn 'app:create_app()'"""
import csv
import functools
import hmac
import io
import os
import secrets
from datetime import date, datetime, timedelta

import click
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash

import db as DB
import i18n
import mailer
import payments as P
import services as S
from config import Config


def create_app(overrides=None):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    if not app.config.get("SECRET_KEY"):
        if app.debug or app.config.get("TESTING"):
            app.config["SECRET_KEY"] = secrets.token_hex(32)
        else:
            raise RuntimeError("ADV_SECRET_KEY nije postavljen.")
    app.config["DATA_DIR"].mkdir(parents=True, exist_ok=True)
    DB.init_db(app.config["DB_PATH"])
    app.teardown_appcontext(DB.close_db)
    cfg = app.config

    # ------------------------------------------------------------ predlošci
    @app.context_processor
    def inject():
        return {"cfg": cfg, "fmt_eur": P.fmt_eur, "fmt_date": P.fmt_date, "csrf_token": csrf_token}

    @app.template_filter("linkify")
    def linkify(s, placeholder, url, label):
        """Umetne poveznicu u prevedeni tekst; sve se ispravno escapea."""
        link = Markup('<a href="{}" target="_blank" rel="noopener">{}</a>').format(url, label)
        return Markup(escape(s)).replace("{" + placeholder + "}", link)

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; font-src 'self'; "
            "script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
        if request.path.startswith(("/admin", "/p/")):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Robots-Tag"] = "noindex"
        return resp

    def client_ip():
        return request.headers.get("X-Real-IP") or request.remote_addr or "?"

    # ------------------------------------------------------------ javne stranice
    def page(lang):
        return render_template("index.html", t=i18n.T[lang], lang=lang,
                               alt_url=url_for("index_en") if lang == "hr" else url_for("index_hr"),
                               privacy_url=url_for("privacy_hr") if lang == "hr" else url_for("privacy_en"))

    @app.get("/")
    def index_hr():
        return page("hr")

    @app.get("/en/")
    def index_en():
        return page("en")

    @app.get("/en")
    def index_en_noslash():
        return redirect(url_for("index_en"), 301)

    @app.get("/privatnost")
    def privacy_hr():
        return render_template("privacy.html", t=i18n.T["hr"], lang="hr", alt_url=url_for("privacy_en"),
                               home=url_for("index_hr"))

    @app.get("/en/privacy")
    def privacy_en():
        return render_template("privacy.html", t=i18n.T["en"], lang="en", alt_url=url_for("privacy_hr"),
                               home=url_for("index_en"))

    @app.get("/dokumenti/statut.pdf")
    def statute_pdf():
        f = cfg["DOCS_DIR"] / "statut.pdf"
        if not f.exists():
            abort(404)
        return send_file(f, mimetype="application/pdf", download_name="Statut-Academia-de-la-Vida.pdf")

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nDisallow: /admin\nDisallow: /p/\nDisallow: /odjava/\n", mimetype="text/plain")

    # ------------------------------------------------------------ pristupnica
    def handle_application(lang):
        con = DB.get_db()
        t = i18n.T[lang]
        if request.form.get("web"):  # zamka za robote (skriveno polje)
            return {"ok": False, "errors": ["err_server"]}, 400
        if S.rate_limited(con, client_ip(), cfg["MAX_SUBMISSIONS_PER_IP_PER_HOUR"]):
            return {"ok": False, "message": t["err_rate"]}, 429
        d, errs = S.validate_application(request.form, lang)
        if errs:
            return {"ok": False, "errors": errs, "message": t["err_prefix"] + " " + "; ".join(t[e] for e in errs) + "."}, 400
        try:
            a = S.create_application(con, d, ip=client_ip())
        except Exception:
            app.logger.exception("Spremanje prijave nije uspjelo")
            return {"ok": False, "message": t["err_server"].replace("{email}", cfg["ORG_EMAIL"])}, 500
        # Prijava je trajno spremljena; potvrda se šalje kroz red poruka.
        sent = False
        try:
            con.execute("BEGIN IMMEDIATE")
            oid = S.queue_confirmation(con, a, cfg)
            DB.audit(con, "web", "application", a["public_no"])
            con.execute("COMMIT")
            s, _f = mailer.send_outbox(con, cfg, ids=[oid], batch=1, include_failed=False)
            sent = s == 1
        except Exception:
            app.logger.exception("Potvrda nije poslana; prijava je spremljena")
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        parts = [t["ok_p"].replace("{no}", a["public_no"]),
                 (t["ok_mail_sent"] if sent else t["ok_mail_later"]).replace("{email}", a["email"]),
                 t["ok_next"]]
        if a["no_oib"]:
            parts.append(t["ok_no_oib"])
        due = S.prepay_due(cfg, a)
        pay = P.membership_payment(cfg, a, due, lang)
        payinfo = {
            "title": t["ok_pay_h"],
            "rows": [[t["pay_amount"], P.fmt_eur(due["amount_cents"], lang),
                      ("%d,%02d" if lang == "hr" else "%d.%02d") % divmod(due["amount_cents"], 100)],
                     [t["pay_recipient"], f"{cfg['ORG_NAME']}, {cfg['ORG_STREET']}, {cfg['ORG_POSTCODE_CITY']}", None],
                     [t["pay_iban"], cfg["ORG_IBAN"], None],
                     [t["pay_model_ref"], f"HR00 {due['reference']}", None],
                     [t["pay_desc"], pay["description"], None]],
            "hub3": url_for("pay_hub3", token=a["token"]),
            "hub3_dl": url_for("pay_hub3", token=a["token"], dl=1),
            "epc": url_for("pay_epc", token=a["token"]),
            "pdf": url_for("pay_pdf", token=a["token"], lang=lang),
            "page": cfg["BASE_URL"] + url_for("pay_page", token=a["token"]),
            "page_label": t["ok_pay_page"],
        }
        return {"ok": True, "public_no": a["public_no"], "mail_sent": sent, "title": t["ok_h"], "paragraphs": parts,
                "pay": payinfo}, 200

    @app.post("/api/prijava")
    def api_apply():
        lang = request.form.get("lang") if request.form.get("lang") in ("hr", "en") else "hr"
        body, code = handle_application(lang)
        return jsonify(body), code

    @app.post("/prijava")
    def apply_nojs():
        lang = request.form.get("lang") if request.form.get("lang") in ("hr", "en") else "hr"
        body, code = handle_application(lang)
        return render_template("result.html", t=i18n.T[lang], lang=lang, body=body,
                               home=url_for("index_hr") if lang == "hr" else url_for("index_en")), code

    # ------------------------------------------------------------ uplata članarine
    def app_by_token(token):
        a = DB.get_db().execute("SELECT * FROM applications WHERE token=?", (token,)).fetchone()
        if not a:
            abort(404)
        return a

    def _png(data, name, download):
        return send_file(io.BytesIO(data), mimetype="image/png", as_attachment=download, download_name=name)

    def _payable(a, lang=None):
        return S.payable(DB.get_db(), cfg, a, lang or a["lang"])

    @app.get("/p/<token>")
    def pay_page(token):
        a = app_by_token(token)
        lang = request.args.get("lang") if request.args.get("lang") in ("hr", "en") else a["lang"]
        st = _payable(a, lang)
        return render_template("pay.html", t=i18n.T[lang], lang=lang, a=a, st=st,
                               alt_url=url_for("pay_page", token=token, lang=i18n.T[lang]["other_lang"]),
                               home=url_for("index_hr") if lang == "hr" else url_for("index_en"))

    def _open_pay(token):
        a = app_by_token(token)
        st = _payable(a)
        if st["state"] not in ("prepay", "open"):
            abort(404)
        return a, st

    @app.get("/p/<token>/hub3.png")
    def pay_hub3(token):
        a, st = _open_pay(token)
        return _png(P.hub3_png(st["pay"]["hub3"], scale=4), f"kod-za-placanje-{a['public_no']}.png", request.args.get("dl") == "1")

    @app.get("/p/<token>/epc.png")
    def pay_epc(token):
        a, st = _open_pay(token)
        return _png(P.epc_png(st["pay"]["epc"]), f"epc-qr-{a['public_no']}.png", request.args.get("dl") == "1")

    @app.get("/p/<token>/obavijest.pdf")
    def pay_pdf(token):
        a = app_by_token(token)
        lang = request.args.get("lang") if request.args.get("lang") in ("hr", "en") else a["lang"]
        st = _payable(a, lang)
        if st["state"] not in ("prepay", "open", "paid"):
            abort(404)
        pdf = P.notice_pdf(cfg, st["pay"], lang, member_name=a["full_name"], public_no=a["public_no"],
                           period=st["period"], due_date=st["due_text"])
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True,
                         download_name=f"obavijest-o-uplati-{a['public_no']}.pdf")

    # ------------------------------------------------------------ donacija
    def donation_amount():
        try:
            cents = S.parse_amount(request.args.get("iznos", ""))
        except ValueError:
            abort(400)
        if not (100 <= cents <= 1_000_000):
            abort(400)
        return cents

    @app.get("/donacija/hub3.png")
    def don_hub3():
        pay = P.donation_payment(cfg, donation_amount())
        return _png(P.hub3_png(pay["hub3"], scale=4), "donacija-kod.png", request.args.get("dl") == "1")

    @app.get("/donacija/epc.png")
    def don_epc():
        pay = P.donation_payment(cfg, donation_amount())
        return _png(P.epc_png(pay["epc"]), "donacija-epc-qr.png", request.args.get("dl") == "1")

    @app.get("/donacija/uplatnica.pdf")
    def don_pdf():
        lang = request.args.get("lang") if request.args.get("lang") in ("hr", "en") else "hr"
        pay = P.donation_payment(cfg, donation_amount())
        return send_file(io.BytesIO(P.notice_pdf(cfg, pay, lang)), mimetype="application/pdf", as_attachment=True,
                         download_name="donacija-academia-de-la-vida.pdf")

    # ------------------------------------------------------------ odjava s newslettera
    @app.route("/odjava/<token>", methods=["GET", "POST"])
    def unsubscribe(token):
        con = DB.get_db()
        sub = con.execute("SELECT * FROM subscribers WHERE unsub_token=?", (token,)).fetchone()
        lang = sub["lang"] if sub else "hr"
        state = "bad" if not sub else ("done" if sub["unsubscribed_at"] else "confirm")
        if sub and request.method == "POST":
            con.execute("UPDATE subscribers SET unsubscribed_at=? WHERE id=?", (DB.now_iso(), sub["id"]))
            state = "done"
        return render_template("unsubscribe.html", t=i18n.T[lang], lang=lang, state=state,
                               home=url_for("index_hr") if lang == "hr" else url_for("index_en"))

    # ================================================================== ADMINISTRACIJA
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def check_csrf():
        tok = request.form.get("csrf", "")
        if not tok or not hmac.compare_digest(tok, session.get("csrf", "")):
            abort(400, "Sigurnosni token nije valjan. Osvježite stranicu i pokušajte ponovno.")

    def admin_required(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            if not session.get("admin"):
                return redirect(url_for("admin_login", next=request.path))
            if request.method == "POST":
                check_csrf()
            g.admin = session["admin"]
            return fn(*a, **kw)
        return wrap

    @app.route("/admin/prijava", methods=["GET", "POST"])
    def admin_login():
        con = DB.get_db()
        error = None
        if request.method == "POST":
            check_csrf()
            ip = client_ip()
            since = (datetime.now() - timedelta(minutes=15)).astimezone().isoformat()
            fails = con.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND ok=0 AND at>=?", (ip, since)).fetchone()[0]
            if fails >= 5:
                error = "Previše neuspjelih pokušaja. Pokušajte ponovno za 15 minuta."
            else:
                u = con.execute("SELECT * FROM admins WHERE username=?", (request.form.get("username", "").strip(),)).fetchone()
                ok = bool(u and check_password_hash(u["pw_hash"], request.form.get("password", "")))
                con.execute("INSERT INTO login_attempts(ip, at, ok) VALUES (?,?,?)", (ip, DB.now_iso(), int(ok)))
                if ok:
                    session.clear()
                    session.permanent = True
                    session["admin"] = u["username"]
                    csrf_token()
                    DB.audit(con, u["username"], "login", ip)
                    nxt = request.args.get("next", "")
                    return redirect(nxt if nxt.startswith("/admin") else url_for("admin_home"))
                error = "Korisničko ime ili lozinka nisu ispravni."
        return render_template("admin/login.html", error=error)

    @app.post("/admin/odjava")
    @admin_required
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    @app.get("/admin")
    @admin_required
    def admin_home():
        con = DB.get_db()
        today = date.today().isoformat()
        soon = (date.today() + timedelta(days=30)).isoformat()
        c = lambda q, *p: con.execute(q, p).fetchone()[0]
        stats = {
            "submitted": c("SELECT COUNT(*) FROM applications WHERE app_status='submitted'"),
            "needs_completion": c("SELECT COUNT(*) FROM applications WHERE needs_completion=1 AND app_status IN ('submitted','approved')"),
            "members": c("SELECT COUNT(*) FROM memberships WHERE status='active'"),
            "open_dues": c("SELECT COUNT(*) FROM dues d JOIN memberships m ON m.id=d.membership_id WHERE d.status='open' AND m.status='active'"),
            "overdue": c("SELECT COUNT(*) FROM dues d JOIN memberships m ON m.id=d.membership_id WHERE d.status='open' AND m.status='active' AND d.due_date<?", today),
            "expiring": c("""SELECT COUNT(*) FROM dues d JOIN memberships m ON m.id=d.membership_id WHERE m.status='active' AND d.status='paid'
                             AND d.period_end BETWEEN ? AND ? AND d.period_index=(SELECT MAX(period_index) FROM dues x WHERE x.membership_id=d.membership_id)""", today, soon),
            "review": c("SELECT COUNT(*) FROM payments WHERE status='review'"),
            "prepaid": c("SELECT COUNT(*) FROM payments WHERE status='prepaid'"),
            "failed": c("SELECT COUNT(*) FROM outbox WHERE status='failed'"),
            "queued": c("SELECT COUNT(*) FROM outbox WHERE status='queued'"),
            "subscribers": c("SELECT COUNT(*) FROM subscribers WHERE unsubscribed_at IS NULL"),
        }
        return render_template("admin/home.html", s=stats)

    # --- prijave i odluke
    @app.get("/admin/prijave")
    @admin_required
    def admin_applications():
        con = DB.get_db()
        rows = con.execute("SELECT * FROM applications WHERE app_status='submitted' ORDER BY id").fetchall()
        paid = {r["id"]: S.prepayment(con, r) for r in rows}
        return render_template("admin/applications.html", rows=rows, paid=paid, today=date.today().isoformat())

    @app.post("/admin/odluka/pregled")
    @admin_required
    def admin_decision_review():
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
        kind = request.form.get("kind")
        if not ids or kind not in ("admit", "reject"):
            flash("Odaberite barem jednu prijavu i vrstu odluke.", "err")
            return redirect(url_for("admin_applications"))
        rows = DB.get_db().execute(f"SELECT * FROM applications WHERE app_status='submitted' AND id IN ({','.join('?'*len(ids))}) ORDER BY id", ids).fetchall()
        con = DB.get_db()
        paid = {r["id"]: S.prepayment(con, r) for r in rows}
        return render_template("admin/decision_confirm.html", rows=rows, kind=kind, paid=paid, today=date.today().isoformat())

    @app.post("/admin/odluka")
    @admin_required
    def admin_decision():
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
        kind = request.form.get("kind")
        ddate = request.form.get("decision_date", "")
        ref = request.form.get("reference", "").strip()
        if not request.form.get("confirm") or not ids or kind not in ("admit", "reject") or not ref:
            flash("Za evidentiranje odluke potrebni su datum, oznaka (npr. zapisnik sjednice) i potvrda.", "err")
            return redirect(url_for("admin_applications"))
        try:
            dd = date.fromisoformat(ddate)
            if dd > date.today():
                raise ValueError
        except ValueError:
            flash("Datum odluke nije ispravan (ne može biti u budućnosti).", "err")
            return redirect(url_for("admin_applications"))
        con = DB.get_db()
        dec_id, done = S.record_decision(con, cfg, g.admin, kind, ddate, ref, ids, request.form.get("note", ""))
        if kind == "admit":
            s, f = mailer.send_outbox(con, cfg, batch=100)
            flash(f"Evidentirana odluka o prijemu ({len(done)} osoba). Poslano poruka: {s}, neuspjelo: {f}.", "ok" if not f else "err")
        else:
            flash(f"Evidentirana odluka o odbijanju ({len(done)} prijava). Automatska poruka se ne šalje.", "ok")
        return redirect(url_for("admin_decision_view", dec_id=dec_id))

    @app.get("/admin/odluke")
    @admin_required
    def admin_decisions():
        con = DB.get_db()
        rows = con.execute("""SELECT d.*, (SELECT COUNT(*) FROM applications a WHERE a.decision_id=d.id) n
                              FROM decisions d ORDER BY d.decision_date DESC, d.id DESC""").fetchall()
        return render_template("admin/decisions.html", rows=rows)

    @app.get("/admin/odluke/<int:dec_id>")
    @admin_required
    def admin_decision_view(dec_id):
        con = DB.get_db()
        d = con.execute("SELECT * FROM decisions WHERE id=?", (dec_id,)).fetchone() or abort(404)
        people = con.execute("SELECT * FROM applications WHERE decision_id=? ORDER BY full_name", (dec_id,)).fetchall()
        return render_template("admin/decision_view.html", d=d, people=people)

    # --- članovi
    @app.get("/admin/clanovi")
    @admin_required
    def admin_members():
        con = DB.get_db()
        f = request.args.get("f", "all")
        q = """SELECT a.*, m.id mid, m.status mstatus, m.admitted_date, m.ended_date,
                 d.id did, d.status dstatus, d.period_start, d.period_end, d.due_date, d.amount_cents, d.reference
               FROM applications a JOIN memberships m ON m.application_id=a.id
               LEFT JOIN dues d ON d.id=(SELECT id FROM dues x WHERE x.membership_id=m.id ORDER BY period_index DESC LIMIT 1)"""
        where, p = [], []
        today = date.today().isoformat()
        if f == "unpaid":
            where.append("d.status='open' AND m.status='active'")
        elif f == "overdue":
            where.append("d.status='open' AND m.status='active' AND d.due_date<?")
            p.append(today)
        elif f == "expiring":
            where.append("d.status='paid' AND m.status='active' AND d.period_end<=?")
            p.append((date.today() + timedelta(days=30)).isoformat())
        elif f == "completion":
            where.append("a.needs_completion=1")
        elif f == "ended":
            where.append("m.status='ended'")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY a.full_name COLLATE NOCASE"
        rows = con.execute(q, p).fetchall()
        return render_template("admin/members.html", rows=rows, f=f, today=today)

    @app.route("/admin/clan/<int:app_id>", methods=["GET", "POST"])
    @admin_required
    def admin_member(app_id):
        con = DB.get_db()
        a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone() or abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            if action == "update":
                oib = "".join(ch for ch in request.form.get("oib", "") if ch.isdigit()) or None
                if oib and not S.oib_valid(oib):
                    flash("OIB nije ispravan.", "err")
                else:
                    con.execute("""UPDATE applications SET full_name=?, email=?, phone=?, oib=?, no_oib=?, country=?, dob=?,
                                   needs_completion=?, admin_note=?, lang=? WHERE id=?""",
                                (request.form["full_name"].strip(), request.form["email"].strip().lower(),
                                 request.form.get("phone") or None, oib, 0 if oib else a["no_oib"],
                                 request.form.get("country") or None, request.form["dob"],
                                 1 if request.form.get("needs_completion") else 0, request.form.get("admin_note") or None,
                                 request.form.get("lang") if request.form.get("lang") in ("hr", "en") else a["lang"], app_id))
                    DB.audit(con, g.admin, "update_member", a["public_no"])
                    pkg = S.PACKAGES.get(request.form.get("paket", ""))
                    if pkg and pkg != (a["member_type"], a["fee_cat"]):
                        con.execute("UPDATE applications SET member_type=?, fee_cat=? WHERE id=?", (pkg[0], pkg[1], app_id))
                        a2 = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
                        con.execute("""UPDATE dues SET amount_cents=? WHERE status='open' AND membership_id IN
                                       (SELECT id FROM memberships WHERE application_id=?)""", (S.fee_cents(cfg, a2, current=True), app_id))
                        DB.audit(con, g.admin, "change_package", f"{a['public_no']} {request.form.get('paket')}")
                        flash("Podaci su spremljeni. Članstvo je promijenjeno; nova obavijest o uplati može se poslati ispod.", "ok")
                    else:
                        flash("Podaci su spremljeni.", "ok")
            elif action == "paid":
                try:
                    cents = S.parse_amount(request.form.get("amount", ""))
                    bdate = S.parse_date(request.form.get("booked_date", ""))
                except ValueError as e:
                    flash(f"Neispravan unos: {e}", "err")
                    return redirect(url_for("admin_member", app_id=app_id))
                ok, msg = S.record_manual_payment(con, cfg, g.admin, int(request.form["due_id"]), bdate, cents,
                                                  request.form.get("note", ""))
                if ok:
                    mailer.send_outbox(con, cfg, batch=10)
                flash(msg, "ok" if ok else "err")
            elif action == "prepaid":
                try:
                    cents = S.parse_amount(request.form.get("amount", ""))
                    bdate = S.parse_date(request.form.get("booked_date", ""))
                except ValueError as e:
                    flash(f"Neispravan unos: {e}", "err")
                    return redirect(url_for("admin_member", app_id=app_id))
                ok, msg = S.record_prepayment(con, cfg, g.admin, a, bdate, cents, request.form.get("note", ""))
                flash(msg, "ok" if ok else "err")
            elif action == "resend_notice":
                due = con.execute("SELECT * FROM dues WHERE id=?", (int(request.form["due_id"]),)).fetchone()
                if due and due["status"] == "open":
                    con.execute("BEGIN IMMEDIATE")
                    S.queue_welcome(con, cfg, a, due, con.execute("SELECT admitted_date FROM memberships WHERE application_id=?", (app_id,)).fetchone()[0])
                    con.execute("COMMIT")
                    s, f = mailer.send_outbox(con, cfg, batch=10)
                    flash("Obavijest za uplatu ponovno je poslana." if s else "Poruka je u redu za slanje (slanje trenutno nije uspjelo).", "ok" if s else "err")
            elif action == "end":
                con.execute("UPDATE memberships SET status='ended', ended_date=?, ended_reason=? WHERE application_id=?",
                            (request.form.get("ended_date") or date.today().isoformat(), request.form.get("ended_reason", ""), app_id))
                con.execute("UPDATE dues SET status='cancelled' WHERE status='open' AND membership_id=(SELECT id FROM memberships WHERE application_id=?)", (app_id,))
                DB.audit(con, g.admin, "end_membership", f"{a['public_no']} {request.form.get('ended_reason','')}")
                flash("Članstvo je evidentirano kao prestalo.", "ok")
            return redirect(url_for("admin_member", app_id=app_id))
        m = con.execute("SELECT * FROM memberships WHERE application_id=?", (app_id,)).fetchone()
        dues = con.execute("SELECT * FROM dues WHERE membership_id=? ORDER BY period_index DESC", (m["id"],)).fetchall() if m else []
        mails = con.execute("SELECT * FROM outbox WHERE related_app_id=? ORDER BY id DESC", (app_id,)).fetchall()
        dec = con.execute("SELECT * FROM decisions WHERE id=?", (a["decision_id"],)).fetchone() if a["decision_id"] else None
        pre = S.prepayment(con, a)
        return render_template("admin/member.html", a=a, m=m, dues=dues, mails=mails, dec=dec, pre=pre,
                               prepay=S.prepay_due(cfg, a), today=date.today().isoformat())

    @app.route("/admin/clan/novi", methods=["GET", "POST"])
    @admin_required
    def admin_member_new():
        """Ručni unos postojećeg člana (npr. članovi primljeni prije pokretanja stranice)."""
        con = DB.get_db()
        if request.method == "POST":
            f = request.form
            form = dict(f)
            form.update({"statut": "1", "punoljetnost": "1", "privatnost": "1"})
            d, errs = S.validate_application(form, f.get("jezik", "hr"), public=False)
            errs = [e for e in errs if e not in ("err_minor",)]
            try:
                adm = date.fromisoformat(f.get("admitted_date", ""))
                pstart = date.fromisoformat(f.get("period_start", "") or f.get("admitted_date", ""))
            except ValueError:
                errs.append("datum prijema / početak razdoblja")
            if errs:
                flash("Provjerite: " + ", ".join(i18n.T["hr"].get(e, e) for e in errs), "err")
                return render_template("admin/member_new.html", f=f)
            created = adm.isoformat() + "T00:00:00"
            a = S.create_application(con, d, source="manual", created_at=datetime.now().astimezone().replace(microsecond=0).isoformat())
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE applications SET app_status='approved' WHERE id=?", (a["id"],))
            con.execute("INSERT INTO memberships(application_id, admitted_date) VALUES (?,?)", (a["id"], adm.isoformat()))
            mid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            s_, e_ = S.period_for(pstart)
            con.execute("""INSERT INTO dues(membership_id, period_index, period_start, period_end, amount_cents, reference, due_date, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""", (mid, 1, s_.isoformat(), e_.isoformat(), S.fee_cents(cfg, a),
                                                       P.membership_reference(a, 1), (date.today() + timedelta(days=cfg["PAYMENT_DUE_DAYS"])).isoformat(), DB.now_iso()))
            DB.audit(con, g.admin, "manual_member", a["public_no"])
            con.execute("COMMIT")
            due = con.execute("SELECT * FROM dues WHERE membership_id=?", (mid,)).fetchone()
            if f.get("already_paid"):
                ok, msg = S.record_manual_payment(con, cfg, g.admin, due["id"], S.parse_date(f.get("paid_date") or date.today().isoformat()),
                                                  due["amount_cents"], "unos postojećeg člana")
                con.execute("UPDATE outbox SET status='held' WHERE related_app_id=? AND kind='paid' AND status='queued'", (a["id"],))
            elif f.get("send_notice"):
                con.execute("BEGIN IMMEDIATE")
                S.queue_welcome(con, cfg, a, due, adm.isoformat())
                con.execute("COMMIT")
                mailer.send_outbox(con, cfg, batch=10)
            flash(f"Član je upisan pod brojem {a['public_no']}.", "ok")
            return redirect(url_for("admin_member", app_id=a["id"]))
        return render_template("admin/member_new.html", f={})

    # --- uplate
    @app.route("/admin/uplate", methods=["GET", "POST"])
    @admin_required
    def admin_payments():
        con = DB.get_db()
        result = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "import":
                up = request.files.get("file")
                if not up or not up.filename:
                    flash("Odaberite datoteku izvoda.", "err")
                else:
                    result = S.import_statement(con, cfg, g.admin, up.read())
                    if "error" in result:
                        flash(result["error"], "err")
                        result = None
                    else:
                        mailer.send_outbox(con, cfg, batch=100)
            elif action in ("assign", "donation", "ignore"):
                ok, msg = S.resolve_review(con, cfg, g.admin, int(request.form["payment_id"]), action,
                                           int(request.form["due_id"]) if request.form.get("due_id", "").isdigit() else None)
                if ok:
                    mailer.send_outbox(con, cfg, batch=10)
                flash(msg, "ok" if ok else "err")
                return redirect(url_for("admin_payments"))
        review = con.execute("SELECT * FROM payments WHERE status='review' ORDER BY booked_date").fetchall()
        prepaid = con.execute("SELECT * FROM payments WHERE status='prepaid' ORDER BY booked_date").fetchall()
        open_dues = con.execute("""SELECT d.*, a.full_name, a.public_no FROM dues d JOIN memberships m ON m.id=d.membership_id
                                   JOIN applications a ON a.id=m.application_id WHERE d.status='open' ORDER BY a.full_name""").fetchall()
        recent = con.execute("""SELECT p.*, a.full_name FROM payments p LEFT JOIN dues d ON d.id=p.due_id
                                LEFT JOIN memberships m ON m.id=d.membership_id LEFT JOIN applications a ON a.id=m.application_id
                                ORDER BY p.id DESC LIMIT 100""").fetchall()
        return render_template("admin/payments.html", review=review, prepaid=prepaid, open_dues=open_dues, recent=recent, result=result)

    # --- poruke
    @app.route("/admin/poruke", methods=["GET", "POST"])
    @admin_required
    def admin_outbox():
        con = DB.get_db()
        if request.method == "POST":
            if request.form.get("action") == "retry":
                con.execute("UPDATE outbox SET attempts=0 WHERE status='failed'")
                s, f = mailer.send_outbox(con, cfg, batch=100)
                flash(f"Poslano: {s}, neuspjelo: {f}.", "ok" if not f else "err")
            elif request.form.get("action") == "release":
                con.execute("UPDATE outbox SET status='queued', attempts=0, last_error=NULL WHERE id=? AND status='held'", (int(request.form["id"]),))
                mailer.send_outbox(con, cfg, batch=10)
                flash("Poruka je puštena u slanje.", "ok")
            return redirect(url_for("admin_outbox"))
        rows = con.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 300").fetchall()
        return render_template("admin/outbox.html", rows=rows)

    # --- newsletter
    @app.route("/admin/newsletter", methods=["GET", "POST"])
    @admin_required
    def admin_newsletter():
        con = DB.get_db()
        if request.method == "POST":
            action = request.form.get("action")
            if action == "save":
                subj, body, lang = request.form.get("subject", "").strip(), request.form.get("body", "").strip(), request.form.get("lang")
                if not subj or not body or lang not in ("hr", "en"):
                    flash("Upišite naslov, tekst i odaberite jezik.", "err")
                else:
                    con.execute("INSERT INTO newsletters(subject, body, lang, created_at, created_by) VALUES (?,?,?,?,?)",
                                (subj, body, lang, DB.now_iso(), g.admin))
                    flash("Nacrt je spremljen. Pošaljite ga tek nakon odobrenja sadržaja.", "ok")
            elif action == "send":
                nid = int(request.form["id"])
                n = con.execute("SELECT * FROM newsletters WHERE id=? AND status='draft'", (nid,)).fetchone()
                approver = request.form.get("approved_by", "").strip()
                if not n or not request.form.get("approved") or not approver:
                    flash("Za slanje potvrdite da je sadržaj odobren i upišite tko ga je odobrio.", "err")
                else:
                    subs = con.execute("SELECT * FROM subscribers WHERE unsubscribed_at IS NULL AND lang=?", (n["lang"],)).fetchall()
                    con.execute("BEGIN IMMEDIATE")
                    for s_ in subs:
                        body = n["body"] + i18n.unsub_footer(n["lang"], f"{cfg['BASE_URL']}/odjava/{s_['unsub_token']}")
                        S.queue_email(con, "newsletter", s_["email"], n["subject"], body)
                    con.execute("UPDATE newsletters SET status='sent', sent_at=?, recipients=?, approved_by=? WHERE id=?",
                                (DB.now_iso(), len(subs), approver, nid))
                    DB.audit(con, g.admin, "newsletter_send", f"{nid} -> {len(subs)} (odobrio: {approver})")
                    con.execute("COMMIT")
                    flash(f"Newsletter je stavljen u red za slanje ({len(subs)} primatelja). Šalje se postupno.", "ok")
            elif action == "delete":
                con.execute("DELETE FROM newsletters WHERE id=? AND status='draft'", (int(request.form["id"]),))
                flash("Nacrt je obrisan.", "ok")
            return redirect(url_for("admin_newsletter"))
        items = con.execute("SELECT * FROM newsletters ORDER BY id DESC").fetchall()
        counts = {l: con.execute("SELECT COUNT(*) FROM subscribers WHERE unsubscribed_at IS NULL AND lang=?", (l,)).fetchone()[0] for l in ("hr", "en")}
        return render_template("admin/newsletter.html", items=items, counts=counts)

    # --- izvoz
    @app.get("/admin/izvoz/<what>.<fmt>")
    @admin_required
    def admin_export(what, fmt):
        con = DB.get_db()
        if what == "popis-clanova":
            head = ["Broj", "Ime i prezime", "OIB", "Bez hrv. OIB-a", "Država", "Datum rođenja", "Kategorija članstva",
                    "Članarina", "Datum pristupanja", "Datum prestanka", "Razlog prestanka", "E-mail", "Telefon", "Treba dopuna"]
            rows = [[r["public_no"], r["full_name"], r["oib"] or "", "da" if r["no_oib"] else "", r["country"] or "", r["dob"],
                     "redovito" if r["member_type"] == "regular" else "pridruženo",
                     "snižena (studenti, umirovljenici)" if r["fee_cat"] == "student" else "standardna", r["admitted_date"], r["ended_date"] or "",
                     r["ended_reason"] or "", r["email"], r["phone"] or "", "da" if r["needs_completion"] else ""]
                    for r in con.execute("""SELECT a.*, m.admitted_date, m.ended_date, m.ended_reason FROM applications a
                                            JOIN memberships m ON m.application_id=a.id ORDER BY a.full_name COLLATE NOCASE""")]
        elif what == "clanarine":
            head = ["Broj", "Ime i prezime", "Razdoblje od", "Razdoblje do", "Iznos (€)", "Poziv na broj", "Rok", "Status", "Plaćeno"]
            rows = [[r["public_no"], r["full_name"], r["period_start"], r["period_end"], r["amount_cents"] / 100, r["reference"],
                     r["due_date"], {"open": "čeka uplatu", "paid": "plaćeno", "cancelled": "poništeno"}[r["status"]], r["paid_at"] or ""]
                    for r in con.execute("""SELECT d.*, a.public_no, a.full_name FROM dues d JOIN memberships m ON m.id=d.membership_id
                                            JOIN applications a ON a.id=m.application_id ORDER BY d.period_start""")]
        elif what == "uplate":
            head = ["Datum", "Iznos (€)", "Platitelj", "Poziv na broj", "Opis", "Vrsta", "Status", "Izvor", "Napomena"]
            rows = [[r["booked_date"], r["amount_cents"] / 100, r["payer"] or "", r["reference"] or "", r["description"] or "",
                     {"membership": "članarina", "donation": "donacija"}.get(r["kind"], "nepoznato"), r["status"], r["source"],
                     r["review_reason"] or ""] for r in con.execute("SELECT * FROM payments ORDER BY booked_date")]
        elif what == "prijave":
            head = ["Broj", "Zaprimljeno", "Ime i prezime", "E-mail", "OIB", "Bez hrv. OIB-a", "Država", "Datum rođenja", "Vrsta",
                    "Članarina", "Jezik", "Status"]
            rows = [[r["public_no"], r["created_at"], r["full_name"], r["email"], r["oib"] or "", "da" if r["no_oib"] else "",
                     r["country"] or "", r["dob"], r["member_type"], r["fee_cat"], r["lang"], r["app_status"]]
                    for r in con.execute("SELECT * FROM applications ORDER BY id")]
        else:
            abort(404)
        DB.audit(con, g.admin, "export", f"{what}.{fmt}")
        stamp = date.today().isoformat()
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(head)
            w.writerows(rows)
            return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename={what}-{stamp}.csv"})
        if fmt == "xlsx":
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = what[:30]
            ws.append(head)
            for r in rows:
                ws.append(r)
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(40, max(10, max(len(str(c.value or "")) for c in col) + 2))
            out = io.BytesIO()
            wb.save(out)
            out.seek(0)
            return send_file(out, as_attachment=True, download_name=f"{what}-{stamp}.xlsx",
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        abort(404)

    @app.get("/admin/izvoz")
    @admin_required
    def admin_export_page():
        backups = sorted(cfg["BACKUP_DIR"].glob("adv-*.sqlite3.gz"), reverse=True)[:10] if cfg["BACKUP_DIR"].exists() else []
        return render_template("admin/export.html", backups=backups)

    # ================================================================== naredbe (cron / systemd timer)
    @app.cli.command("init-db")
    def cli_init():
        DB.init_db(cfg["DB_PATH"])
        click.echo(f"Evidencija spremna: {cfg['DB_PATH']}")

    @app.cli.command("create-admin")
    @click.argument("username")
    def cli_admin(username):
        pw = click.prompt("Lozinka (najmanje 12 znakova)", hide_input=True, confirmation_prompt=True)
        if len(pw) < 12:
            raise click.ClickException("Lozinka mora imati najmanje 12 znakova.")
        con = DB.connect(cfg["DB_PATH"])
        con.execute("INSERT INTO admins(username, pw_hash, created_at) VALUES (?,?,?) ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash",
                    (username, generate_password_hash(pw), DB.now_iso()))
        click.echo(f"Administrator '{username}' je spremljen.")

    @app.cli.command("send-outbox")
    def cli_send():
        con = DB.connect(cfg["DB_PATH"])
        s, f = mailer.send_outbox(con, cfg, batch=int(os.environ.get("ADV_OUTBOX_BATCH", "40")))
        click.echo(f"poslano={s} neuspjelo={f}")

    @app.cli.command("daily")
    def cli_daily():
        """Dnevno: obnove i podsjetnici, sažetak za Upravni odbor."""
        con = DB.connect(cfg["DB_PATH"])
        n = S.run_renewals(con, cfg)
        since = (datetime.now() - timedelta(days=1)).astimezone().replace(microsecond=0).isoformat()
        text = S.digest(con, cfg, since)
        if text and cfg["DIGEST_TO"]:
            S.queue_email(con, "digest", cfg["DIGEST_TO"], "Sažetak: nove prijave i uplate – Academia de la Vida", text)
        s, f = mailer.send_outbox(con, cfg, batch=100)
        click.echo(f"obnove={n} sazetak={'da' if text else 'ne'} poslano={s} neuspjelo={f}")

    @app.cli.command("backup")
    def cli_backup():
        path = S.backup(cfg["DB_PATH"], cfg["BACKUP_DIR"], cfg["BACKUP_KEEP"])
        click.echo(f"Kopija: {path}")

    return app

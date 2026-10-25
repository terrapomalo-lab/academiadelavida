"""Slanje poruka iz reda (outbox). Poruka se nikad ne gubi: ako slanje zakaže, ostaje 'failed' i šalje se ponovno."""
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from db import now_iso
import payments as P
import services as S


def _attachments(con, cfg, spec):
    out = []
    for att in json.loads(spec or "[]"):
        if att["kind"].startswith("app_"):
            a = con.execute("SELECT * FROM applications WHERE id=?", (att.get("app_id"),)).fetchone()
            if not a:
                continue
            lang = a["lang"]
            pay = P.membership_payment(cfg, a, S.prepay_due(cfg, a), lang)
            if att["kind"] == "app_hub3_png":
                out.append((f"kod-za-placanje-{a['public_no']}.png", "image", "png", P.hub3_png(pay["hub3"], scale=4)))
            else:
                pdf = P.notice_pdf(cfg, pay, lang, member_name=a["full_name"], public_no=a["public_no"],
                                   period=("12 mjeseci od datuma prijema u članstvo" if lang == "hr" else "12 months from the date of admission"),
                                   due_date=None)
                name = ("obavijest-o-uplati-" if lang == "hr" else "payment-notice-") + a["public_no"] + ".pdf"
                out.append((name, "application", "pdf", pdf))
            continue
        due = con.execute("SELECT * FROM dues WHERE id=?", (att.get("due_id"),)).fetchone()
        if not due:
            continue
        a = con.execute("SELECT a.* FROM applications a JOIN memberships m ON m.application_id=a.id WHERE m.id=?",
                        (due["membership_id"],)).fetchone()
        lang = a["lang"]
        pay = P.membership_payment(cfg, a, due, lang)
        if att["kind"] == "hub3_png":
            out.append((f"kod-za-placanje-{a['public_no']}.png", "image", "png", P.hub3_png(pay["hub3"], scale=4)))
        elif att["kind"] == "notice_pdf":
            pdf = P.notice_pdf(cfg, pay, lang, member_name=a["full_name"], public_no=a["public_no"],
                               period=S.period_text(due["period_start"], due["period_end"], lang),
                               due_date=P.fmt_date(due["due_date"], lang))
            name = ("obavijest-o-uplati-" if lang == "hr" else "payment-notice-") + a["public_no"] + ".pdf"
            out.append((name, "application", "pdf", pdf))
    return out


def build_message(con, cfg, row):
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = row["to_addr"]
    msg["Subject"] = row["subject"]
    msg["Reply-To"] = cfg["MAIL_REPLY_TO"]
    msg["Message-ID"] = make_msgid(domain="academiadelavida.hr")
    msg.set_content(row["body_text"])
    for fname, maintype, subtype, data in _attachments(con, cfg, row["attachments"]):
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
    return msg


def send_outbox(con, cfg, ids=None, batch=40, include_failed=True, max_attempts=8):
    """Šalje poruke iz reda. Vraća (poslano, neuspjelo)."""
    statuses = ("queued", "failed") if include_failed else ("queued",)
    q = f"SELECT * FROM outbox WHERE status IN ({','.join('?'*len(statuses))}) AND attempts<?"
    params = list(statuses) + [max_attempts]
    if ids:
        q += f" AND id IN ({','.join('?'*len(ids))})"
        params += list(ids)
    q += " ORDER BY id LIMIT ?"
    params.append(batch)
    rows = con.execute(q, params).fetchall()
    if not rows:
        return 0, 0

    sent = failed = 0
    smtp = None
    try:
        if not cfg["MAIL_SUPPRESS_SEND"]:
            if not cfg["SMTP_HOST"]:
                raise RuntimeError("SMTP nije postavljen (ADV_SMTP_HOST).")
            smtp = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=20)
            if cfg["SMTP_STARTTLS"]:
                smtp.starttls(context=ssl.create_default_context())
            if cfg["SMTP_USER"]:
                smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"] or "")
    except Exception as e:  # cijela serija ostaje za ponovni pokušaj
        err = f"{type(e).__name__}: {e}"[:500]
        for r in rows:
            con.execute("UPDATE outbox SET status='failed', attempts=attempts+1, last_error=? WHERE id=?", (err, r["id"]))
        return 0, len(rows)

    preview_dir = cfg["DATA_DIR"] / "mail-preview"
    for r in rows:
        try:
            msg = build_message(con, cfg, r)
            if cfg["MAIL_SUPPRESS_SEND"]:
                preview_dir.mkdir(parents=True, exist_ok=True)
                (preview_dir / f"{r['id']:05d}-{r['kind']}.eml").write_bytes(bytes(msg))
            else:
                smtp.send_message(msg)
            con.execute("UPDATE outbox SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?",
                        (now_iso(), r["id"]))
            sent += 1
        except Exception as e:
            con.execute("UPDATE outbox SET status='failed', attempts=attempts+1, last_error=? WHERE id=?",
                        (f"{type(e).__name__}: {e}"[:500], r["id"]))
            failed += 1
    if smtp:
        try:
            smtp.quit()
        except Exception:
            pass
    return sent, failed

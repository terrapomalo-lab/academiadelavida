"""Jednokratno: isprika onima koji su u podsjetniku 28. 11. dobili rečenicu da uplata za knjigu nije zaprimljena."""
import sqlite3, sys
from datetime import datetime

send = sys.argv[1] == "send"
c = sqlite3.connect(sys.argv[2]); c.row_factory = sqlite3.Row
rows = c.execute("""SELECT * FROM event_regs WHERE book_qty>0 AND book_status='pending' AND status='confirmed'
                    AND reminded_at IS NOT NULL
                    AND email NOT IN (SELECT to_addr FROM outbox WHERE kind='event_apology')""").fetchall()
print(f"Primatelja isprike: {len(rows)}")
for r in rows:
    print("  -", r["full_name"], "<" + r["email"] + ">", r["reference"])
if not send:
    sys.exit(0)
subj = "Ispravak: uplata za knjigu s posvetom"
for r in rows:
    body = (f"Dobar dan, {r['full_name']},\n\n"
            "jutros ste od nas automatski dobili podsjetnik u kojem piše da uplatu za knjigu s posvetom još nismo zaprimili. "
            "Ispričavamo se zbog te poruke: poslana je prije nego što smo stigli obraditi uplate s bankovnog izvoda.\n\n"
            "Ako ste uplatu već izvršili, sve je u redu i ne trebate ništa poduzimati niti nam slati potvrdu o uplati. "
            "Vaša knjiga s posvetom čeka vas na predstavljanju u ponedjeljak, 30. 11. u 13:30, u Palači HAZU.\n\n"
            "Hvala vam na razumijevanju, radujemo se vašem dolasku."
            "\n\nSrdačan pozdrav,\nUpravni odbor udruge Academia de la Vida\nacademiadelavidaudruga@gmail.com · academiadelavida.hr")
    c.execute("INSERT INTO outbox(kind, to_addr, subject, body_text, attachments, status, created_at) VALUES (?,?,?,?,?,?,?)",
              ("event_apology", r["email"], subj, body, "[]", "queued", datetime.now().astimezone().isoformat(timespec="seconds")))
c.commit()
print("Isprike su stavljene u red za slanje.")

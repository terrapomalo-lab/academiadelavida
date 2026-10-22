"""SQLite evidencija: jedna datoteka, transakcije, strani ključevi uključeni."""
import sqlite3
from datetime import datetime, timezone
from flask import g, current_app

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS admins (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY,
  ip TEXT NOT NULL,
  at TEXT NOT NULL,
  ok INTEGER NOT NULL
);

-- Prijava (pristupnica). app_status: submitted | approved | rejected | withdrawn
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY,
  public_no TEXT UNIQUE NOT NULL,          -- interni broj, npr. ADV-2026-0007 (nije zakonski identifikator)
  year INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  token TEXT UNIQUE NOT NULL,              -- tajni ključ za stranicu s uplatom
  created_at TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT 'hr',
  full_name TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT,
  oib TEXT,
  no_oib INTEGER NOT NULL DEFAULT 0,
  country TEXT,
  dob TEXT NOT NULL,
  member_type TEXT NOT NULL CHECK (member_type IN ('regular','associate')),
  fee_cat TEXT NOT NULL CHECK (fee_cat IN ('standard','student')),
  interests TEXT,
  statute_accepted_at TEXT NOT NULL,
  privacy_ack_at TEXT NOT NULL,
  needs_completion INTEGER NOT NULL DEFAULT 0,  -- npr. nema OIB-a: dopuna prije konačnog upisa
  app_status TEXT NOT NULL DEFAULT 'submitted',
  decision_id INTEGER REFERENCES decisions(id),
  source TEXT NOT NULL DEFAULT 'web',      -- web | manual
  ip TEXT,
  admin_note TEXT,
  UNIQUE (year, seq)
);

-- Odluka Upravnog odbora (o prijemu ili odbijanju)
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('admit','reject')),
  decision_date TEXT NOT NULL,
  reference TEXT NOT NULL,                 -- npr. "Zapisnik 3. sjednice UO"
  note TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL
);

-- Članstvo. status: active | ended
CREATE TABLE IF NOT EXISTS memberships (
  id INTEGER PRIMARY KEY,
  application_id INTEGER UNIQUE NOT NULL REFERENCES applications(id),
  admitted_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  ended_date TEXT,
  ended_reason TEXT
);

-- Članarina po razdoblju. status: open | paid | cancelled
CREATE TABLE IF NOT EXISTS dues (
  id INTEGER PRIMARY KEY,
  membership_id INTEGER NOT NULL REFERENCES memberships(id),
  period_index INTEGER NOT NULL,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  reference TEXT UNIQUE NOT NULL,          -- poziv na broj (model HR00)
  due_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  paid_at TEXT,
  payment_id INTEGER REFERENCES payments(id),
  notice_sent_at TEXT,
  reminder_sent_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (membership_id, period_index)
);

-- Stvarno primljene uplate. kind: membership | donation | unknown ; status: matched | review | ignored
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,                    -- manual | import
  fingerprint TEXT UNIQUE NOT NULL,        -- sprječava dvostruko evidentiranje iste transakcije
  bank_txn_id TEXT,
  booked_date TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  payer TEXT,
  reference TEXT,
  description TEXT,
  kind TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'review',
  due_id INTEGER REFERENCES dues(id),
  review_reason TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL
);

-- Red poruka: sve e-poruke prolaze kroz ovu tablicu (ponovno slanje ako slanje zakaže)
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  to_addr TEXT NOT NULL,
  subject TEXT NOT NULL,
  body_text TEXT NOT NULL,
  attachments TEXT,                        -- JSON popis {kind, ref}
  related_app_id INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | sent | failed | held
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
);

-- Newsletter: zasebna, dobrovoljna prijava
CREATE TABLE IF NOT EXISTS subscribers (
  id INTEGER PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  name TEXT,
  lang TEXT NOT NULL DEFAULT 'hr',
  consent_at TEXT NOT NULL,
  source TEXT NOT NULL,
  unsub_token TEXT UNIQUE NOT NULL,
  unsubscribed_at TEXT
);

CREATE TABLE IF NOT EXISTS newsletters (
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  lang TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',    -- draft | sent
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  approved_by TEXT,
  sent_at TEXT,
  recipients INTEGER
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS ix_app_status ON applications(app_status);
CREATE INDEX IF NOT EXISTS ix_dues_status ON dues(status);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox(status);
"""


def now_iso():
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def connect(path):
    con = sqlite3.connect(str(path), timeout=15, isolation_level=None)  # autocommit; transakcije eksplicitno
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 15000")
    return con


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_e=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    con.executescript(SCHEMA)
    con.close()


def audit(con, actor, action, detail=""):
    con.execute("INSERT INTO audit(at, actor, action, detail) VALUES (?,?,?,?)", (now_iso(), actor, action, detail))

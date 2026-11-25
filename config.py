"""Postavke aplikacije. Tajne (lozinke, ključevi) dolaze isključivo iz varijabli okruženja
(datoteka .env na serveru, izvan javnog koda), nikad iz ovog repozitorija."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class Config:
    # --- Sigurnost ---
    SECRET_KEY = _env("ADV_SECRET_KEY")  # obavezno na serveru
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env("ADV_COOKIE_SECURE", "1") == "1"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8

    # --- Putanje ---
    DATA_DIR = Path(_env("ADV_DATA_DIR", str(BASE_DIR / "data")))
    DB_PATH = Path(_env("ADV_DB_PATH", str(DATA_DIR / "adv.sqlite3")))
    BACKUP_DIR = Path(_env("ADV_BACKUP_DIR", str(DATA_DIR / "backups")))
    BACKUP_KEEP = int(_env("ADV_BACKUP_KEEP", "30"))
    DOCS_DIR = BASE_DIR / "docs"

    # --- Javna adresa (za poveznice u e-porukama) ---
    BASE_URL = _env("ADV_BASE_URL", "https://academiadelavida.hr").rstrip("/")

    # --- Podaci udruge ---
    ORG_NAME_FULL = "Academia de la Vida – akademija umijeća življenja"
    ORG_NAME_FULL_EN = "Academia de la Vida – Academy of the Art of Living"
    ORG_NAME = "Academia de la Vida"
    ORG_STREET = "Hribarov prilaz 13"
    ORG_POSTCODE_CITY = "10000 Zagreb"
    ORG_COUNTRY = "Hrvatska"
    ORG_COUNTRY_EN = "Croatia"
    ORG_OIB = "37491305354"
    ORG_REG_NO = "21002990"
    ORG_EMAIL = "academiadelavidaudruga@gmail.com"
    ORG_IBAN = "HR1123600001101666226"
    ORG_BIC = _env("ADV_ORG_BIC", "ZABAHR2X")  # Zagrebačka banka (potvrđeno)

    # --- Članarina (prema odluci o članarini) ---
    # Odluka o članarini (nova): pridruženo 50 €, pridruženo – studenti i umirovljenici 20 €, redovito 150 €
    FEE_REGULAR_EUR = 150
    FEE_ASSOC_EUR = 50
    FEE_REDUCED_EUR = 20          # samo uz pridruženo članstvo
    # Prazno = sve prijave po ovoj odluci. (Datum se upisuje samo ako bi starije prijave trebale plaćati po staroj cijeni.)
    FEES_V2_FROM = _env("ADV_FEES_V2_FROM", "")
    # Knjiga na dar uz donaciju (35 € + 3 € dostava BOX NOW), vrijedi do uključivo navedenog datuma
    BOOK_OFFER_UNTIL = _env("ADV_BOOK_OFFER_UNTIL", "2026-12-31")
    BOOK_PRICE_CENTS = 3500       # knjiga
    BOOK_SHIP_CENTS = 300         # dostava BOX NOW po paketu
    BOOK_PER_PARCEL = 3           # do 3 knjige u jednom paketu (jedna dostava)
    BOOK_UNIT_CENTS = BOOK_PRICE_CENTS + BOOK_SHIP_CENTS   # najmanja donacija za 1 knjigu (38,00 €)
    BOOK_MAX_QTY = 5
    BOOK_COVER = "img/knjiga-naslovnica.jpg"
    # Predstavljanje knjige POMALO (prijave na /predstavljanje)
    EVENT = {
        "key": "pomalo-2026-11-30",
        "title": "POMALO – Mediteransko umijeće življenja",
        "date": "2026-11-30", "weekday": "ponedjeljak", "time": "13:30",
        "place": "Velika sjednička dvorana HAZU, Trg Nikole Šubića Zrinskog 11, Zagreb",
        "capacity": 200,             # prijave (sjedeća + stajaća mjesta)
        "notify_at": 120,            # obavijest udruzi kad broj prijavljenih osoba prijeđe ovaj broj
        "reg_until": "2026-11-28",     # zadnji dan prijava (produženo s 26. 11.)
        "book_until": "2026-11-28",    # rok za uplatu i predbilježbu knjige s posvetom (produženo s 25. 11.)
        "book_cents": int(_env("ADV_EVENT_BOOK_CENTS", "3500")),   # donacija po primjerku (35 €)
        "max_copies": 10,
        "remind_days_before": 2,
    }
    FEE_STANDARD_EUR = 50         # stara cijena, koristi se samo ako je FEES_V2_FROM postavljen
    FEE_STUDENT_EUR = 20

    # Društvene mreže: upisati punu adresu profila; prazno = ne prikazuje se
    SOCIAL_LINKS = [
        ("YouTube", _env("ADV_SOCIAL_YOUTUBE", "")),
        ("Facebook", _env("ADV_SOCIAL_FACEBOOK", "")),
        ("Instagram", _env("ADV_SOCIAL_INSTAGRAM", "")),
        ("LinkedIn", _env("ADV_SOCIAL_LINKEDIN", "")),
    ]
    PERIOD_MONTHS = 12
    # Rok za uplatu članarine: 15 dana (potvrđeno)
    PAYMENT_DUE_DAYS = int(_env("ADV_PAYMENT_DUE_DAYS", "15"))
    DECISION_REF_HR = _env("ADV_DECISION_REF_HR", "Odluka Upravnog odbora o visini i načinu plaćanja članarine od 22. listopada 2026.")
    DECISION_REF_EN = _env("ADV_DECISION_REF_EN", "Decision of the Management Board on the amount and payment of the membership fee of 22 October 2026")
    REMINDER_DAYS_BEFORE_END = int(_env("ADV_REMINDER_DAYS", "30"))

    # --- E-pošta (SMTP) ---
    SMTP_HOST = _env("ADV_SMTP_HOST")
    SMTP_PORT = int(_env("ADV_SMTP_PORT", "587"))
    SMTP_USER = _env("ADV_SMTP_USER")
    SMTP_PASSWORD = _env("ADV_SMTP_PASSWORD")
    SMTP_STARTTLS = _env("ADV_SMTP_STARTTLS", "1") == "1"
    MAIL_FROM = _env("ADV_MAIL_FROM", "Academia de la Vida <academiadelavidaudruga@gmail.com>")
    MAIL_REPLY_TO = _env("ADV_MAIL_REPLY_TO", "academiadelavidaudruga@gmail.com")
    DIGEST_TO = _env("ADV_DIGEST_TO", "academiadelavidaudruga@gmail.com")
    # Odmah obavijest udruzi za svaku novu prijavu i narudžbu knjige (prazno = isključeno)
    NOTIFY_TO = _env("ADV_NOTIFY_TO", "academiadelavidaudruga@gmail.com")
    MAIL_SUPPRESS_SEND = _env("ADV_MAIL_SUPPRESS", "0") == "1"  # za testiranje: poruke se ne šalju

    # --- Zaštita obrasca ---
    MAX_SUBMISSIONS_PER_IP_PER_HOUR = int(_env("ADV_RATE_LIMIT", "200"))  # visoko: na kongresu mnogi dijele istu Wi-Fi IP adresu

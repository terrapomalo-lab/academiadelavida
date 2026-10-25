"""Podaci za plaćanje: HUB3 PDF417 (hrvatske banke), EPC QR (SEPA, inozemne aplikacije),
PDF obavijest/uplatnica.

HUB3 prema HUB-ovoj „Uputi za 2D bar kod“ (EUR): PDF417, razina zaštite 4, 9 stupaca,
binarni način, UTF-8, zaglavlje HRVHUB30, polja odvojena znakom LF (0x0A).
"""
import io
import math
import re
import unicodedata
from datetime import date

import qrcode
from PIL import Image
from pdf417gen.compaction import BYTE_LATCH, BYTE_LATCH_ALT
from pdf417gen.compaction.byte import compact_bytes
from pdf417gen.encoding import encode_rows, get_padding, validate_barcode_size
from pdf417gen.error_correction import compute_error_correction_code_words
from pdf417gen.rendering import render_image

# ---------------------------------------------------------------- HUB3

HUB3_FIELD_MAX = {
    "payer_name": 30, "payer_street": 27, "payer_city": 27,
    "recipient_name": 25, "recipient_street": 25, "recipient_city": 27,
    "iban": 21, "model": 4, "reference": 22, "purpose": 4, "description": 35,
}

# Dopušteni znakovi prema uputi: znamenke, hrvatska abeceda, Q W X Y, razmak i , . : - + ? ' / ( )
_ALLOWED = set("0123456789ABCČĆDĐEFGHIJKLMNOPQRSŠTUVWXYZŽabcčćdđefghijklmnopqrsštuvwxyzž ,.:-+?'/()")
_KEEP = set("ČĆĐŠŽčćđšž")


def hub3_clean(text, max_len):
    """Prilagodi tekst dopuštenom skupu znakova i skrati na najveću duljinu (u znakovima)."""
    if not text:
        return ""
    out = []
    for ch in str(text):
        if ch in _ALLOWED:
            out.append(ch)
            continue
        if ch in "–—":
            out.append("-")
            continue
        if ch in _KEEP:
            out.append(ch)
            continue
        base = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in base if not unicodedata.combining(c))
        if base == "ß":
            base = "ss"
        for c in base:
            if c in _ALLOWED:
                out.append(c)
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    return s[:max_len]


def amount_field(amount_cents):
    if amount_cents < 0:
        raise ValueError("Iznos ne može biti negativan")
    return f"{int(amount_cents):015d}"


def hub3_payload(*, amount_cents, payer_name, recipient_name, recipient_street, recipient_city,
                 iban, model, reference, purpose, description, payer_street="", payer_city=""):
    m = HUB3_FIELD_MAX
    fields = [
        "HRVHUB30",
        "EUR",
        amount_field(amount_cents),
        hub3_clean(payer_name, m["payer_name"]),
        hub3_clean(payer_street, m["payer_street"]),
        hub3_clean(payer_city, m["payer_city"]),
        hub3_clean(recipient_name, m["recipient_name"]),
        hub3_clean(recipient_street, m["recipient_street"]),
        hub3_clean(recipient_city, m["recipient_city"]),
        iban.replace(" ", "")[: m["iban"]],
        model[: m["model"]],
        reference[: m["reference"]],
        purpose[: m["purpose"]],
        hub3_clean(description, m["description"]),
    ]
    # Uputa: separator slijedi iza posljednjeg znaka svakog polja.
    return "\n".join(fields) + "\n"


def _pdf417_binary(data_bytes, columns=9, security_level=4):
    """PDF417 s prisilnim binarnim (byte) načinom, kako traži uputa."""
    latch = BYTE_LATCH_ALT if len(data_bytes) % 6 == 0 else BYTE_LATCH
    data_words = [latch] + list(compact_bytes(list(data_bytes)))
    ec_count = 2 ** (security_level + 1)
    padding_words = get_padding(len(data_words), ec_count, columns)
    length_descriptor = len(data_words) + len(padding_words) + 1
    cw_count = len(data_words) + ec_count + len(padding_words) + 1
    validate_barcode_size(length_descriptor, math.ceil(cw_count / columns))
    words = [length_descriptor] + data_words + padding_words
    words = words + compute_error_correction_code_words(words, security_level)
    rows = [words[i:i + columns] for i in range(0, len(words), columns)]
    return list(encode_rows(rows, columns, security_level))


def hub3_png(payload, scale=3):
    codes = _pdf417_binary(payload.encode("utf-8"))
    img = render_image(codes, scale=scale, ratio=3, padding=scale * 4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- EPC QR (SEPA)

def epc_payload(*, amount_cents, recipient_name, iban, bic, remittance, purpose=""):
    """EPC069-12, verzija 002, UTF-8 (oznaka 1)."""
    eur = f"EUR{amount_cents // 100}.{amount_cents % 100:02d}" if amount_cents else ""
    lines = ["BCD", "002", "1", "SCT", bic or "", recipient_name[:70], iban.replace(" ", ""),
             eur, purpose[:4], "", remittance[:140]]
    return "\n".join(lines)


def epc_png(payload, box=8):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box, border=4)
    qr.add_data(payload.encode("utf-8"))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- modeli plaćanja

def membership_payment(cfg, app_row, due_row, lang):
    name = app_row["full_name"]
    desc = ("Članarina " if lang == "hr" else "Clanarina ") + app_row["public_no"]
    data = {
        "kind": "membership",
        "amount_cents": due_row["amount_cents"],
        "payer_name": name,
        "recipient_name": cfg["ORG_NAME"],
        "recipient_street": cfg["ORG_STREET"],
        "recipient_city": cfg["ORG_POSTCODE_CITY"],
        "iban": cfg["ORG_IBAN"],
        "bic": cfg["ORG_BIC"],
        "model": "HR00",
        "reference": due_row["reference"],
        "purpose": "OTHR",
        "description": "Članarina " + app_row["public_no"],
    }
    data["hub3"] = hub3_payload(**{k: data[k] for k in (
        "amount_cents", "payer_name", "recipient_name", "recipient_street", "recipient_city",
        "iban", "model", "reference", "purpose", "description")})
    data["epc"] = epc_payload(amount_cents=data["amount_cents"], recipient_name=cfg["ORG_NAME"],
                              iban=cfg["ORG_IBAN"], bic=cfg["ORG_BIC"],
                              remittance=f"HR00 {due_row['reference']} {desc}")
    return data


def donation_payment(cfg, amount_cents):
    data = {
        "kind": "donation",
        "amount_cents": amount_cents,
        "payer_name": "",
        "recipient_name": cfg["ORG_NAME"],
        "recipient_street": cfg["ORG_STREET"],
        "recipient_city": cfg["ORG_POSTCODE_CITY"],
        "iban": cfg["ORG_IBAN"],
        "bic": cfg["ORG_BIC"],
        "model": "HR99",
        "reference": "",
        "purpose": "CHAR",
        "description": "Donacija",
    }
    data["hub3"] = hub3_payload(**{k: data[k] for k in (
        "amount_cents", "payer_name", "recipient_name", "recipient_street", "recipient_city",
        "iban", "model", "reference", "purpose", "description")})
    data["epc"] = epc_payload(amount_cents=amount_cents, recipient_name=cfg["ORG_NAME"],
                              iban=cfg["ORG_IBAN"], bic=cfg["ORG_BIC"], remittance="Donacija", purpose="CHAR")
    return data


def membership_reference(app_row, period_index):
    """Poziv na broj, model HR00: GODINA-REDNI BROJ-RAZDOBLJE, npr. 2026-0007-1."""
    return f"{app_row['year']}-{app_row['seq']:04d}-{period_index}"


def fmt_eur(cents, lang="hr"):
    whole, frac = divmod(int(cents), 100)
    if lang == "hr":
        return f"{whole:,}".replace(",", ".") + f",{frac:02d} €"
    return f"€{whole:,}.{frac:02d}"


def fmt_date(iso, lang="hr"):
    d = date.fromisoformat(iso[:10])
    return d.strftime("%d. %m. %Y.") if lang == "hr" else d.strftime("%d %b %Y")


# ---------------------------------------------------------------- PDF obavijest

def notice_pdf(cfg, pay, lang, *, member_name=None, public_no=None, period=None, due_date=None):
    """Obavijest za uplatu (članarina) ili uplatnica za donaciju, s HUB3 i EPC kodom."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    import os
    fdir = os.environ.get("ADV_FONT_DIR", "/usr/share/fonts/truetype/dejavu")
    if "AdvSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("AdvSans", os.path.join(fdir, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("AdvSansB", os.path.join(fdir, "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFont(TTFont("AdvSerif", os.path.join(fdir, "DejaVuSerif.ttf")))

    hr = lang == "hr"
    navy = (0x17 / 255, 0x2C / 255, 0x46 / 255)
    gold = (0xB8 / 255, 0x95 / 255, 0x55 / 255)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    x0 = 20 * mm
    y = H - 22 * mm

    c.setFillColorRGB(*navy)
    c.setFont("AdvSerif", 20)
    c.drawString(x0, y, "ACADEMIA DE LA VIDA")
    y -= 6 * mm
    c.setFont("AdvSans", 9)
    c.drawString(x0, y, cfg["ORG_NAME_FULL"] if hr else cfg["ORG_NAME_FULL_EN"])
    y -= 4.5 * mm
    c.drawString(x0, y, f"{cfg['ORG_STREET']}, {cfg['ORG_POSTCODE_CITY']} · OIB {cfg['ORG_OIB']} · {cfg['ORG_EMAIL']}")
    y -= 4 * mm
    c.setStrokeColorRGB(*gold)
    c.setLineWidth(0.8)
    c.line(x0, y, W - x0, y)
    y -= 12 * mm

    c.setFont("AdvSansB", 15)
    if pay["kind"] == "membership":
        title = "Obavijest o uplati članarine" if hr else "Membership fee payment notice"
    else:
        title = "Podaci za donaciju" if hr else "Donation payment details"
    c.drawString(x0, y, title)
    y -= 10 * mm

    rows = []
    if pay["kind"] == "membership":
        rows += [
            ("Član" if hr else "Member", member_name),
            ("Broj prijave" if hr else "Application number", public_no),
            ("Razdoblje članarine" if hr else "Membership period", period),
            ("Rok za uplatu" if hr else "Payment due by", due_date),
        ]
    rows += [
        ("Iznos" if hr else "Amount", fmt_eur(pay["amount_cents"], lang) if pay["amount_cents"] else ("po vašem izboru" if hr else "your choice")),
        ("Primatelj" if hr else "Recipient", f"{cfg['ORG_NAME']}, {cfg['ORG_STREET']}, {cfg['ORG_POSTCODE_CITY']}"),
        ("IBAN", cfg["ORG_IBAN"]),
        ("BIC/SWIFT", cfg["ORG_BIC"]),
        ("Model i poziv na broj" if hr else "Model and reference", f"{pay['model']} {pay['reference']}".strip()),
        ("Opis plaćanja" if hr else "Payment description", pay["description"]),
    ]
    rows = [r for r in rows if r[1]]
    for label, value in rows:
        c.setFont("AdvSans", 9)
        c.setFillColorRGB(0.33, 0.38, 0.45)
        c.drawString(x0, y, label)
        c.setFont("AdvSansB", 11)
        c.setFillColorRGB(*navy)
        c.drawString(x0 + 52 * mm, y, str(value or ""))
        y -= 7.5 * mm

    if pay["kind"] == "membership":
        y -= 2 * mm
        c.setFont("AdvSans", 8.5)
        c.setFillColorRGB(0.33, 0.38, 0.45)
        ref = cfg["DECISION_REF_HR"] if hr else cfg["DECISION_REF_EN"]
        c.drawString(x0, y, ("Temelj: " if hr else "Basis: ") + ref)
        y -= 9 * mm

    # Kodovi
    hub = Image.open(io.BytesIO(hub3_png(pay["hub3"], scale=3)))
    hub_w = 58 * mm
    hub_h = hub_w * hub.height / hub.width
    c.setFont("AdvSansB", 10)
    c.setFillColorRGB(*navy)
    c.drawString(x0, y, "Hrvatske banke (HUB3)" if hr else "Croatian banks (HUB3)")
    c.drawString(x0 + 95 * mm, y, "Inozemne banke (EPC QR)" if hr else "Other EU banks (EPC QR)")
    y -= 3 * mm
    c.drawImage(ImageReader(hub), x0, y - hub_h, width=hub_w, height=hub_h)
    epc = Image.open(io.BytesIO(epc_png(pay["epc"], box=6)))
    q = 40 * mm
    c.drawImage(ImageReader(epc), x0 + 95 * mm, y - q, width=q, height=q)
    y -= max(hub_h, q) + 10 * mm

    c.setFont("AdvSans", 9)
    c.setFillColorRGB(0.2, 0.25, 0.32)
    if hr:
        lines = [
            "Plaćanje mobilnim bankarstvom: odaberite skeniranje (npr. „Slikaj i plati“) i usmjerite kameru na kod.",
            "Na istom mobitelu: učitajte spremljenu sliku koda iz galerije, ako aplikacija to podržava.",
            "Provjerite podatke prije potvrde plaćanja. Prikaz ili preuzimanje koda nije potvrda uplate.",
        ]
    else:
        lines = [
            "Mobile banking: choose the scan option in your banking app and point the camera at the code.",
            "On the same phone: load the saved code image from your gallery, if your app supports it.",
            "Check the details before confirming. Viewing or downloading the code is not proof of payment.",
        ]
    for ln in lines:
        c.drawString(x0, y, ln)
        y -= 5 * mm

    c.setFont("AdvSans", 7.5)
    c.setFillColorRGB(0.45, 0.5, 0.56)
    c.drawString(x0, 15 * mm, f"{cfg['ORG_NAME_FULL']} · OIB {cfg['ORG_OIB']} · Reg. br. {cfg['ORG_REG_NO']}")
    c.showPage()
    c.save()
    return buf.getvalue()

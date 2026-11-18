"""Pemindai email tagihan/langganan via IMAP.

Privasi: hanya email yang lolos filter kata kunci (subjek/pengirim) yang dikirim ke
model, isinya dipotong, dan folder dibuka read-only (BODY.PEEK, tidak menandai 'dibaca').
"""
from __future__ import annotations

import email
import email.message
import email.utils
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from email import policy
from email.header import decode_header, make_header
from typing import Callable

from .db import Database
from .formatting import rupiah
from .llm import Llm, LlmError, to_int

log = logging.getLogger(__name__)

BILLING_HINTS = re.compile(
    r"tagihan|invoice|receipt|kuitansi|bukti (?:pembayaran|bayar)|langganan|subscription|"
    r"renewal|perpanjang|jatuh tempo|pembayaran|payment|billing|\btrial\b|statement|"
    r"your (?:order|bill)|auto-?renew",
    re.I,
)
MAX_BODY_CHARS = 3000
MAX_LLM_CALLS_PER_SCAN = 40


@dataclass
class RawEmail:
    message_id: str
    sender: str
    subject: str
    sent_on: date
    body: str


def looks_like_billing(subject: str, sender: str) -> bool:
    return bool(BILLING_HINTS.search(subject) or BILLING_HINTS.search(sender))


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def message_text(msg: email.message.EmailMessage, limit: int = MAX_BODY_CHARS) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeDecodeError):
        return ""
    if part.get_content_type() == "text/html":
        text = _strip_html(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _header(value: object) -> str:
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:  # header rusak: jangan sampai menggagalkan seluruh scan
        return str(value or "")


HEADER_FIELDS = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)])"
HEADER_BATCH = 200


def _sent_on(msg: email.message.Message, fallback: date) -> date:
    try:
        return email.utils.parsedate_to_datetime(str(msg["Date"])).date()
    except (TypeError, ValueError):
        return fallback


def _message_id(msg: email.message.Message, sender: str, subject: str, sent: date) -> str:
    return str(msg["Message-ID"] or f"{sender}|{subject}|{sent}").strip()


def make_imap_fetcher(host: str, user: str, password: str, folder: str,
                      lookback_days: int, today: Callable[[], date] = date.today,
                      is_seen: Callable[[str], bool] | None = None,
                      ) -> Callable[[], list[RawEmail]]:
    """Fetcher yang mengambil email kandidat tagihan sejak `lookback_days` hari lalu.

    Header diambil per kelompok 200 (jauh lebih cepat daripada satu-satu), badan email
    hanya diunduh untuk kandidat yang lolos filter dan belum pernah diproses.
    """

    def fetch() -> list[RawEmail]:
        since = (today() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        out: list[RawEmail] = []
        with imaplib.IMAP4_SSL(host) as imap:
            imap.login(user, password)
            imap.select(folder, readonly=True)
            status, data = imap.search(None, "SINCE", since)
            if status != "OK" or not data or not data[0]:
                return out
            ids = data[0].split()
            candidates: list[bytes] = []
            for i in range(0, len(ids), HEADER_BATCH):
                st, head = imap.fetch(b",".join(ids[i : i + HEADER_BATCH]), HEADER_FIELDS)
                if st != "OK":
                    continue
                for item in head:
                    if not isinstance(item, tuple):
                        continue
                    hmsg = email.message_from_bytes(item[1], policy=policy.default)
                    subject, sender = _header(hmsg["Subject"]), _header(hmsg["From"])
                    if not looks_like_billing(subject, sender):
                        continue
                    sent = _sent_on(hmsg, today())
                    if is_seen and is_seen(_message_id(hmsg, sender, subject, sent)):
                        continue
                    candidates.append(item[0].split()[0])
            # terbaru dulu: kalau dibatasi jumlah panggilan AI, yang terbaru tetap terproses
            for num in reversed(candidates):
                st, full = imap.fetch(num, "(BODY.PEEK[])")
                if st != "OK" or not full or not isinstance(full[0], tuple):
                    continue
                msg = email.message_from_bytes(full[0][1], policy=policy.default)
                subject, sender = _header(msg["Subject"]), _header(msg["From"])
                sent = _sent_on(msg, today())
                out.append(RawEmail(_message_id(msg, sender, subject, sent), sender,
                                    subject, sent, message_text(msg)))
        return out

    return fetch


_GENERIC_NAMES = {"billing", "noreply", "no-reply", "info", "mail", "email", "support", "admin"}


def sender_name(sender: str) -> str:
    """Nama cadangan dari pengirim: 'Netflix <info@x.com>' -> 'Netflix', 'a@mailer.spotify.com' -> 'Spotify'."""
    display = re.sub(r"<.*?>", "", sender).strip(' "')
    if display and "@" not in display and display.lower() not in _GENERIC_NAMES:
        return display
    m = re.search(r"@([\w.-]+)", sender)
    if not m:
        return ""
    labels = [p for p in m.group(1).split(".") if p]
    core = labels[-2] if len(labels) >= 2 else labels[0]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "or", "go"}:
        core = labels[-3]  # *.co.id, *.com.au
    return core.capitalize()


def _valid_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def scan(db: Database, llm: Llm, chat_id: int, today: date,
         fetch: Callable[[], list[RawEmail]]) -> list[str]:
    """Pindai email, simpan tagihan/langganan baru. Mengembalikan pesan temuan untuk user."""
    findings: list[str] = []
    calls = 0
    for mail in fetch():
        if db.email_seen(mail.message_id):
            continue
        if calls >= MAX_LLM_CALLS_PER_SCAN:
            break  # sisanya diproses di scan berikutnya (belum ditandai 'seen')
        calls += 1
        try:
            data = llm.extract_billing_email(mail.sender, mail.subject, mail.body)
        except LlmError as exc:
            log.warning("Ekstraksi email gagal: %s", exc)
            continue
        db.mark_email(mail.message_id, chat_id)
        if not data or data.get("is_billing") is not True:
            continue

        name = (str(data.get("name") or "").strip() or sender_name(mail.sender))[:60]
        if not name:
            continue
        amount = to_int(data.get("amount"))
        due = _valid_date(data.get("due_date")) or mail.sent_on
        kind = "langganan" if data.get("kind") == "langganan" else "tagihan"
        cycle = "yearly" if data.get("cycle") == "yearly" else "monthly"
        due_month = due.month if cycle == "yearly" else None

        bill, old_amount, created = db.upsert_bill(
            chat_id, name, amount, due.day, kind, today, cycle, due_month, source="email"
        )
        findings.append(_describe(bill.name, bill.kind, amount, old_amount, created,
                                  bill.active, data.get("is_trial") is True))
    return [f for f in findings if f]


def _describe(name: str, kind: str, amount: int | None, old: int | None, created: bool,
              active: bool, trial: bool) -> str:
    price = f" ({rupiah(amount)})" if amount else ""
    if not active:
        return (f"🚨 {name} masih menagih{price}, padahal kamu tandai sudah dibatalkan. "
                f"Cek apakah langganannya benar-benar berhenti.")
    if created:
        extra = " ⏳ Ini masa percobaan — siap-siap batalkan sebelum ditagih." if trial else ""
        return f"📧 Terdeteksi dari email: {kind} {name}{price}.{extra}"
    if amount and old and amount != old:
        return f"💸 Harga {name} berubah: {rupiah(old)} → {rupiah(amount)}."
    return ""

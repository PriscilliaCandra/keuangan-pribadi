from datetime import date
from email.message import EmailMessage

from keuangan import email_scan
from keuangan.email_scan import RawEmail
from keuangan.llm import LlmError
from tests.conftest import CHAT, TODAY


class FakeLlm:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def extract_billing_email(self, sender, subject, body):
        self.calls.append((sender, subject, body))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def mail(mid="m1", subject="Tagihan Netflix", body="..."):
    return RawEmail(mid, "billing@netflix.com", subject, date(2026, 11, 10), body)


NETFLIX = {"is_billing": True, "name": "Netflix", "amount": 54000,
           "due_date": "2026-12-05", "kind": "langganan", "cycle": "monthly",
           "is_trial": False, "already_paid": True}


def test_looks_like_billing():
    assert email_scan.looks_like_billing("Invoice #123", "x@y.com")
    assert email_scan.looks_like_billing("Halo", "billing@toko.com")
    assert not email_scan.looks_like_billing("Diskon 50% hari ini!", "promo@toko.com")


def test_message_text_prefers_plain_and_truncates():
    msg = EmailMessage()
    msg.set_content("Total   tagihan\nRp 54.000")
    msg.add_alternative("<p>ignored</p>", subtype="html")
    assert email_scan.message_text(msg) == "Total tagihan Rp 54.000"
    assert len(email_scan.message_text(msg, limit=5)) == 5


def test_message_text_html_only_strips_tags_and_scripts():
    msg = EmailMessage()
    msg.set_content("<html><script>evil()</script><b>Bayar</b> <i>Rp 1</i></html>", subtype="html")
    assert email_scan.message_text(msg) == "Bayar Rp 1"


def test_scan_creates_subscription_and_dedups(db):
    llm = FakeLlm([NETFLIX])
    out = email_scan.scan(db, llm, CHAT, TODAY, lambda: [mail()])
    assert len(out) == 1 and "Netflix" in out[0] and "Rp54.000" in out[0]
    bill = db.find_bill(CHAT, "netflix")
    assert (bill.due_day, bill.kind, bill.source) == (5, "langganan", "email")
    # email yang sama tidak diproses ulang (tidak memanggil LLM lagi)
    assert email_scan.scan(db, llm, CHAT, TODAY, lambda: [mail()]) == []
    assert len(llm.calls) == 1


def test_scan_ignores_non_billing_but_marks_seen(db):
    llm = FakeLlm([{"is_billing": False}])
    assert email_scan.scan(db, llm, CHAT, TODAY, lambda: [mail()]) == []
    assert db.email_seen("m1") and db.list_bills(CHAT) == []


def test_scan_survives_llm_error_and_retries_later(db):
    llm = FakeLlm([LlmError("down")])
    assert email_scan.scan(db, llm, CHAT, TODAY, lambda: [mail()]) == []
    assert not db.email_seen("m1")  # belum ditandai -> dicoba lagi di scan berikutnya


def test_scan_rejects_garbage_model_output(db):
    llm = FakeLlm([{"is_billing": True, "name": "", "amount": -5},
                   {"is_billing": "true", "name": "X"}])
    nameless = RawEmail("a", "", "Tagihan", date(2026, 11, 10), "...")  # tanpa nama & tanpa pengirim
    out = email_scan.scan(db, llm, CHAT, TODAY, lambda: [nameless, mail("b")])
    assert out == [] and db.list_bills(CHAT) == []


def test_scan_flags_price_change_and_cancelled_still_billing(db):
    db.upsert_bill(CHAT, "Netflix", 54_000, 5, "langganan", TODAY)
    changed = dict(NETFLIX, amount=65_000)
    out = email_scan.scan(db, FakeLlm([changed]), CHAT, TODAY, lambda: [mail()])
    assert "Rp54.000 → Rp65.000" in out[0]

    bill = db.find_bill(CHAT, "netflix")
    db.set_bill_active(bill.id, False)
    out = email_scan.scan(db, FakeLlm([changed]), CHAT, TODAY, lambda: [mail("m2")])
    assert "masih menagih" in out[0]


def test_scan_trial_warning(db):
    trial = dict(NETFLIX, name="Disney", is_trial=True)
    out = email_scan.scan(db, FakeLlm([trial]), CHAT, TODAY, lambda: [mail()])
    assert "masa percobaan" in out[0]


def test_scan_caps_llm_calls(db):
    n = email_scan.MAX_LLM_CALLS_PER_SCAN
    mails = [mail(f"m{i}") for i in range(n + 5)]
    llm = FakeLlm([{"is_billing": False}] * (n + 5))
    email_scan.scan(db, llm, CHAT, TODAY, lambda: mails)
    assert len(llm.calls) == n


# ---------- fetcher IMAP (server palsu) ----------
class FakeImap:
    """Mensimulasikan IMAP4_SSL: 5 email, hanya #2 dan #4 yang tampak seperti tagihan."""
    MAILS = {
        b"1": ("Diskon besar", "promo@toko.com"),
        b"2": ("Tagihan listrik", "pln@pln.co.id"),
        b"3": ("Halo", "teman@x.com"),
        b"4": ("Invoice hosting", "billing@host.com"),
        b"5": ("Newsletter", "news@x.com"),
    }
    header_calls = 0
    full_calls: list = []

    def __init__(self, host): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): return "OK", []
    def select(self, folder, readonly=False): return "OK", [b"5"]
    def search(self, charset, *criteria): return "OK", [b" ".join(self.MAILS)]

    def fetch(self, ids, spec):
        nums = ids.split(b",")
        if "HEADER.FIELDS" in spec:
            type(self).header_calls += 1
            out = []
            for n in nums:
                subj, frm = self.MAILS[n]
                raw = (f"From: {frm}\r\nSubject: {subj}\r\nMessage-ID: <{n.decode()}@t>\r\n"
                       f"Date: Mon, 10 Nov 2026 08:00:00 +0700\r\n\r\n").encode()
                out += [(n + b" (BODY[HEADER.FIELDS (FROM SUBJECT)] {1}", raw), b")"]
            return "OK", out
        type(self).full_calls.append(nums[0])
        subj, frm = self.MAILS[nums[0]]
        raw = (f"From: {frm}\r\nSubject: {subj}\r\nMessage-ID: <{nums[0].decode()}@t>\r\n"
               f"Date: Mon, 10 Nov 2026 08:00:00 +0700\r\nContent-Type: text/plain\r\n\r\n"
               f"isi {subj}").encode()
        return "OK", [(nums[0] + b" (RFC822 {1}", raw), b")"]


def test_imap_fetcher_batches_headers_and_downloads_only_candidates(monkeypatch):
    FakeImap.header_calls, FakeImap.full_calls = 0, []
    monkeypatch.setattr(email_scan.imaplib, "IMAP4_SSL", FakeImap)
    fetch = email_scan.make_imap_fetcher("h", "u", "p", "INBOX", 45, lambda: TODAY)
    mails = fetch()
    assert FakeImap.header_calls == 1  # semua header dalam satu perintah
    assert FakeImap.full_calls == [b"4", b"2"]  # hanya kandidat, terbaru dulu
    assert [m.subject for m in mails] == ["Invoice hosting", "Tagihan listrik"]
    assert mails[0].message_id == "<4@t>" and mails[0].sent_on == date(2026, 11, 10)
    assert "isi Invoice hosting" in mails[0].body


def test_imap_fetcher_skips_already_processed(monkeypatch):
    FakeImap.header_calls, FakeImap.full_calls = 0, []
    monkeypatch.setattr(email_scan.imaplib, "IMAP4_SSL", FakeImap)
    fetch = email_scan.make_imap_fetcher("h", "u", "p", "INBOX", 45, lambda: TODAY,
                                         is_seen=lambda mid: mid == "<4@t>")
    assert [m.subject for m in fetch()] == ["Tagihan listrik"]
    assert FakeImap.full_calls == [b"2"]


import pytest


@pytest.mark.parametrize("sender,expected", [
    ("Netflix <info@mailer.netflix.com>", "Netflix"),
    ("billing@mailer.spotify.com", "Spotify"),
    ('"PLN Mobile" <noreply@pln.co.id>', "PLN Mobile"),
    ("noreply@pln.co.id", "Pln"),
    ("Billing <billing@hosting.id>", "Hosting"),
    ("", ""),
])
def test_sender_name(sender, expected):
    assert email_scan.sender_name(sender) == expected


def test_scan_falls_back_to_sender_name_when_model_gives_none(db):
    data = dict(NETFLIX, name=None)
    out = email_scan.scan(db, FakeLlm([data]), CHAT, TODAY, lambda: [
        RawEmail("x1", "Netflix <info@mailer.netflix.com>", "Tagihan", date(2026, 11, 10), "...")])
    assert "Netflix" in out[0] and db.find_bill(CHAT, "netflix") is not None

import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from keuangan import agent
from keuangan.llm import AnthropicLlm, LlmError, OllamaLlm
from keuangan.service import Service
from tests.conftest import CHAT, TODAY


@pytest.fixture
def seeded(db):
    d = lambda n: TODAY - timedelta(days=n)  # noqa: E731
    db.add_expense(CHAT, 25_000, "kopi kenangan", "Makanan & Minuman", d(0))
    db.add_expense(CHAT, 30_000, "kopi susu", "Makanan & Minuman", d(3))
    db.add_expense(CHAT, 500_000, "sepatu", "Belanja", d(5))
    db.add_expense(CHAT, 90_000, "gojek", "Transportasi", d(20))  # bulan lalu
    db.add_expense(CHAT, 40_000, "kopi lagi", "Makanan & Minuman", d(40))  # dua bulan lalu
    db.add_expense(2, 999_000, "milik orang lain", "Belanja", d(0))
    db.add_income(CHAT, 8_000_000, "gaji", d(10))
    db.set_budget(CHAT, "Makanan & Minuman", 100_000)
    db.upsert_bill(CHAT, "Netflix", 54_000, 20, "langganan", TODAY)
    return agent.QueryTools(db, CHAT, TODAY)


# ---------- periode ----------
@pytest.mark.parametrize("periode,start,end", [
    ("hari_ini", date(2026, 11, 17), date(2026, 11, 18)),
    ("kemarin", date(2026, 11, 16), date(2026, 11, 17)),
    ("minggu_ini", date(2026, 11, 16), date(2026, 11, 18)),  # Senin 16 Nov
    ("7_hari", date(2026, 11, 11), date(2026, 11, 18)),
    ("bulan_ini", date(2026, 11, 1), date(2026, 12, 1)),
    ("bulan_lalu", date(2026, 10, 1), date(2026, 11, 1)),
    ("3_bulan", date(2026, 9, 1), date(2026, 12, 1)),
    ("tahun_ini", date(2026, 1, 1), date(2026, 11, 18)),
])
def test_resolve_period(periode, start, end):
    assert agent.resolve_period(periode, TODAY) == (start, end)


def test_resolve_period_january_edges():
    jan = date(2027, 1, 10)
    assert agent.resolve_period("bulan_lalu", jan) == (date(2026, 12, 1), date(2027, 1, 1))
    assert agent.resolve_period("3_bulan", jan) == (date(2026, 11, 1), date(2027, 2, 1))


# ---------- cari_transaksi ----------
def test_total_this_month_excludes_other_chats(seeded):
    r = seeded.run("cari_transaksi", {})
    assert r["total"] == 555_000 and r["total_rp"] == "Rp555.000" and r["jumlah_transaksi"] == 3


def test_keyword_and_category_filters(seeded):
    r = seeded.run("cari_transaksi", {"kata_kunci": "KOPI", "periode": "semua"})
    assert r["total"] == 95_000 and r["jumlah_transaksi"] == 3
    r = seeded.run("cari_transaksi", {"kategori": "belanja"})
    assert r["total"] == 500_000


def test_category_name_in_keyword_is_treated_as_category(seeded):
    # model kecil sering menulis kata_kunci="transportasi" alih-alih kategori
    r = seeded.run("cari_transaksi", {"kata_kunci": "transportasi", "periode": "semua"})
    assert r["total"] == 90_000 and r["jumlah_transaksi"] == 1
    r = seeded.run("cari_transaksi", {"kata_kunci": "Makanan", "periode": "bulan_ini"})
    assert r["total"] == 55_000
    # kata biasa tetap pencarian teks
    assert seeded.run("cari_transaksi", {"kata_kunci": "sepatu", "periode": "semua"})["total"] == 500_000
    # tebakan awalan tidak dipakai ("kop" bukan kategori)
    assert seeded.run("cari_transaksi", {"kata_kunci": "kop", "periode": "semua"})["jumlah_transaksi"] == 3


def test_grouping_by_category_month_and_day(seeded):
    r = seeded.run("cari_transaksi", {"periode": "semua", "kelompok": "kategori"})
    assert [g["kunci"] for g in r["kelompok"]] == ["Belanja", "Makanan & Minuman", "Transportasi"]
    r = seeded.run("cari_transaksi", {"periode": "semua", "kelompok": "bulan"})
    assert [g["kunci"] for g in r["kelompok"]] == ["2026-10", "2026-11"]
    assert sum(g["total"] for g in r["kelompok"]) == r["total"]
    r = seeded.run("cari_transaksi", {"kelompok": "hari"})
    assert len(r["kelompok"]) == 3


def test_details_biggest_and_latest_with_limit(seeded):
    big = seeded.run("cari_transaksi", {"periode": "semua", "batas": 2, "urut": "terbesar"})
    assert [x["catatan"] for x in big["rincian"]] == ["sepatu", "gojek"]
    new = seeded.run("cari_transaksi", {"periode": "semua", "batas": 2, "urut": "terbaru"})
    assert [x["catatan"] for x in new["rincian"]] == ["kopi kenangan", "kopi susu"]
    assert seeded.run("cari_transaksi", {"batas": 999, "periode": "semua"})["rincian"].__len__() == 5
    assert "rincian" not in seeded.run("cari_transaksi", {})


def test_income_query(seeded):
    r = seeded.run("cari_transaksi", {"jenis": "pemasukan", "periode": "bulan_ini"})
    assert r["total"] == 8_000_000 and r["jumlah_transaksi"] == 1


def test_explicit_dates_override_period(seeded):
    r = seeded.run("cari_transaksi", {"mulai": "2026-11-10", "sampai": "2026-11-17", "periode": "semua"})
    assert r["jumlah_transaksi"] == 3  # kopi hari ini, kopi susu (3 hari lalu), sepatu (5 hari lalu)
    assert r["dari"] == "2026-11-10" and r["sampai"] == "2026-11-17"


def test_empty_result(seeded):
    r = seeded.run("cari_transaksi", {"kata_kunci": "tidak-ada"})
    assert r["total"] == 0 and r["jumlah_transaksi"] == 0 and "rata_rata_per_transaksi_rp" not in r


@pytest.mark.parametrize("args", [
    {"periode": "kapan-kapan"}, {"jenis": "hutang"}, {"kelompok": "warna"}, {"urut": "acak"},
    {"mulai": "kemarin"}, {"mulai": "2026-11-10", "sampai": "2026-11-01"},
    {"kategori": "ngawur"}, {"jenis": "pemasukan", "kategori": "belanja"},
])
def test_bad_arguments_return_error_not_exception(seeded, args):
    assert "error" in seeded.run("cari_transaksi", args)


def test_unknown_argument_names_are_rejected_not_ignored(seeded):
    r = seeded.run("cari_transaksi", {"kateg": "makanan"})  # salah ketik dari 'kategori'
    assert "error" in r and "kateg" in r["error"] and "kategori" in r["error"]
    assert "error" in seeded.run("arus_kas", {"periode": "bulan_ini", "x": 1})
    assert "error" in seeded.run("status_anggaran", {"apa_saja": 1})


def test_unknown_tool_and_non_dict_args(seeded):
    assert "error" in seeded.run("hapus_semua", {})
    assert seeded.run("status_anggaran", "bukan dict")["anggaran"]  # args non-dict diabaikan
    assert "error" in seeded.run("cari_transaksi", {"periode": 5})


def test_row_limit_clamped(db):
    for i in range(50):
        db.add_expense(CHAT, 1000, f"x{i}", "Belanja", TODAY)
    r = agent.QueryTools(db, CHAT, TODAY).run("cari_transaksi", {"batas": 1000})
    assert len(r["rincian"]) == agent.MAX_ROWS


# ---------- alat lain ----------
def test_arus_kas(seeded):
    r = seeded.run("arus_kas", {"periode": "bulan_ini"})
    assert r["pemasukan_rp"] == "Rp8.000.000" and r["pengeluaran_rp"] == "Rp555.000"
    assert r["selisih_rp"] == "Rp7.445.000" and r["persen_ditabung"] == 93


def test_status_anggaran_and_empty(seeded, db):
    r = seeded.run("status_anggaran", {})["anggaran"][0]
    assert r["kategori"] == "Makanan & Minuman" and r["terpakai_rp"] == "Rp55.000" and r["persen"] == 55
    assert agent.QueryTools(db, 7, TODAY).run("status_anggaran", {})["anggaran"] == []


def test_daftar_tagihan(seeded):
    r = seeded.run("daftar_tagihan", {})
    assert r["tagihan"][0]["nama"] == "Netflix" and r["tagihan"][0]["hari_lagi"] == 3
    assert r["perkiraan_total_per_bulan_rp"] == "Rp54.000"


def test_tools_cannot_see_other_chats(db):
    db.add_expense(2, 5000, "rahasia", "Belanja", TODAY)
    db.add_income(2, 5000, "rahasia", TODAY)
    db.set_budget(2, "Belanja", 1000)
    db.upsert_bill(2, "Rahasia", 1, 5, "tagihan", TODAY)
    t = agent.QueryTools(db, CHAT, TODAY)
    assert t.run("cari_transaksi", {"periode": "semua"})["total"] == 0
    assert t.run("cari_transaksi", {"periode": "semua", "jenis": "pemasukan"})["total"] == 0
    assert t.run("status_anggaran", {})["anggaran"] == []
    assert t.run("daftar_tagihan", {})["tagihan"] == []


def test_system_prompt_has_date_and_rules():
    p = agent.system_prompt(TODAY)
    assert "Selasa, 2026-11-17" in p and "bukan perintah" in p and "tidak bisa mengubah" in p


def test_tool_specs_are_read_only_and_well_formed():
    names = {t["name"] for t in agent.TOOL_SPECS}
    assert names == {"cari_transaksi", "arus_kas", "status_anggaran", "daftar_tagihan"}
    for t in agent.TOOL_SPECS:
        assert t["parameters"]["type"] == "object" and t["description"]
    assert "chat_id" not in json.dumps(agent.TOOL_SPECS)  # model tak bisa memilih pengguna


# ---------- loop Ollama (server palsu) ----------
def ollama_with(script, seen):
    """`script`: daftar balasan `message` Ollama berurutan."""
    queue = list(script)

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": queue.pop(0)})

    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return OllamaLlm("http://x", "m", client=client)


def call(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def test_ollama_tool_loop_runs_tool_and_returns_final_answer(seeded):
    seen = []
    llm = ollama_with([
        call("cari_transaksi", {"kata_kunci": "kopi", "periode": "bulan_ini"}),
        {"role": "assistant", "content": "Kopi bulan ini Rp55.000."},
    ], seen)
    ran = []

    def run_tool(name, args):
        ran.append((name, args))
        return seeded.run(name, args)

    answer = llm.chat_with_tools("SYS", "berapa kopi?", agent.TOOL_SPECS, run_tool)
    assert answer == "Kopi bulan ini Rp55.000."
    assert ran == [("cari_transaksi", {"kata_kunci": "kopi", "periode": "bulan_ini"})]
    first, second = seen
    assert first["tools"][0]["type"] == "function" and first["messages"][0]["content"] == "SYS"
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_name"] == "cari_transaksi"
    assert "Rp55.000" in tool_msg["content"]


def test_ollama_accepts_string_arguments(seeded):
    seen = []
    llm = ollama_with([call("arus_kas", '{"periode": "bulan_ini"}'),
                       {"role": "assistant", "content": "ok"}], seen)
    got = []
    llm.chat_with_tools("S", "q", agent.TOOL_SPECS, lambda n, a: (got.append(a), {})[1])
    assert got == [{"periode": "bulan_ini"}]


def test_ollama_garbage_arguments_become_empty_dict():
    seen = []
    llm = ollama_with([call("arus_kas", "{bukan json"), {"role": "assistant", "content": "ok"}], seen)
    got = []
    llm.chat_with_tools("S", "q", agent.TOOL_SPECS, lambda n, a: (got.append(a), {})[1])
    assert got == [{}]


def test_ollama_step_limit_and_empty_answer():
    forever = [call("arus_kas", {})] * 10
    with pytest.raises(LlmError, match="Terlalu banyak langkah"):
        ollama_with(forever, []).chat_with_tools("S", "q", agent.TOOL_SPECS, lambda n, a: {}, max_steps=3)
    with pytest.raises(LlmError, match="tidak memberi jawaban"):
        ollama_with([{"role": "assistant", "content": "  "}], []).chat_with_tools(
            "S", "q", agent.TOOL_SPECS, lambda n, a: {})


def test_oversized_tool_result_is_replaced_by_error():
    seen = []
    llm = ollama_with([call("cari_transaksi", {}), {"role": "assistant", "content": "ok"}], seen)
    llm.chat_with_tools("S", "q", agent.TOOL_SPECS, lambda n, a: {"x": "a" * 10_000})
    assert "terlalu besar" in seen[1]["messages"][-1]["content"]


# ---------- loop Claude (klien palsu) ----------
class FakeClaude:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def tool_use(name, args, tid="t1"):
    block = SimpleNamespace(type="tool_use", id=tid, name=name, input=args)
    return SimpleNamespace(stop_reason="tool_use", content=[block])


def final(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def test_claude_tool_loop(seeded):
    fake = FakeClaude([tool_use("status_anggaran", {}), final("Makanan baru 55% terpakai.")])
    llm = AnthropicLlm("k", "m", client=fake)
    answer = llm.chat_with_tools("SYS", "anggaranku?", agent.TOOL_SPECS, seeded.run)
    assert answer == "Makanan baru 55% terpakai."
    first, second = fake.calls
    assert first["tools"][0]["input_schema"]["type"] == "object" and first["system"] == "SYS"
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert "Rp55.000" in result["content"]
    assert second["messages"][-2]["role"] == "assistant"  # blok asisten dikembalikan apa adanya


def test_claude_refusal_and_step_limit():
    refusal = SimpleNamespace(stop_reason="refusal", content=[])
    with pytest.raises(LlmError, match="ditolak"):
        AnthropicLlm("k", "m", client=FakeClaude([refusal])).chat_with_tools(
            "S", "q", agent.TOOL_SPECS, lambda n, a: {})
    loop = FakeClaude([tool_use("arus_kas", {})] * 10)
    with pytest.raises(LlmError, match="Terlalu banyak langkah"):
        AnthropicLlm("k", "m", client=loop).chat_with_tools(
            "S", "q", agent.TOOL_SPECS, lambda n, a: {}, max_steps=2)


# ---------- layanan ----------
class FakeAgentLlm:
    def __init__(self, answer="Jawabannya Rp1.", error=False):
        self.answer, self.error, self.asked = answer, error, []

    def chat_with_tools(self, system, question, tools, run_tool, max_steps=6):
        self.asked.append(question)
        if self.error:
            raise LlmError("x")
        assert run_tool("arus_kas", {})["pemasukan_rp"]  # alat benar-benar terpasang
        return self.answer

    def categorize(self, description):
        return None


def test_service_routes_questions_to_agent(db):
    llm = FakeAgentLlm()
    s = Service(db, llm, clock=lambda: TODAY)
    assert s.handle_text(CHAT, "berapa pengeluaran kopi bulan ini?") == "Jawabannya Rp1."
    assert s.ask_command(CHAT, "bulan apa paling boros") == "Jawabannya Rp1."
    assert len(llm.asked) == 2
    assert db.sum_between(CHAT, date(2026, 1, 1), date(2027, 1, 1)) == 0  # tak ada yang tercatat


def test_expense_with_question_mark_is_still_recorded(db):
    llm = FakeAgentLlm()
    s = Service(db, llm, clock=lambda: TODAY)
    assert "Dicatat" in s.handle_text(CHAT, "makan siang 35rb?")
    assert llm.asked == []


def test_questions_without_ai_fall_back_gracefully(db):
    s = Service(db, None, clock=lambda: TODAY)
    assert "nominalnya" in s.handle_text(CHAT, "berapa pengeluaran kopi?")
    assert "butuh AI" in s.ask_command(CHAT, "halo")
    assert "Contoh" in s.ask_command(CHAT, "  ")


def test_ask_failure_is_friendly_and_question_is_capped(db):
    s = Service(db, FakeAgentLlm(error=True), clock=lambda: TODAY)
    assert "belum bisa menjawab" in s.ask_command(CHAT, "berapa?")
    llm = FakeAgentLlm()
    Service(db, llm, clock=lambda: TODAY).ask_command(CHAT, "x" * 5000)
    assert len(llm.asked[0]) == 500

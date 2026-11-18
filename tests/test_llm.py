import json
from types import SimpleNamespace

import httpx
import pytest

from keuangan.llm import AnthropicLlm, LlmError, OllamaLlm, make_llm, to_int


@pytest.mark.parametrize("value,expected", [
    (85000, 85000), (85000.0, 85000), ("85000", 85000), ("85.000", 85000),
    ("Rp 85.000", 85000), ("1,250,000", 1250000),
    (0, None), (-5, None), (True, None), (None, None), ("abc", None), ("85.5", None), ("", None),
])
def test_to_int(value, expected):
    assert to_int(value) == expected


def ollama(handler, **kw):
    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return OllamaLlm("http://x", "teks-model", client=client, **kw)


def reply(content):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_ollama_text_request_shape_and_parse():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply('{"category": "Transportasi"}')

    assert ollama(handler).categorize("gojek ke kantor") == "Transportasi"
    assert seen["model"] == "teks-model" and seen["stream"] is False and seen["format"] == "json"
    assert seen["messages"][0]["role"] == "system" and "images" not in seen["messages"][1]


def test_ollama_receipt_uses_vision_model_and_images():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply('{"is_receipt": true, "total": 85000}')

    data = ollama(handler, vision_model="vis-model").read_receipt(b"\x89PNG")
    assert data["total"] == 85000
    assert seen["model"] == "vis-model" and len(seen["messages"][1]["images"]) == 1


def test_ollama_vision_defaults_to_main_model():
    seen = {}
    ollama(lambda r: (seen.update(json.loads(r.content)), reply("{}"))[1]).read_receipt(b"x")
    assert seen["model"] == "teks-model"


def test_ollama_missing_model_gives_pull_hint():
    with pytest.raises(LlmError, match="ollama pull teks-model"):
        ollama(lambda r: httpx.Response(404, json={"error": "not found"})).categorize("x")


def test_ollama_server_down():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LlmError, match="tidak bisa dihubungi"):
        ollama(handler).categorize("x")


def test_ollama_server_error_and_bad_body():
    with pytest.raises(LlmError, match="500"):
        ollama(lambda r: httpx.Response(500, text="boom")).categorize("x")
    with pytest.raises(LlmError, match="bukan JSON"):
        ollama(lambda r: httpx.Response(200, text="<html>")).categorize("x")


def test_ollama_non_json_answer_returns_none():
    assert ollama(lambda r: reply("maaf saya tidak tahu")).categorize("x") is None


def test_categorize_rejects_unknown_category():
    assert ollama(lambda r: reply('{"category": "Hack"}')).categorize("x") is None


class FakeAnthropic:
    def __init__(self, text, stop="end_turn"):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._text, self._stop = text, stop

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self._stop,
                               content=[SimpleNamespace(type="text", text=self._text)])


def test_anthropic_builds_image_content_and_parses():
    fake = FakeAnthropic('ok {"is_receipt": true, "total": 1000}')
    data = AnthropicLlm("k", "m", client=fake).read_receipt(b"img", "image/png")
    assert data["total"] == 1000
    content = fake.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"


def test_anthropic_refusal():
    with pytest.raises(LlmError, match="ditolak"):
        AnthropicLlm("k", "m", client=FakeAnthropic("", "refusal")).categorize("x")


def test_make_llm_selects_provider():
    from keuangan.config import load_config
    base = {"TELEGRAM_BOT_TOKEN": "t"}
    assert make_llm(load_config(base)) is None
    assert isinstance(make_llm(load_config({**base, "OLLAMA_MODEL": "qwen2.5:7b"})), OllamaLlm)
    assert isinstance(make_llm(load_config({**base, "ANTHROPIC_API_KEY": "k"})), AnthropicLlm)


def test_ollama_email_extraction_sends_json_schema_with_enums():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply('{"is_billing": false, "kind": "tagihan", "cycle": "monthly"}')

    data = ollama(handler).extract_billing_email("a@b.com", "Halo", "isi")
    assert data["is_billing"] is False
    schema = seen["format"]
    assert isinstance(schema, dict)
    assert schema["properties"]["kind"]["enum"] == ["langganan", "tagihan"]
    assert schema["properties"]["cycle"]["enum"] == ["monthly", "yearly"]


def test_ollama_other_tasks_keep_plain_json_format():
    seen = {}
    ollama(lambda r: (seen.update(json.loads(r.content)), reply("{}"))[1]).categorize("x")
    assert seen["format"] == "json"

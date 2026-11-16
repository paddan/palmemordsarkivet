"""Tester för bildmotorn ``llm_vision`` (falsk klient, inga nätanrop)."""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import llm_vision  # noqa: E402
from llm_vision import VisionError, VisionProfile  # noqa: E402


def _response(content: str, finish_reason: str = "stop", usage=(120, 45)):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


class _StatusError(Exception):
    """Som ``openai.APIStatusError``: bär ``status_code`` och ``response.headers``."""

    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class _FakeClient:
    """Har ``chat.completions.create``; svaren tas i tur och ordning."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _profile(name: str = "Syn") -> VisionProfile:
    return VisionProfile(name=name, model="vision-model", base_url="https://example.org/v1",
                         api_key="hemlig", external=True)


def _image_url(call: dict) -> str:
    parts = call["messages"][-1]["content"]
    return next(p["image_url"]["url"] for p in parts if p.get("type") == "image_url")


def test_transcribe_sends_png_and_returns_markdown() -> None:
    client = _FakeClient([_response("## Förhör\n\nHan gick hem.")])
    image = Image.new("RGB", (3000, 1500), "white")

    result = llm_vision.transcribe(image, _profile(), client=client)

    assert result.markdown == "## Förhör\n\nHan gick hem."
    assert (result.input_tokens, result.output_tokens) == (120, 45)
    call = client.calls[0]
    assert call["model"] == "vision-model"
    url = _image_url(call)
    assert url.startswith("data:image/png;base64,")
    sent = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(sent.size) <= 2000  # längsta sidan nedskalad
    assert sent.size == (2000, 1000)


def test_transcribe_strips_code_fence() -> None:
    client = _FakeClient([_response("```markdown\n# Rubrik\ntext\n```")])

    result = llm_vision.transcribe(Image.new("L", (100, 100), 255), _profile(), client=client)

    assert result.markdown == "# Rubrik\ntext"


def test_transcribe_length_cut_is_an_error() -> None:
    client = _FakeClient([_response("## Halv", finish_reason="length")])

    with pytest.raises(VisionError) as info:
        llm_vision.transcribe(Image.new("RGB", (50, 50), "white"), _profile(), client=client)

    assert info.value.fatal is False
    assert "length" in str(info.value)


def test_transcribe_429_retries_then_fatal(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(llm_vision.time, "sleep", sleeps.append)
    client = _FakeClient([_StatusError(429) for _ in range(5)])

    with pytest.raises(VisionError) as info:
        llm_vision.transcribe(Image.new("RGB", (50, 50), "white"), _profile(), client=client)

    assert info.value.fatal is True
    assert len(client.calls) == 5
    assert sleeps == [2, 4, 8, 16]
    assert "5 försök" in str(info.value)


def test_transcribe_429_recovers_before_last_attempt(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(llm_vision.time, "sleep", sleeps.append)
    client = _FakeClient([_StatusError(429)] * 4 + [_response("ok")])

    result = llm_vision.transcribe(Image.new("RGB", (50, 50), "white"), _profile(), client=client)

    assert result.markdown == "ok"
    assert sleeps == [2, 4, 8, 16]


def test_transcribe_honours_numeric_retry_after_capped(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(llm_vision.time, "sleep", sleeps.append)
    client = _FakeClient([
        _StatusError(429, {"retry-after": "7"}),
        _StatusError(429, {"retry-after": "3600"}),
        _StatusError(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _response("ok"),
    ])

    result = llm_vision.transcribe(Image.new("RGB", (50, 50), "white"), _profile(), client=client)

    assert result.markdown == "ok"
    # 7 s enligt servern, 3600 kapat till 60, ett datum ger tabellens 8 s
    assert sleeps == [7.0, 60.0, 8]


def test_transcribe_auth_error_is_fatal_without_retry(monkeypatch) -> None:
    monkeypatch.setattr(llm_vision.time, "sleep", lambda s: None)
    client = _FakeClient([_StatusError(401)])

    with pytest.raises(VisionError) as info:
        llm_vision.transcribe(Image.new("RGB", (50, 50), "white"), _profile(), client=client)

    assert info.value.fatal is True
    assert len(client.calls) == 1
    assert "hemlig" not in str(info.value)


def test_transcribe_server_error_retries_then_skips(monkeypatch) -> None:
    monkeypatch.setattr(llm_vision.time, "sleep", lambda s: None)
    client = _FakeClient([_StatusError(500), _StatusError(503), _response("ok")])

    result = llm_vision.transcribe(Image.new("RGB", (50, 50), "white"), _profile(), client=client)

    assert result.markdown == "ok" and len(client.calls) == 3


def test_check_profile_rejects_model_without_vision() -> None:
    client = _FakeClient([_response("Jag kan inte se bilder.")])

    with pytest.raises(VisionError) as info:
        llm_vision.check_profile(_profile("Textmodell"), client=client)

    assert info.value.fatal is True
    assert "Textmodell" in str(info.value)
    assert "kan inte läsa bilder" in str(info.value)


def test_check_profile_accepts_reading_model() -> None:
    client = _FakeClient([_response("PALME 1986")])

    llm_vision.check_profile(_profile(), client=client)

    assert _image_url(client.calls[0]).startswith("data:image/png;base64,")


def test_load_profile_marks_external_and_rejects_claude(monkeypatch) -> None:
    profiles = {
        "Router": {"backend_name": "OpenRouter", "provider": "openai",
                   "model": "google/gemini-2.5-flash", "api_key_env": "OPENROUTER_API_KEY"},
        "Lokal": {"backend_name": "Ollama (lokal)", "provider": "openai",
                  "model": "qwen2.5vl", "base_url": "http://localhost:11434/v1"},
        "Claude": {"backend_name": "Claude", "provider": "claude", "model": "claude-opus-4-8"},
    }

    def fake_load(name):
        if name not in profiles:
            raise ValueError(f"Okänd LLM-konfiguration {name!r}.")
        return profiles[name]

    monkeypatch.setattr(llm_vision._llm_config, "load_profile", fake_load)
    monkeypatch.setenv("OPENROUTER_API_KEY", "nyckel")

    router = llm_vision.load_profile("Router")
    assert router.external is True
    assert router.model == "google/gemini-2.5-flash"
    assert router.base_url == "https://openrouter.ai/api/v1"
    assert router.api_key == "nyckel"
    assert "nyckel" not in repr(router)

    lokal = llm_vision.load_profile("Lokal")
    assert lokal.external is False

    with pytest.raises(ValueError, match="Claude"):
        llm_vision.load_profile("Claude")
    with pytest.raises(ValueError):
        llm_vision.load_profile("Finns inte")
    with pytest.raises(ValueError):
        llm_vision.load_profile("")


def test_destination_names_host_and_kind() -> None:
    assert llm_vision.destination(_profile()) == "example.org (extern)"
    local = VisionProfile(name="L", model="m", base_url="http://127.0.0.1:8080/v1",
                          api_key="", external=False)
    assert llm_vision.destination(local) == "127.0.0.1:8080 (lokal)"


def test_insert_mask_markers_and_text_agree() -> None:
    markdown = "## Rubrik\nRad två\nRad tre\nRad fyra"

    marked = llm_vision.insert_mask_markers(markdown, [(120, 280)], 400)

    lines = marked.split("\n")
    assert lines[lines.index("[MASKAD]") + 2] == "Rad tre"
    assert marked.index("[MASKAD]") < marked.index("Rad tre")
    assert marked.index("Rad två") < marked.index("[MASKAD]")
    # eget stycke: tomma rader runt markören
    i = lines.index("[MASKAD]")
    assert lines[i - 1] == "" and lines[i + 1] == ""

    text = llm_vision.markdown_to_text(marked)
    assert "[MASKAD]" in text
    assert "## " not in text and "Rubrik" in text
    assert text.index("Rad två") < text.index("[MASKAD]") < text.index("Rad tre")


def test_insert_mask_markers_without_blocks_is_unchanged() -> None:
    assert llm_vision.insert_mask_markers("a\nb", [], 400) == "a\nb"


def test_insert_mask_markers_never_splits_a_table() -> None:
    markdown = "Före\n| A | B |\n|---|---|\n| 1 | 2 |\nEfter"

    marked = llm_vision.insert_mask_markers(markdown, [(190, 210)], 400)

    assert "| 1 | 2 |\n\n[MASKAD]" in marked
    assert marked.index("[MASKAD]") < marked.index("Efter")


def test_markdown_to_text_strips_syntax() -> None:
    markdown = (
        "# Förhör med **Anna**\n"
        "- första punkten\n"
        "2. andra punkten\n"
        "## **Rubrik** __två__\n"
        "| Namn | Tid |\n"
        "|:---|---:|\n"
        "| Anna | 21.20 |\n"
        "> citat\n"
        "[oläsligt] och [MASKAD]"
    )

    text = llm_vision.markdown_to_text(markdown)

    assert text.split("\n") == [
        "Förhör med Anna",
        "- första punkten",
        "2. andra punkten",
        "Rubrik två",
        "Namn\tTid",
        "Anna\t21.20",
        "citat",
        "[oläsligt] och [MASKAD]",
    ]


@pytest.mark.parametrize("line", ["12. Han sa", "2) Andra punkten", "* * *",
                                  "* stjärnpunkt", "+ pluspunkt", "  3. indragen"])
def test_markdown_to_text_keeps_list_markers_verbatim(line: str) -> None:
    # Punktnummer och listtecken är arkivets text: den rena texten indexeras,
    # citeras och poängsätts och ska vara ordagrann.
    assert llm_vision.markdown_to_text(line) == line


def test_vision_profile_names_lists_openai_compatible_profiles_default_first(
        tmp_path, monkeypatch) -> None:
    import json

    import config

    fil = tmp_path / "llm_config.json"
    fil.write_text(json.dumps({
        "profiles": {
            "Claude": {"backend_name": "Claude"},
            "Router": {"backend_name": "OpenRouter", "model": "openai/gpt-4o"},
            "Lokal": {"backend_name": "Ollama (lokal)"},
            "Borttagen": {"backend_name": "Finns inte"},
        },
        "default": "Lokal",
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", fil)

    assert llm_vision.vision_profile_names() == ["Lokal", "Router"]


def test_prompt_is_swedish_and_asks_for_mask_markers() -> None:
    assert "[oläsligt]" in llm_vision.PROMPT
    assert "Markdown" in llm_vision.PROMPT
    assert "[MASKAD]" in llm_vision.PROMPT


@pytest.mark.parametrize("marker", [
    "[svärtat]", "[Svärtad]", "[ svärtade ]", "[maskerat]", "[maskerad text]",
    "[överstruket]", "[censurerat]", "[redacted]", "[REDACTED]", "[blacked out]",
    "[svart fält]", "███", "■■■■", "[MASKAD]",
])
def test_normalize_mask_markers_rewrites_model_variants(marker: str) -> None:
    out = llm_vision.normalize_mask_markers(f"bodde på {marker} sedan 1983")
    assert out == "bodde på [MASKAD] sedan 1983"


def test_normalize_mask_markers_leaves_other_brackets() -> None:
    text = "[oläsligt] och [sic] och [1] och ett ensamt █"
    assert llm_vision.normalize_mask_markers(text) == text


def test_apply_masks_keeps_model_markers_and_skips_pixel_insertion() -> None:
    """Regression: modellens [svärtat] stod kvar och pixeldetekteringen lade
    dessutom till lika många [MASKAD] som egna stycken — dubbla markörer."""
    markdown = "Rad ett [svärtat]\nRad två\nStockholm, [svärtat]."

    out = llm_vision.apply_masks(markdown, [(10, 20), (300, 320)], 400)

    assert out == "Rad ett [MASKAD]\nRad två\nStockholm, [MASKAD]."


def test_apply_masks_falls_back_to_pixel_blocks_without_model_markers() -> None:
    markdown = "Rad ett\nRad två\nRad tre\nRad fyra"

    out = llm_vision.apply_masks(markdown, [(120, 280)], 400)

    assert out == llm_vision.insert_mask_markers(markdown, [(120, 280)], 400)
    assert out.count("[MASKAD]") == 1

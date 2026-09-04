from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import llm_correct
from llm_correct import _correct_all, _openai, _resolve_api_key
from operations.exceptions import OperationFailed


class _FakeAsyncOpenAIClient:
    def __init__(self, response: MagicMock) -> None:
        self.chat = MagicMock()
        self.chat.completions.create = AsyncMock(return_value=response)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.closed = True


def _openai_response(content: str) -> MagicMock:
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    return mock_response


def test_claude_reads_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _resolve_api_key("claude", base_url="", explicit_key="") == "test-key"


def test_claude_reads_oauth_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _resolve_api_key("claude", base_url="", explicit_key="") == "oauth-tok"


def test_claude_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(OperationFailed):
        _resolve_api_key("claude", base_url="", explicit_key="")


def test_openai_reads_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _resolve_api_key("openai", base_url="", explicit_key="") == "sk-test"


def test_openai_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(OperationFailed):
        _resolve_api_key("openai", base_url="", explicit_key="")


def test_openai_custom_base_url_no_validation(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Inget fel ens utan nyckel när base_url är satt
    result = _resolve_api_key("openai", base_url="http://localhost:11434/v1", explicit_key="")
    assert result == ""


def test_explicit_key_overrides_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    result = _resolve_api_key("openai", base_url="", explicit_key="my-key")
    assert result == "my-key"


def test_deepseek_base_url_reads_deepseek_env(monkeypatch):
    # DeepSeek har custom base_url MEN kräver nyckel — ska läsas ur DEEPSEEK_API_KEY.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    result = _resolve_api_key("openai", base_url="https://api.deepseek.com/v1", explicit_key="")
    assert result == "ds-key"


def test_localhost_base_url_stays_keyless_even_with_deepseek_env(monkeypatch):
    # Lokal server (Ollama) ska fortsatt köra utan nyckel, även om DEEPSEEK_API_KEY råkar vara satt.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    result = _resolve_api_key("openai", base_url="http://localhost:11434/v1", explicit_key="")
    assert result == ""


def test_deepseek_base_url_missing_key_raises(monkeypatch):
    # Känd fjärr-provider utan sin env-nyckel ska avbryta med vägledning, inte tyst tom sträng.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(OperationFailed):
        _resolve_api_key("openai", base_url="https://api.deepseek.com/v1", explicit_key="")


def test_profile_env_reads_custom_key(monkeypatch):
    monkeypatch.setenv("QWEN_KEY", "qwen-secret")
    result = _resolve_api_key(
        "openai", base_url="http://internal/v1", explicit_key="", profile_env="QWEN_KEY"
    )
    assert result == "qwen-secret"


def test_profile_env_missing_key_raises(monkeypatch):
    monkeypatch.delenv("QWEN_KEY", raising=False)
    with pytest.raises(OperationFailed, match="QWEN_KEY"):
        _resolve_api_key(
            "openai", base_url="http://internal/v1", explicit_key="", profile_env="QWEN_KEY"
        )


def test_explicit_key_wins_over_profile_env(monkeypatch):
    monkeypatch.setenv("QWEN_KEY", "qwen-secret")
    result = _resolve_api_key(
        "openai", base_url="http://internal/v1", explicit_key="explicit", profile_env="QWEN_KEY"
    )
    assert result == "explicit"


def test_openai_returns_corrected_text():
    mock_client = _FakeAsyncOpenAIClient(_openai_response("rättad text"))

    with patch("llm_correct.AsyncOpenAI", return_value=mock_client):
        result = asyncio.run(_openai(
            text="OCR-skräp text",
            model="gpt-4o-mini",
            base_url="",
            api_key="sk-test",
        ))
    assert result == "rättad text"
    assert mock_client.closed


def test_openai_fallback_on_empty_response():
    mock_client = _FakeAsyncOpenAIClient(_openai_response(""))

    with patch("llm_correct.AsyncOpenAI", return_value=mock_client):
        result = asyncio.run(_openai(
            text="original text",
            model="gpt-4o-mini",
            base_url="",
            api_key="sk-test",
        ))
    assert result == "original text"
    assert mock_client.closed


def test_openai_uses_local_as_fallback_api_key():
    mock_client = _FakeAsyncOpenAIClient(_openai_response("svar"))

    captured_kwargs = {}

    def capture_client(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_client

    with patch("llm_correct.AsyncOpenAI", side_effect=capture_client):
        asyncio.run(_openai(
            text="text",
            model="llama3.1:8b",
            base_url="http://localhost:11434/v1",
            api_key="",
        ))
    assert captured_kwargs["api_key"] == "local"
    assert captured_kwargs["base_url"] == "http://localhost:11434/v1"
    assert mock_client.closed


def test_correct_all_dispatches_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()

    stem = "testdok"
    (txt_dir / f"{stem}.txt").write_text("sida ett\fsida två", encoding="utf-8")

    provider_cfg = {
        "provider": "claude",
        "model": "claude-haiku-4-5-20251001",
        "base_url": "",
        "api_key": "",
    }

    with patch("llm_correct._claude", new=AsyncMock(return_value="rättad")) as mock_claude, \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1]},
            txt_dir=txt_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
        ))
    mock_claude.assert_called_once()


def test_correct_all_dispatches_to_openai(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()

    stem = "testdok"
    (txt_dir / f"{stem}.txt").write_text("sida ett\fsida två", encoding="utf-8")

    provider_cfg = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "base_url": "",
        "api_key": "sk-test",
    }

    with patch("llm_correct._openai", new=AsyncMock(return_value="rättad")) as mock_openai, \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1]},
            txt_dir=txt_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
        ))
    mock_openai.assert_called_once()


def test_correct_all_runs_pages_concurrently(tmp_path, monkeypatch):
    # jobs=3 ska köra tre sidor samtidigt: räkna max antal överlappande anrop.
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / "testdok.txt").write_text("a\fb\fc", encoding="utf-8")

    provider_cfg = {"provider": "claude", "model": "m", "base_url": "", "api_key": ""}

    inflight = 0
    peak = 0

    async def fake_claude(text, model):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return "rättad"

    with patch("llm_correct._claude", new=fake_claude), \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1, 2, 3]},
            txt_dir=txt_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
            jobs=3,
        ))
    assert peak == 3


def test_correct_all_semaphore_caps_concurrency(tmp_path, monkeypatch):
    # jobs=1 ska serialisera anropen även om flera sidor är dåliga.
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / "testdok.txt").write_text("a\fb\fc", encoding="utf-8")

    provider_cfg = {"provider": "claude", "model": "m", "base_url": "", "api_key": ""}

    inflight = 0
    peak = 0

    async def fake_claude(text, model):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return "rättad"

    with patch("llm_correct._claude", new=fake_claude), \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1, 2, 3]},
            txt_dir=txt_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
            jobs=1,
        ))
    assert peak == 1


def test_correct_all_aborts_when_context_cancelled(tmp_path, monkeypatch):
    # En cancellad context ska få loopen att avbryta innan något LLM-anrop görs.
    from operations.exceptions import OperationCancelled

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / "testdok.txt").write_text("a\fb", encoding="utf-8")

    class CancellingCtx:
        def step(self, name, *, completed=0, total=None) -> None:
            pass

        def progress(self, completed, total, message="") -> None:
            pass

        def log(self, message, *, level="info") -> None:
            pass

        def check_cancelled(self) -> None:
            raise OperationCancelled("avbrutet")

    with patch("llm_correct._claude", new=AsyncMock(return_value="x")) as mock_claude, \
            pytest.raises(OperationCancelled):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1, 2]},
            txt_dir=txt_dir,
            provider_cfg={"provider": "claude", "model": "m",
                          "base_url": "", "api_key": ""},
            dry_run=False,
            ctx=CancellingCtx(),
        ))
    mock_claude.assert_not_called()


class _RecordingCtx:
    """Enkel context som fångar progress/logg-anrop för worker-flödet."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.progress_calls: list[tuple] = []
        self.log_calls: list[tuple[str, str]] = []

    def check_cancelled(self) -> None:
        pass

    def step(self, name, *, completed=0, total=None) -> None:
        self.steps.append(name)

    def progress(self, completed, total, message="") -> None:
        self.progress_calls.append((completed, total, message))

    def log(self, message, *, level="info") -> None:
        self.log_calls.append((message, level))


def test_correct_all_reports_progress_via_context(tmp_path, monkeypatch):
    """Bakgrundsjobb ska få strukturerad progress via ctx.progress, inte print()."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / "testdok.txt").write_text("a\fb\fc", encoding="utf-8")

    ctx = _RecordingCtx()
    with patch("llm_correct._claude", new=AsyncMock(return_value="rättad")), \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1, 2, 3]},
            txt_dir=txt_dir,
            provider_cfg={"provider": "claude", "model": "m",
                          "base_url": "", "api_key": ""},
            dry_run=False,
            ctx=ctx,
        ))

    assert ctx.steps == ["LLM-korrigering"]
    assert len(ctx.progress_calls) == 3
    assert all(total == 3 for _, total, _ in ctx.progress_calls)


def test_correct_all_logs_page_errors_via_context(tmp_path, monkeypatch):
    """Sidfel ska hamna i jobbloggen via ctx.log, inte i DEVNULL-stdout."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / "testdok.txt").write_text("a\fb", encoding="utf-8")

    ctx = _RecordingCtx()

    async def failing(text, model):
        raise RuntimeError("api fel")

    with patch("llm_correct._claude", new=failing), \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1]},
            txt_dir=txt_dir,
            provider_cfg={"provider": "claude", "model": "m",
                          "base_url": "", "api_key": ""},
            dry_run=False,
            ctx=ctx,
        ))

    assert any("api fel" in msg and level == "error" for msg, level in ctx.log_calls)


def _run_llm_correct_capturing_provider_cfg(monkeypatch, tmp_path, saved, **overrides):
    """Kör run_llm_correct med tunga delar mockade; returnera provider_cfg."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    captured: dict = {}

    fake_state_db = MagicMock()
    fake_state_db.connect.return_value = MagicMock()
    fake_state_db.get_bad_pages.return_value = [{"pdf_stem": "dok", "page_num": 1}]
    fake_state_db.llm_corrected.return_value = False

    async def fake_correct_all(bad, txt_dir, provider_cfg, dry_run, jobs=1, ctx=None):
        captured.update(provider_cfg)

    kwargs = dict(threshold=50.0, provider="", model="", base_url="", api_key="",
                  txt=tmp_path / "text", root=tmp_path, jobs=1, dry_run=False,
                  test=None)
    kwargs.update(overrides)

    with patch("llm_correct._llm_config.load", return_value=dict(saved)), \
         patch("llm_correct.state_db", fake_state_db), \
         patch("llm_correct._correct_all", side_effect=fake_correct_all):
        rc = llm_correct.run_llm_correct(**kwargs)
    assert rc == 0
    return captured


def test_llm_correction_rejects_unknown_profile_before_work_starts(tmp_path, monkeypatch):
    """Ett felskrivet profilnamn får aldrig tyst bli Claude-default."""
    config_file = tmp_path / "llm_config.json"
    config_file.write_text(
        '{"profiles": {"Standard": {}}, "default": "Standard"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_correct._llm_config, "CONFIG_FILE", config_file)
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))

    with pytest.raises(OperationFailed, match="Okänd LLM-konfiguration 'Saknas'.*"):
        llm_correct.run_llm_correct(
            threshold=50.0,
            provider="",
            model="",
            base_url="",
            api_key="",
            txt=tmp_path / "text",
            root=tmp_path,
            jobs=1,
            dry_run=True,
            test=None,
            profile="Saknas",
        )


def test_provider_override_without_model_resets_to_provider_default(
    tmp_path, monkeypatch
):
    # Regression för kodgranskningsfynd 11: --provider utan --model ska
    # återställa modellen till provider-default, inte ärva sparad claude-modell.
    saved = {"provider": "claude", "model": "claude-opus-4-8", "base_url": ""}
    captured = _run_llm_correct_capturing_provider_cfg(
        monkeypatch, tmp_path, saved, provider="openai")
    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-4o-mini"


def test_no_overrides_keeps_saved_model(tmp_path, monkeypatch):
    saved = {"provider": "claude", "model": "claude-opus-4-8", "base_url": ""}
    captured = _run_llm_correct_capturing_provider_cfg(monkeypatch, tmp_path, saved)
    assert captured["provider"] == "claude"
    assert captured["model"] == "claude-opus-4-8"


def test_provider_and_model_both_given_uses_explicit_model(tmp_path, monkeypatch):
    saved = {"provider": "claude", "model": "claude-opus-4-8", "base_url": ""}
    captured = _run_llm_correct_capturing_provider_cfg(
        monkeypatch, tmp_path, saved, provider="openai", model="deepseek-chat")
    assert captured["model"] == "deepseek-chat"


def test_main_returns_run_exit_code(monkeypatch):
    # Regression för kodgranskningsfynd 16: main() ska vidarebefordra returkoden.
    monkeypatch.setattr(sys, "argv", ["llm_correct.py"])
    with patch("llm_correct.run_llm_correct", return_value=1) as mock_run:
        assert llm_correct.main() == 1
    mock_run.assert_called_once()

"""Tester för ontologins LLM-anrop (``ontologi.llm``) — alltid mot en falsk klient."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ontologi import llm


class _StatusFel(Exception):
    """Liknar openai.APIStatusError: bär ``status_code``."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _svar(text: str = '{"observationer": []}') -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )


class _FalskKlient:
    def __init__(self, utfall: list) -> None:
        self.utfall = list(utfall)
        self.anrop: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.anrop.append(kwargs)
        nasta = self.utfall.pop(0)
        if isinstance(nasta, Exception):
            raise nasta
        return nasta

    async def close(self) -> None:
        return None


_CFG = {"kind": "openai", "backend_name": "OpenAI", "model": "gpt-test",
        "api_key": "hemlig", "base_url": ""}


def _kor(klient, cfg=_CFG):
    vantan: list[float] = []

    async def sov(s: float) -> None:
        vantan.append(s)

    res = asyncio.run(llm.call_page("prompt", "system", cfg, sleep=sov,
                                    client_factory=lambda _cfg: klient))
    return res, vantan


def test_retry_on_429_then_success() -> None:
    klient = _FalskKlient([_StatusFel(429), _StatusFel(429), _svar("ok-text")])
    (text, usage), vantan = _kor(klient)
    assert text == "ok-text"
    assert len(klient.anrop) == 3
    assert vantan == [2, 4]
    assert usage == {"input_tokens": 120, "output_tokens": 30}


def test_401_is_fatal_without_retry() -> None:
    klient = _FalskKlient([_StatusFel(401), _svar()])
    with pytest.raises(llm.LlmFatal):
        _kor(klient)
    assert len(klient.anrop) == 1


def test_429_after_last_attempt_is_fatal() -> None:
    klient = _FalskKlient([_StatusFel(429)] * 4)
    with pytest.raises(llm.LlmFatal):
        _kor(klient)
    assert len(klient.anrop) == 4


def test_5xx_after_last_attempt_is_a_page_error_not_fatal() -> None:
    klient = _FalskKlient([_StatusFel(503)] * 4)
    with pytest.raises(llm.LlmPageError) as info:
        _kor(klient)
    assert not isinstance(info.value, llm.LlmFatal)
    assert len(klient.anrop) == 4


def test_400_is_a_page_error_without_retry() -> None:
    klient = _FalskKlient([_StatusFel(400), _svar()])
    with pytest.raises(llm.LlmPageError):
        _kor(klient)
    assert len(klient.anrop) == 1


def test_json_format_only_for_openai_backend() -> None:
    klient = _FalskKlient([_svar()])
    _kor(klient)
    assert klient.anrop[0]["response_format"] == {"type": "json_object"}
    assert klient.anrop[0]["max_tokens"] == 16000
    assert klient.anrop[0]["messages"][0] == {"role": "system", "content": "system"}

    klient = _FalskKlient([_svar()])
    _kor(klient, dict(_CFG, backend_name="DeepSeek"))
    assert "response_format" not in klient.anrop[0]


def test_unknown_usage_is_none() -> None:
    svar = _svar()
    svar.usage = None
    (_, usage), _ = _kor(_FalskKlient([svar]))
    assert usage == {"input_tokens": None, "output_tokens": None}


@pytest.mark.parametrize("orsak", ["length", "content_filter", "insufficient_system_resource"])
def test_truncated_answer_is_a_page_error(orsak) -> None:
    svar = _svar('{"observationer": [')
    svar.choices[0].finish_reason = orsak
    klient = _FalskKlient([svar])
    with pytest.raises(llm.LlmPageError, match=orsak):
        _kor(klient)
    assert len(klient.anrop) == 1


@pytest.mark.parametrize("orsak", ["stop", None])
def test_complete_answer_is_returned(orsak) -> None:
    svar = _svar()
    svar.choices[0].finish_reason = orsak
    (text, _), _ = _kor(_FalskKlient([svar]))
    assert text == '{"observationer": []}'


def _claude_meddelanden(stop_reason):
    import claude_agent_sdk as sdk

    return [
        sdk.AssistantMessage(content=[sdk.TextBlock(text='{"observationer": []}')],
                             model="claude-test"),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                          is_error=False, num_turns=1, session_id="s",
                          stop_reason=stop_reason,
                          usage={"input_tokens": 10, "output_tokens": 5}),
    ]


@pytest.mark.parametrize("stop_reason,avklippt", [("end_turn", False), (None, False),
                                                   ("max_tokens", True)])
def test_claude_truncated_answer_is_a_page_error(monkeypatch, stop_reason, avklippt) -> None:
    import claude_agent_sdk as sdk

    async def falsk_query(*, prompt, options):
        for m in _claude_meddelanden(stop_reason):
            yield m

    monkeypatch.setattr(sdk, "query", falsk_query)
    cfg = {"kind": "claude", "model": "claude-test"}
    if avklippt:
        with pytest.raises(llm.LlmPageError, match="max_tokens"):
            _kor(None, cfg)
    else:
        (text, usage), _ = _kor(None, cfg)
        assert text == '{"observationer": []}'
        assert usage == {"input_tokens": 10, "output_tokens": 5}

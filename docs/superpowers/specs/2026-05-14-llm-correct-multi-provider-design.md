# Design: multi-provider stöd i llm_correct

**Datum:** 2026-05-14
**Status:** Godkänd

## Bakgrund

`llm_correct.py` rättar dåliga OCR-sidor via LLM. Idag är Claude hårdkodat via `claude_agent_sdk`. Webui.py stöder redan Claude, OpenAI och DeepSeek — samma flexibilitet ska finnas i `llm_correct`.

## Nya flaggor

| Flagga | Default | Beskrivning |
|---|---|---|
| `--provider` | `claude` | `claude` eller `openai` |
| `--base-url` | _(tom)_ | Override API-URL (DeepSeek, Ollama, llama.cpp, vLLM, etc.) |
| `--api-key` | _(tom)_ | Override API-nyckel (annars läses från env) |
| `--model` | _(per provider)_ | claude → `claude-haiku-4-5-20251001`, openai → `gpt-4o-mini` |

## Nyckelvalidering

- `--provider claude`: kräver `CLAUDE_CODE_OAUTH_TOKEN` eller `ANTHROPIC_API_KEY`
- `--provider openai` utan `--base-url`: kräver `OPENAI_API_KEY`
- `--provider openai` med `--base-url`: ingen validering — användaren ansvarar

## Kodändringar i `src/llm_correct.py`

### Nya/ändrade funktioner

- `_claude(text, model)` — nuvarande `_haiku`, namnbyte för tydlighet
- `_openai(text, model, base_url, api_key)` — ny async-funktion, använder `openai.AsyncOpenAI`
- `_correct_all(bad, txt_dir, pages_dir, provider_cfg, dry_run)` — tar `provider_cfg: dict` med nycklarna `provider`, `model`, `base_url`, `api_key`; delegerar till `_claude` eller `_openai`

### provider_cfg-struktur

```python
{
    "provider": "claude" | "openai",
    "model": str,
    "base_url": str,   # tom sträng om ej satt
    "api_key": str,    # tom sträng om ej satt
}
```

### Idempotens

`.llm`-markörer fungerar oavsett provider — ingen förändring.

## Ändringar i `llm_correct.sh`

Bara usage-texten uppdateras med `--provider`, `--base-url`, `--api-key`. Alla flaggor passerar redan igenom via `extra`-arrayen.

## Exempel

```bash
# Claude Haiku (default, som idag)
./llm_correct.sh --threshold 60

# OpenAI GPT-4o-mini
./llm_correct.sh --provider openai --model gpt-4o-mini

# DeepSeek via OpenAI-kompatibelt API
./llm_correct.sh --provider openai --base-url https://api.deepseek.com/v1 --model deepseek-chat

# Lokal Ollama
./llm_correct.sh --provider openai --base-url http://localhost:11434/v1 --model llama3.1:8b
```

## Ej i scope

- Namngivna backends (t.ex. `--provider deepseek`) — täcks av `--provider openai --base-url`
- Strömning/streaming — inte relevant för batch-korrektion
- Parallellitet — styrs redan av befintlig logik

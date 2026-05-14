# llm_correct multi-provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lägg till `--provider`, `--base-url` och `--api-key` flaggor i `llm_correct` så att man kan använda valfri OpenAI-kompatibel LLM utöver Claude.

**Architecture:** Ny async-funktion `_openai` hanterar OpenAI-kompatibla providers. Existerande `_haiku` döps om till `_claude`. `_correct_all` tar en `provider_cfg: dict` istället för bara `model`. En ren `_resolve_api_key`-funktion hanterar nyckelvalidering (testbar utan sidoeffekter).

**Tech Stack:** Python 3.14, `openai` (AsyncOpenAI, redan en dependency), `claude_agent_sdk`, `pytest`, `pytest-asyncio`

---

### Task 1: Byt namn på `_haiku` → `_claude` och extrahera `_resolve_api_key`

**Files:**
- Modify: `src/llm_correct.py`
- Create: `tests/test_llm_correct.py`

- [ ] **Steg 1: Skriv det misslyckande testet**

```python
# tests/test_llm_correct.py
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_correct import _resolve_api_key


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
    with pytest.raises(SystemExit):
        _resolve_api_key("claude", base_url="", explicit_key="")


def test_openai_reads_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _resolve_api_key("openai", base_url="", explicit_key="") == "sk-test"


def test_openai_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
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
```

- [ ] **Steg 2: Kör testet — ska misslyckas**

```bash
.venv/bin/pytest tests/test_llm_correct.py -v
```

Förväntat: `ImportError: cannot import name '_resolve_api_key' from 'llm_correct'`

- [ ] **Steg 3: Döp om `_haiku` → `_claude` och lägg till `_resolve_api_key` i `src/llm_correct.py`**

Byt namn på funktionen (rad 40 i nuläget):
```python
async def _claude(text: str, model: str) -> str:
```

Lägg till direkt efter `HAIKU_MODEL`-konstanten (rad 29) en ny konstant och en ny funktion:

```python
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


def _resolve_api_key(provider: str, base_url: str, explicit_key: str) -> str:
    if explicit_key:
        return explicit_key
    if provider == "claude":
        key = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or
               os.environ.get("ANTHROPIC_API_KEY") or "")
        if not key:
            print("Sätt CLAUDE_CODE_OAUTH_TOKEN eller ANTHROPIC_API_KEY.", file=sys.stderr)
            sys.exit(1)
        return key
    # openai
    if not base_url:
        key = os.environ.get("OPENAI_API_KEY") or ""
        if not key:
            print("Sätt OPENAI_API_KEY.", file=sys.stderr)
            sys.exit(1)
        return key
    return ""  # custom base_url → ingen validering
```

- [ ] **Steg 4: Uppdatera anropet till `_haiku` i `_correct_all` (rad 105)**

```python
corrected = await _claude(page_text, model)
```

- [ ] **Steg 5: Kör testerna — ska vara gröna**

```bash
.venv/bin/pytest tests/test_llm_correct.py -v
```

Förväntat: 7 PASSED

- [ ] **Steg 6: Commit**

```bash
git add src/llm_correct.py tests/test_llm_correct.py
git commit -m "refactor: döp om _haiku → _claude, extrahera _resolve_api_key"
```

---

### Task 2: Lägg till `_openai`-funktion

**Files:**
- Modify: `src/llm_correct.py`
- Modify: `tests/test_llm_correct.py`

- [ ] **Steg 1: Skriv det misslyckande testet**

Lägg till i `tests/test_llm_correct.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from llm_correct import _openai


def test_openai_returns_corrected_text():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "rättad text"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch("llm_correct.AsyncOpenAI", return_value=mock_client):
        result = asyncio.run(_openai(
            text="OCR-skräp text",
            model="gpt-4o-mini",
            base_url="",
            api_key="sk-test",
        ))
    assert result == "rättad text"


def test_openai_fallback_on_empty_response():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = ""

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch("llm_correct.AsyncOpenAI", return_value=mock_client):
        result = asyncio.run(_openai(
            text="original text",
            model="gpt-4o-mini",
            base_url="",
            api_key="sk-test",
        ))
    assert result == "original text"


def test_openai_uses_local_as_fallback_api_key():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "svar"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

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
```

- [ ] **Steg 2: Kör testet — ska misslyckas**

```bash
.venv/bin/pytest tests/test_llm_correct.py::test_openai_returns_corrected_text -v
```

Förväntat: `ImportError: cannot import name '_openai' from 'llm_correct'`

- [ ] **Steg 3: Lägg till `_openai` i `src/llm_correct.py`**

Lägg till direkt efter `_claude`-funktionen (efter rad ~54):

```python
async def _openai(text: str, model: str, base_url: str, api_key: str) -> str:
    client = AsyncOpenAI(
        api_key=api_key or "local",
        base_url=base_url or None,
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text},
        ],
        max_tokens=4096,
    )
    return response.choices[0].message.content or text
```

Lägg till modul-nivå-import av `AsyncOpenAI` (krävs för att testerna ska kunna patcha `llm_correct.AsyncOpenAI`). Lägg till direkt under de befintliga importarna:

```python
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]
```

- [ ] **Steg 4: Kör testerna — ska vara gröna**

```bash
.venv/bin/pytest tests/test_llm_correct.py -v
```

Förväntat: 10 PASSED

- [ ] **Steg 5: Commit**

```bash
git add src/llm_correct.py tests/test_llm_correct.py
git commit -m "feat: lägg till _openai-funktion med AsyncOpenAI"
```

---

### Task 3: Uppdatera `_correct_all` att ta `provider_cfg`

**Files:**
- Modify: `src/llm_correct.py`
- Modify: `tests/test_llm_correct.py`

- [ ] **Steg 1: Skriv det misslyckande testet**

Lägg till i `tests/test_llm_correct.py`:

```python
import asyncio
from unittest.mock import patch, AsyncMock
from llm_correct import _correct_all


def test_correct_all_dispatches_to_claude(tmp_path):
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    pages_dir = tmp_path / "text_pages"
    pages_dir.mkdir()

    stem = "testdok"
    (txt_dir / f"{stem}.txt").write_text("sida ett\fsida två", encoding="utf-8")
    (pages_dir / stem).mkdir()

    provider_cfg = {
        "provider": "claude",
        "model": "claude-haiku-4-5-20251001",
        "base_url": "",
        "api_key": "",
    }

    with patch("llm_correct._claude", new=AsyncMock(return_value="rättad")) as mock_claude, \
         patch("llm_correct.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1]},
            txt_dir=txt_dir,
            pages_dir=pages_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
        ))
    mock_claude.assert_called_once()


def test_correct_all_dispatches_to_openai(tmp_path):
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    pages_dir = tmp_path / "text_pages"
    pages_dir.mkdir()

    stem = "testdok"
    (txt_dir / f"{stem}.txt").write_text("sida ett\fsida två", encoding="utf-8")
    (pages_dir / stem).mkdir()

    provider_cfg = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "base_url": "",
        "api_key": "sk-test",
    }

    with patch("llm_correct._openai", new=AsyncMock(return_value="rättad")) as mock_openai, \
         patch("llm_correct.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1]},
            txt_dir=txt_dir,
            pages_dir=pages_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
        ))
    mock_openai.assert_called_once()
```

- [ ] **Steg 2: Kör testerna — ska misslyckas**

```bash
.venv/bin/pytest tests/test_llm_correct.py::test_correct_all_dispatches_to_claude tests/test_llm_correct.py::test_correct_all_dispatches_to_openai -v
```

Förväntat: `TypeError` (felaktigt anropsformat för `_correct_all`)

- [ ] **Steg 3: Uppdatera `_correct_all`-signaturen och innanmätet**

Ersätt funktionssignaturen och `corrected`-anropet:

```python
async def _correct_all(
    bad: dict[str, list[int]],
    txt_dir: Path,
    pages_dir: Path,
    provider_cfg: dict,
    dry_run: bool,
) -> None:
    from merge_pages import merge_one  # noqa: PLC0415
```

Ersätt `corrected = await _haiku(page_text, model)` (numera `_claude`) med:

```python
if provider_cfg["provider"] == "claude":
    corrected = await _claude(page_text, provider_cfg["model"])
else:
    corrected = await _openai(
        page_text,
        provider_cfg["model"],
        provider_cfg["base_url"],
        provider_cfg["api_key"],
    )
```

- [ ] **Steg 4: Kör alla tester — ska vara gröna**

```bash
.venv/bin/pytest tests/test_llm_correct.py -v
```

Förväntat: 12 PASSED

- [ ] **Steg 5: Commit**

```bash
git add src/llm_correct.py tests/test_llm_correct.py
git commit -m "refactor: _correct_all tar provider_cfg istället för model"
```

---

### Task 4: Uppdatera `main()` med nya flaggor

**Files:**
- Modify: `src/llm_correct.py`

- [ ] **Steg 1: Lägg till argumenten och bygg `provider_cfg` i `main()`**

Ersätt hela `main()`-funktionen i `src/llm_correct.py`:

```python
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description='LLM-korrektion av dåliga OCR-sidor.'
    )
    ap.add_argument('--threshold', type=float, default=50.0,
                    help='score-tröskel (default: 50)')
    ap.add_argument('--provider', default='claude', choices=['claude', 'openai'],
                    help='LLM-provider (default: claude)')
    ap.add_argument('--model', default='',
                    help='modellnamn (default: haiku för claude, gpt-4o-mini för openai)')
    ap.add_argument('--base-url', default='',
                    help='override API-URL för OpenAI-kompatibla providers (Ollama, DeepSeek, ...)')
    ap.add_argument('--api-key', default='',
                    help='override API-nyckel (annars läses från env)')
    ap.add_argument('--pages-jsonl', default='',
                    help='quality_pages.jsonl (default: <root>/quality_pages.jsonl)')
    ap.add_argument('--txt', default='',
                    help='text-katalog (default: <root>/text)')
    ap.add_argument('--pages-out', default='',
                    help='text_pages-katalog (default: <root>/text_pages)')
    ap.add_argument('--root', default='', help='projektrot')
    ap.add_argument('--dry-run', action='store_true',
                    help='visa vad som skulle rättas utan att göra det')
    args = ap.parse_args()

    root = Path(args.root) if args.root else ROOT
    jsonl = Path(args.pages_jsonl) if args.pages_jsonl else root / 'generated' / 'quality_pages.jsonl'
    txt_dir = Path(args.txt) if args.txt else root / 'generated' / 'text'
    pages_dir = Path(args.pages_out) if args.pages_out else root / 'generated' / 'text_pages'

    if not jsonl.exists():
        print(f'Saknar {jsonl} — kör ./quality.sh --per-page först.', file=sys.stderr)
        sys.exit(1)

    default_model = HAIKU_MODEL if args.provider == 'claude' else OPENAI_DEFAULT_MODEL
    model = args.model or default_model
    api_key = _resolve_api_key(args.provider, args.base_url, args.api_key)

    provider_cfg = {
        "provider": args.provider,
        "model": model,
        "base_url": args.base_url,
        "api_key": api_key,
    }

    # Samla dåliga sidor ur JSONL
    raw: dict[str, list[int]] = defaultdict(list)
    with open(jsonl, encoding='utf-8') as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            score = float(row.get('score') or 0.0)
            if score < args.threshold:
                raw[row['file']].append(int(row['page']))

    # Filtrera bort sidor som redan är rättade (.llm-markör)
    bad: dict[str, list[int]] = defaultdict(list)
    skipped = 0
    for txt_name, pages in raw.items():
        stem = txt_name[:-4] if txt_name.endswith('.txt') else txt_name
        stem_dir = pages_dir / stem
        for p in pages:
            if (stem_dir / f'page-{p:03d}.llm').exists():
                skipped += 1
            else:
                bad[txt_name].append(p)

    total = sum(len(v) for v in bad.values())
    if not bad:
        print(f'Inga nya sidor att rätta ({skipped} redan rättade).')
        return

    print(f'Rättar {total} sidor i {len(bad)} filer'
          + (f' ({skipped} redan rättade hoppas över)' if skipped else '')
          + f' med {args.provider}/{model}.')
    if args.dry_run:
        print('[dry-run — inga filer skrivs]')

    asyncio.run(_correct_all(bad, txt_dir, pages_dir, provider_cfg, args.dry_run))

    if not args.dry_run:
        print('\nKlart. Kör ./quality.sh för att se förbättringen.')
        print('Kör ./ingest.sh för att re-indexera ändrade filer.')
```

- [ ] **Steg 2: Kör alla tester**

```bash
.venv/bin/pytest tests/test_llm_correct.py -v
```

Förväntat: 12 PASSED

- [ ] **Steg 3: Verifiera att `--help` ser rätt ut**

```bash
.venv/bin/python src/llm_correct.py --help
```

Förväntat: Flaggorna `--provider`, `--base-url`, `--api-key`, `--model` syns med beskrivningar.

- [ ] **Steg 4: Commit**

```bash
git add src/llm_correct.py
git commit -m "feat: lägg till --provider, --base-url, --api-key i llm_correct"
```

---

### Task 5: Uppdatera `llm_correct.sh` usage-text

**Files:**
- Modify: `llm_correct.sh`

- [ ] **Steg 1: Ersätt `usage()`-funktionen**

Ersätt hela `usage()`-funktionen i `llm_correct.sh`:

```bash
usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot (\$PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/llm_correct.py (alla okända flaggor passerar igenom):
  --threshold N           score-tröskel (default: 50)
  --provider PROVIDER     claude (default) eller openai
  --model MODEL           modellnamn (default: claude-haiku-4-5-20251001 / gpt-4o-mini)
  --base-url URL          override API-URL, t.ex. https://api.deepseek.com/v1
                          eller http://localhost:11434/v1 för Ollama
  --api-key KEY           override API-nyckel (annars läses från env)
  --pages-jsonl FILE      quality_pages.jsonl (default: <root>/generated/quality_pages.jsonl)
  --txt DIR               text-katalog (default: <root>/generated/text)
  --pages-out DIR         text_pages-katalog (default: <root>/generated/text_pages)
  --dry-run               visa vad som skulle rättas utan att göra det

Exempel:
  ./llm_correct.sh --threshold 60
  ./llm_correct.sh --provider openai --model gpt-4o-mini
  ./llm_correct.sh --provider openai --base-url https://api.deepseek.com/v1 --model deepseek-chat
  ./llm_correct.sh --provider openai --base-url http://localhost:11434/v1 --model llama3.1:8b
EOF
}
```

- [ ] **Steg 2: Verifiera att `--help` i `.sh` ser rätt ut**

```bash
./llm_correct.sh --help
```

Förväntat: Nya flaggor och exempelkommandon syns.

- [ ] **Steg 3: Commit**

```bash
git add llm_correct.sh
git commit -m "docs: uppdatera llm_correct.sh usage med nya provider-flaggor"
```

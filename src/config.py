"""Delad LLM-konfiguration — läses av webui och llm_correct."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "generated" / "llm_config.json"

_DEFAULTS: dict = {
    "backend_name": "Claude",
    "provider": "claude",
    "model": "claude-opus-4-8",
    "base_url": "",
}


def load() -> dict:
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **stored}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )

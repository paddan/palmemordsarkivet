"""CLI för att visa/ändra LLM-konfigurationen (generated/llm_config.json)
utan att starta webgränssnittet. Konsumeras av webui, llm_correct och
graph/extract_entities.

Kör:
    ./llm_config.sh                        # visa aktuell konfig
    ./llm_config.sh --model claude-haiku-4-5-20251001
    ./llm_config.sh --provider openai --base-url https://api.deepseek.com/v1 --model deepseek-chat
    ./llm_config.sh --reset                # tillbaka till defaults
"""
from __future__ import annotations

import argparse
import sys

import config

# Default-modeller per provider när man byter provider utan att ange --model.
# Matchar DEFAULT_CLAUDE_MODEL/OPENAI_DEFAULT_MODEL i graph/extract_entities.py.
PROVIDER_DEFAULT_MODELS = {
    "claude": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

# Visningsnamn per provider, matchar webui:s namngivning.
PROVIDER_BACKEND_NAMES = {
    "claude": "Claude",
    "openai": "OpenAI",
}


def _print_config(cfg: dict, *, missing_file: bool) -> None:
    print("Aktiv LLM-konfiguration (generated/llm_config.json):")
    print(f"  provider:  {cfg['provider']}")
    print(f"  model:     {cfg['model']}")
    base_url = cfg.get("base_url") or "(ingen)"
    print(f"  base_url:  {base_url}")
    if missing_file:
        print("  (ingen sparad konfig — visar defaults)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Visa/ändra LLM-konfigurationen utan webui.",
    )
    parser.add_argument("--provider", help="claude eller openai")
    parser.add_argument("--model", help="modellnamn")
    parser.add_argument("--base-url", help="override API-URL")
    parser.add_argument(
        "--reset", action="store_true", help="ta bort sparad konfig (tillbaka till defaults)"
    )
    args = parser.parse_args(argv)

    set_flags = args.provider is not None or args.model is not None or args.base_url is not None
    if args.reset and set_flags:
        print("--reset kan inte kombineras med --provider/--model/--base-url", file=sys.stderr)
        return 2

    if args.provider is not None and args.provider not in PROVIDER_DEFAULT_MODELS:
        valid = "/".join(PROVIDER_DEFAULT_MODELS)
        print(f"Ogiltig provider: {args.provider!r} (giltiga värden: {valid})", file=sys.stderr)
        return 2

    if args.reset:
        if config.CONFIG_FILE.exists():
            config.CONFIG_FILE.unlink()
        cfg = config.load()
        _print_config(cfg, missing_file=not config.CONFIG_FILE.exists())
        return 0

    if not set_flags:
        cfg = config.load()
        _print_config(cfg, missing_file=not config.CONFIG_FILE.exists())
        return 0

    cfg = config.load()

    if args.provider is not None:
        cfg["provider"] = args.provider
        cfg["backend_name"] = PROVIDER_BACKEND_NAMES[args.provider]
        if args.model is None:
            cfg["model"] = PROVIDER_DEFAULT_MODELS[args.provider]
            print(
                f"Provider bytt till {args.provider} — modell återställd till "
                f"default ({cfg['model']})."
            )

    if args.model is not None:
        cfg["model"] = args.model

    if args.base_url is not None:
        cfg["base_url"] = args.base_url

    config.save(cfg)
    _print_config(cfg, missing_file=not config.CONFIG_FILE.exists())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

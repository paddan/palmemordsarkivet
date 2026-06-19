"""CLI för att visa/ändra LLM-konfigurationen (generated/llm_config.json)
utan att starta webgränssnittet. Konsumeras av Utredning-sidan, llm_correct och
graph/extract_entities.

Kör:
    ./llm_config.sh                        # interaktiv meny (TTY); annars visa konfig
    ./llm_config.sh --model claude-haiku-4-5-20251001
    ./llm_config.sh --provider openai --base-url https://api.deepseek.com/v1 --model deepseek-chat
    ./llm_config.sh --reset                # tillbaka till defaults

Utan argument från en terminal startas en meny där backend och modell väljs ur
samma katalog som Utredning-sidans sidofält (src/backends.py). Körs utan TTY (pipe/skript)
skrivs den aktuella konfigurationen ut, precis som tidigare.
"""
from __future__ import annotations

import argparse
import os
import sys

import backends
import config

# Default-modeller per provider när man byter provider utan att ange --model.
# Matchar DEFAULT_CLAUDE_MODEL/OPENAI_DEFAULT_MODEL i graph/extract_entities.py.
PROVIDER_DEFAULT_MODELS = {
    "claude": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

# Visningsnamn per provider, matchar Utredning-sidans namngivning.
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


def _prompt_choice(out, read, title, options, *, default=None, allow_custom=False) -> str:
    """Visa en numrerad lista och returnera det valda värdet.

    Tomt svar → default (om satt). ``allow_custom`` lägger till ett extra val som
    läser ett fritt namn (för modeller som inte finns i listan).
    """
    out(title)
    for i, opt in enumerate(options, 1):
        marker = "  (nuvarande)" if opt == default else ""
        out(f"  {i}. {opt}{marker}")
    custom_n = len(options) + 1
    if allow_custom:
        out(f"  {custom_n}. (skriv eget namn)")
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = read(f"Val{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(options):
                return options[n - 1]
            if allow_custom and n == custom_n:
                name = read("Modellnamn: ").strip()
                if name:
                    return name
        out("Ogiltigt val, försök igen.")


def run_menu(read=input, out=print) -> int:
    """Interaktiv meny för att välja backend och modell (speglar Utredning-sidans sidofält)."""
    try:
        return _run_menu(read, out)
    except (EOFError, KeyboardInterrupt):
        out("")
        out("Avbrutet.")
        return 1


def _run_menu(read, out) -> int:
    cfg = config.load()
    out("Aktuell LLM-konfiguration:")
    _print_config(cfg, missing_file=not config.CONFIG_FILE.exists())
    out("")

    names = list(backends.BACKENDS)
    cur_backend = cfg.get("backend_name") if cfg.get("backend_name") in names else names[0]
    backend_name = _prompt_choice(out, read, "Välj backend:", names, default=cur_backend)
    backend = backends.BACKENDS[backend_name]

    base_url = backend.get("base_url", "")
    api_key = ""
    if backend.get("configurable"):
        default_url = cfg.get("base_url") or backend.get("base_url", "")
        entered = read(f"Endpoint-URL [{default_url}]: ").strip()
        base_url = entered or default_url
        api_key = read("API-nyckel (valfritt, Enter för ingen): ").strip()
        backend = {**backend, "base_url": base_url}
    elif backend.get("env"):
        api_key = os.environ.get(backend["env"], "")

    models = backends.available_models(backend, api_key)
    if backend_name == cfg.get("backend_name") and cfg.get("model") in models:
        default_model = cfg["model"]
    elif backend.get("model") in models:
        default_model = backend["model"]
    else:
        default_model = models[0] if models else backend.get("model", "")
    model = _prompt_choice(
        out, read, "Välj modell:", models, default=default_model, allow_custom=True
    )

    new_cfg = {
        "backend_name": backend_name,
        "provider": backend["kind"],
        "model": model,
        "base_url": base_url,
    }
    config.save(new_cfg)
    out("")
    out("Sparad konfiguration:")
    _print_config(new_cfg, missing_file=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Visa/ändra LLM-konfigurationen utan webgränssnittet.",
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
        if sys.stdin.isatty():
            return run_menu()
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
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        raise SystemExit(130)

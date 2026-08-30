"""Delad LLM-konfiguration med stöd för flera namngivna profiler.

Lagras i ``generated/llm_config.json`` som
``{"profiles": {namn: {backend_name, provider, model, base_url, api_key_env}},
"default": namn}``.
``load()``/``save()`` behålls bakåtkompatibla mot default-profilen så befintliga
konsumenter (llm_correct, extract_entities m.fl.) fortsätter fungera oförändrat.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "generated" / "llm_config.json"

_DEFAULTS: dict = {
    "backend_name": "Claude",
    "provider": "claude",
    "model": "claude-opus-4-8",
    "base_url": "",
}

DEFAULT_PROFILE_NAME = "Standard"


def resolve_runtime_profile(
    profile: Mapping[str, object],
    catalog: Mapping[str, Mapping[str, object]],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Slå ihop sparad profil med backend-defaults och lös hemlighet via env.

    Bara miljövariabelns namn lagras i profilen. Kända backends fortsätter
    använda sin katalogdefinierade ``env`` när profilen saknar ``api_key_env``.
    """
    env = os.environ if environ is None else environ
    fallback_name = next(iter(catalog))
    requested_name = str(profile.get("backend_name") or fallback_name)
    backend_name = requested_name if requested_name in catalog else fallback_name
    backend = catalog[backend_name]
    api_key_env = str(profile.get("api_key_env") or backend.get("env") or "").strip()
    return {
        **backend,
        "backend_name": backend_name,
        "kind": str(backend.get("kind") or profile.get("provider") or ""),
        "model": str(profile.get("model") or backend.get("model") or ""),
        "base_url": str(profile.get("base_url") or backend.get("base_url") or ""),
        "api_key_env": api_key_env,
        "api_key": env.get(api_key_env, "") if api_key_env else "",
    }


def profile_cache_key(name: str, profile: Mapping[str, object]) -> tuple[str, str, str, str]:
    """Stabil identitet för resultat som beror på vald LLM-profil."""
    return (
        name,
        str(profile.get("backend_name") or ""),
        str(profile.get("model") or ""),
        str(profile.get("base_url") or ""),
    )


def load_all() -> dict:
    """Returnera ``{"profiles": {namn: profil}, "default": namn}``."""
    profiles: dict[str, dict] = {}
    default = DEFAULT_PROFILE_NAME

    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and isinstance(stored.get("profiles"), dict) and stored["profiles"]:
                for name, profile in stored["profiles"].items():
                    profiles[name] = {**_DEFAULTS, **profile}
                default = stored.get("default", next(iter(profiles)))
            elif isinstance(stored, dict):
                # Migrera gammalt en-profil-format.
                profiles[DEFAULT_PROFILE_NAME] = {**_DEFAULTS, **stored}
        except (OSError, ValueError):
            pass

    if not profiles:
        profiles[DEFAULT_PROFILE_NAME] = dict(_DEFAULTS)

    if default not in profiles:
        default = next(iter(profiles))

    return {"profiles": profiles, "default": default}


def load() -> dict:
    """Returnera default-profilens config (bakåtkompatibel med gammalt format)."""
    all_cfg = load_all()
    cfg: dict = all_cfg["profiles"].get(all_cfg["default"], dict(_DEFAULTS))
    return cfg


def load_profile(name: str | None = None) -> dict:
    """Returnera en namngiven profil, eller default-profilen om ``name`` är None."""
    all_cfg = load_all()
    name = name or all_cfg["default"]
    profile: dict = all_cfg["profiles"].get(name, dict(_DEFAULTS))
    return profile


def save(cfg: dict) -> None:
    """Spara ``cfg`` som default-profilens config (bakåtkompatibel)."""
    all_cfg = load_all()
    all_cfg["profiles"][all_cfg["default"]] = cfg
    _write(all_cfg)


def save_profiles(profiles: dict[str, dict], default: str) -> None:
    """Spara hela profilkatalogen och default-namnet."""
    _write({"profiles": profiles, "default": default})


def _write(all_cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(all_cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )

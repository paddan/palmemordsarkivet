"""LLM-räknare: token- och kostnadsbokföring per profil."""

from __future__ import annotations

import sqlite3

from ._schema import now

# --- LLM-räknare (token och kostnad per profil) ------------------------

_LLM_USAGE_COLUMNS = (
    "profile", "calls", "input_tokens", "output_tokens",
    "cache_hit_tokens", "cost_usd", "cost_partial", "last_model", "updated_at",
)


def get_llm_usage(conn: sqlite3.Connection, profile: str) -> dict:
    """Ackumulerade token och kostnader för en LLM-profil.

    Saknad rad ger nollställda värden, så anroparen slipper skilja på "aldrig
    använd" och "inga anrop än"."""
    row = conn.execute(
        f"SELECT {', '.join(_LLM_USAGE_COLUMNS)} FROM llm_usage WHERE profile=?",
        (profile,),
    ).fetchone()
    if row is None:
        return {
            "profile": profile, "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_hit_tokens": 0, "cost_usd": 0.0, "cost_partial": 0,
            "last_model": "", "updated_at": "",
        }
    return dict(row)


def add_llm_usage(
    conn: sqlite3.Connection,
    *,
    profile: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int = 0,
    cost: float | None = None,
) -> None:
    """Lägg ett avslutat LLM-anrop till profilens räknare.

    ``cost=None`` betyder att priset saknades för anropet: token räknas ändå,
    och raden märks som ofullständig (``cost_partial``) eftersom summan då bara
    är en undre gräns."""
    name = profile.strip()
    if not name:
        raise ValueError("profile får inte vara tom")
    conn.execute(
        """
        INSERT INTO llm_usage(
            profile, calls, input_tokens, output_tokens, cache_hit_tokens,
            cost_usd, cost_partial, last_model, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile) DO UPDATE SET
            calls            = calls + excluded.calls,
            input_tokens     = input_tokens + excluded.input_tokens,
            output_tokens    = output_tokens + excluded.output_tokens,
            cache_hit_tokens = cache_hit_tokens + excluded.cache_hit_tokens,
            cost_usd         = cost_usd + excluded.cost_usd,
            cost_partial     = MAX(cost_partial, excluded.cost_partial),
            last_model       = excluded.last_model,
            updated_at       = excluded.updated_at
        """,
        (
            name,
            1,
            int(input_tokens),
            int(output_tokens),
            int(cache_hit_tokens),
            float(cost or 0.0),
            0 if cost is not None else 1,
            model,
            now(),
        ),
    )
    conn.commit()


def rename_llm_usage(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Flytta räknaren när en profil byter namn.

    En eventuell rad för det nya namnet tas bort först, så att ett återanvänt
    namn inte blandar ihop två profilers historik."""
    if old == new or not new.strip():
        return
    conn.execute("DELETE FROM llm_usage WHERE profile=?", (new.strip(),))
    conn.execute(
        "UPDATE llm_usage SET profile=?, updated_at=? WHERE profile=?",
        (new.strip(), now(), old),
    )
    conn.commit()


def delete_llm_usage(conn: sqlite3.Connection, profile: str) -> bool:
    """Radera räknaren för en borttagen profil. True om något togs bort."""
    cur = conn.execute("DELETE FROM llm_usage WHERE profile=?", (profile,))
    conn.commit()
    return cur.rowcount > 0

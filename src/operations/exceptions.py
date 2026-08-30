"""Undantag som delas av operationslagret."""

from __future__ import annotations


class OperationCancelled(Exception):
    """Operationen avbröts kontrollerat (Ctrl-C eller användarbegärd cancel)."""


class OperationFailed(Exception):
    """Operationen misslyckades med ett användbart felmeddelande."""


def ensure_successful_result(result: int | None, operation_label: str) -> None:
    """Översätt domänfunktioners icke-nollkod till operationslagrets feltyp."""
    if isinstance(result, int) and result != 0:
        raise OperationFailed(f"{operation_label} misslyckades med exitkod {result}")

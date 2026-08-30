"""Argumentparsning och förgrundskörning för registrerade operationer."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .models import OperationDefinition
from .registry import get_registry


def build_operation_parser(definition: OperationDefinition) -> argparse.ArgumentParser:
    """Bygg en parser från operationens gemensamma parameterdefinitioner."""
    parser = argparse.ArgumentParser(description=definition.description)
    for parameter in definition.parameters:
        parameter.add_to_parser(parser)
    return parser


def parse_operation_args(definition: OperationDefinition, argv: Sequence[str]) -> dict[str, object]:
    """Parsa CLI-argument till normaliserade parametervärden."""
    args = build_operation_parser(definition).parse_args(argv)
    params = vars(args)
    if definition.validate is not None:
        definition.validate(params)
    return params


def run_operation_cli(operation_id: str, argv: Sequence[str]) -> int:
    """Kör en registrerad operation i förgrunden och returnera dess exitkod."""
    try:
        definition = get_registry().get(operation_id)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        params = parse_operation_args(definition, argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    from .context import OperationContext, TerminalSink
    from .exceptions import OperationCancelled, OperationFailed, ensure_successful_result

    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)
    try:
        result = definition.run(context, params)
        ensure_successful_result(result, definition.label)
    except KeyboardInterrupt:
        return 130
    except OperationCancelled as exc:
        print(exc, file=sys.stderr)
        return 130
    except OperationFailed as exc:
        # Snyggt felmeddelande i stället för traceback; icke-noll exitkod.
        print(exc, file=sys.stderr)
        return 1
    return 0

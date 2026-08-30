"""Datamodeller för operationer och deras parametrar."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .context import OperationContext

ParameterKind = Literal["bool", "int", "float", "str", "path", "choice"]


@dataclass(frozen=True)
class ParameterDefinition:
    """En parameter som delas av CLI och administrationsgränssnittet."""

    name: str
    flags: tuple[str, ...]
    kind: ParameterKind
    default: object
    help: str
    choices: tuple[str, ...] = ()
    required: bool = False
    secret: bool = False

    def add_to_parser(self, parser: argparse.ArgumentParser) -> None:
        """Lägg till parametern i en argparse-parser."""
        kwargs: dict[str, Any] = {
            "dest": self.name,
            "default": self._normalized_default(),
            "help": self.help,
            "required": self.required,
        }
        if self.kind == "bool":
            kwargs["action"] = "store_true" if self.default is False else "store_false"
        elif self.kind == "int":
            kwargs["type"] = int
        elif self.kind == "float":
            kwargs["type"] = float
        elif self.kind == "str":
            kwargs["type"] = str
        elif self.kind == "path":
            kwargs["type"] = Path
        elif self.kind == "choice":
            if not self.choices:
                raise ValueError(f"Valparametern {self.name!r} saknar tillåtna värden")
            kwargs["choices"] = self.choices
            kwargs["type"] = str
        else:
            raise ValueError(f"Okänd parametertyp: {self.kind!r}")
        parser.add_argument(*self.flags, **kwargs)

    def normalize_value(self, value: object) -> object:
        """Normalisera ett parametervärde till samma form som CLI:t använder."""
        if value is None:
            return None
        if self.kind == "path":
            return value if isinstance(value, Path) else Path(str(value))
        if self.kind == "choice":
            if value not in self.choices:
                allowed = ", ".join(self.choices)
                raise ValueError(f"Ogiltigt värde för {self.name}: {value!r} (giltiga: {allowed})")
            return value
        if self.kind == "int":
            return value if isinstance(value, int) else int(str(value))
        if self.kind == "float":
            return value if isinstance(value, float) else float(str(value))
        if self.kind == "str":
            return str(value)
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"Parametern {self.name} måste vara sann eller falsk")
            return value
        raise ValueError(f"Okänd parametertyp: {self.kind!r}")

    def validate_background_value(self, value: object) -> object:
        """Validera ett värde innan det kan sparas för ett bakgrundsjobb."""
        if self.secret and value not in (None, ""):
            raise ValueError(
                f"Hemliga värden för {self.name} får inte sparas; använd en miljövariabel i stället."
            )
        return self.normalize_value(value)

    def _normalized_default(self) -> object:
        return self.normalize_value(self.default)


@dataclass(frozen=True)
class ProgressUpdate:
    """En strukturerad lägesuppdatering från en operation."""

    step: str
    completed: int = 0
    total: int | None = None
    message: str = ""


# Returtypen int | None rymmer både adaptrar som alltid lyckas (None) och
# run-funktioner som returnerar en exitkod. Params är Mapping[str, Any] eftersom
# registryn sprider värdena (**params) in i domänmodulernas options-dataclasses.
OperationRunner = Callable[["OperationContext", Mapping[str, Any]], int | None]
ParameterValidator = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class OperationDefinition:
    """Metadata och körbar funktion för en registrerad operation."""

    id: str
    label: str
    group: str
    description: str
    parameters: tuple[ParameterDefinition, ...]
    admin_visible: bool
    mutating: bool
    confirmation: str | None
    run: OperationRunner
    validate: ParameterValidator | None = None

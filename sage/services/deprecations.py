"""The deprecation warning a call that uses a deprecated form receives.

CAS-ADR-008 clause 8 has every caller adaptation that withdraws or changes
something callers use follow a deprecation. The published contract marks a
deprecation for whoever reads the specification; this module reaches the caller
who never does. A call that uses a deprecated operation, parameter, value or
default gets a warning in its response naming the form, its replacement and the
earliest date the adaptation may ship.

The warning is decided here, beneath both request surfaces (CAS-ADR-052), from
the request model each surface builds, so REST and MCP give the same input the
same warning. A form is judged used by its value on that model, never by which
fields a caller set: one surface passes every argument explicitly and the other
omits what the caller omitted, and both arrive at the same values.

A deprecation can be declared only for an operation whose response already
carries caller-facing warnings and whose service consults this module;
``WARNING_CARRIERS`` names them. Declaring one elsewhere first gives the
operation that field, which is itself a published capability.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel

from sage.models.schemas import DiscoverRequest, IngestRequest


class DeprecatedForm(StrEnum):
    """What a deprecation withdraws or changes."""

    OPERATION = "operation"
    PARAMETER = "parameter"
    VALUE = "value"
    DEFAULT = "default"


class DeprecationRegistryError(ValueError):
    """A declared deprecation this module cannot honour."""


@dataclass(frozen=True)
class Deprecation:
    """One deprecated form of one operation, named by its operation id."""

    operation: str
    form: DeprecatedForm
    replacement: str
    earliest_adaptation: dt.date
    parameter: str | None = None
    value: Any = None

    def subject(self) -> str:
        if self.form is DeprecatedForm.OPERATION:
            return f"the {self.operation} operation"
        if self.form is DeprecatedForm.PARAMETER:
            return f"the {self.parameter} parameter"
        if self.form is DeprecatedForm.VALUE:
            return f"the value {self.value!r} of {self.parameter}"
        return f"the default of {self.parameter}"

    def warning(self) -> str:
        return (
            f"Deprecated: {self.subject()} is deprecated; use {self.replacement} instead. "
            f"The change may ship from {self.earliest_adaptation.isoformat()}."
        )

    def used_by(self, request: BaseModel) -> bool:
        if self.form is DeprecatedForm.OPERATION:
            return True
        # validate_registry has every other form name its parameter.
        value = getattr(request, self.parameter or "", None)
        if self.form is DeprecatedForm.VALUE:
            return value == self.value
        field = type(request).model_fields.get(self.parameter or "")
        default = None if field is None else field.get_default(call_default_factory=True)
        at_default = value is None or value == default
        return at_default if self.form is DeprecatedForm.DEFAULT else not at_default


#: The operations whose response carries a caller-facing warnings list and whose
#: service consults ``warnings_for``: ``search`` in its hints, ``ingest_document``
#: in the ingest result's warnings. Each names the request model its service
#: receives, which holds every parameter a deprecation of it can name.
WARNING_CARRIERS: Final[Mapping[str, type[BaseModel]]] = {
    "search": DiscoverRequest,
    "ingest_document": IngestRequest,
}

#: Every deprecated form currently served. An entry names the operation id the
#: REST operation and the MCP tool share, and stays until the adaptation ships.
DEPRECATIONS: tuple[Deprecation, ...] = ()


def validate_registry(entries: Iterable[Deprecation]) -> None:
    """Refuse a declaration no response could carry, or one missing its subject."""
    for entry in entries:
        if entry.operation not in WARNING_CARRIERS:
            raise DeprecationRegistryError(
                f"{entry.operation!r} has no warnings field to carry a deprecation; "
                "give its response one and consult warnings_for in its service first"
            )
        if entry.form is not DeprecatedForm.OPERATION and not entry.parameter:
            raise DeprecationRegistryError(
                f"a {entry.form} deprecation of {entry.operation!r} names no parameter"
            )
        request_model = WARNING_CARRIERS[entry.operation]
        if entry.parameter and entry.parameter not in request_model.model_fields:
            raise DeprecationRegistryError(
                f"{entry.operation!r} takes no parameter {entry.parameter!r}"
            )
        if entry.form is DeprecatedForm.VALUE and entry.value is None:
            raise DeprecationRegistryError(
                f"a value deprecation of {entry.operation!r} names no value"
            )


def warnings_for(operation: str, request: BaseModel) -> list[str]:
    """The deprecation warnings a call to ``operation`` with ``request`` receives."""
    return [
        entry.warning()
        for entry in DEPRECATIONS
        if entry.operation == operation and entry.used_by(request)
    ]


validate_registry(DEPRECATIONS)

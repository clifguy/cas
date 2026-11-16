"""The deprecation warning a call that uses a deprecated form receives.

CAS-ADR-008 clause 8 has every caller adaptation that withdraws or changes
something callers use follow a deprecation. The published contract marks a
deprecation for whoever reads the specification; this module reaches the caller
who never does. A call that uses a deprecated operation, parameter, value,
value format or default gets a warning in its response naming the form, its replacement and the
earliest date the adaptation may ship. So does a call whose response carries a
deprecated response field.

A response field is judged by the response, not the request: whether a row
shape appears can turn on the server's own choice (an inline-budget degrade),
so the service passes the component names its final response carries to
``response_field_warnings``. ``warnings_for`` judges the request alone and never
returns a response-field warning. ``RESPONSE_FIELD_COMPONENTS`` names the
components each service reports, and so the only ones such a deprecation can
name.

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
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel

from sage.models.schemas import (
    LIGHT_ROW_COVERAGE_ADAPTATION,
    BulkMetadataItem,
    DiscoverRequest,
    DocumentSummaryLight,
    IngestRequest,
)


class DeprecatedForm(StrEnum):
    """What a deprecation withdraws or changes."""

    OPERATION = "operation"
    PARAMETER = "parameter"
    VALUE = "value"
    FORMAT = "format"
    DEFAULT = "default"
    RESPONSE_FIELD = "response_field"


_TIMESTAMP_PREFIX: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def is_timestamp(value: Any) -> bool:
    """Whether ``value`` is an ISO 8601 timestamp string."""
    if not isinstance(value, str) or not _TIMESTAMP_PREFIX.match(value):
        return False
    try:
        dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


#: The value formats a ``FORMAT`` deprecation can name, each with the test that
#: tells a value of that format from any other value of its parameter.
VALUE_FORMATS: Final[Mapping[str, Callable[[Any], bool]]] = {
    "timestamp": is_timestamp,
}


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
    field: str | None = None

    def subject(self) -> str:
        if self.form is DeprecatedForm.OPERATION:
            return f"the {self.operation} operation"
        if self.form is DeprecatedForm.PARAMETER:
            return f"the {self.parameter} parameter"
        if self.form is DeprecatedForm.VALUE:
            return f"the value {self.value!r} of {self.parameter}"
        if self.form is DeprecatedForm.FORMAT:
            return f"a {self.value} value of {self.parameter}"
        if self.form is DeprecatedForm.RESPONSE_FIELD:
            return f"the {self.field} response field"
        return f"the default of {self.parameter}"

    def carried_by(self, components: set[str]) -> bool:
        """Whether a response carrying ``components`` carries this response field."""
        # validate_registry has a response-field entry name "Component.field".
        return self.form is DeprecatedForm.RESPONSE_FIELD and (
            (self.field or "").partition(".")[0] in components
        )

    def warning(self) -> str:
        return (
            f"Deprecated: {self.subject()} is deprecated; use {self.replacement} instead. "
            f"The change may ship from {self.earliest_adaptation.isoformat()}."
        )

    def used_by(self, request: BaseModel) -> bool:
        if self.form is DeprecatedForm.RESPONSE_FIELD:
            return False
        if self.form is DeprecatedForm.OPERATION:
            return True
        # validate_registry has every other form name its parameter.
        value = getattr(request, self.parameter or "", None)
        if self.form is DeprecatedForm.VALUE:
            return value == self.value
        if self.form is DeprecatedForm.FORMAT:
            return value is not None and VALUE_FORMATS[self.value](value)
        field = type(request).model_fields.get(self.parameter or "")
        default = None if field is None else field.get_default(call_default_factory=True)
        at_default = value is None or value == default
        return at_default if self.form is DeprecatedForm.DEFAULT else not at_default


#: The operations whose response carries a caller-facing warnings list and whose
#: service consults ``warnings_for``: ``search`` in its hints, ``ingest_document``
#: in the ingest result's warnings, ``update_metadata`` in each item result's
#: warnings, ``list_pending_metadata`` in its page's warnings. Each names the
#: request model its service judges, which holds every parameter a deprecation
#: of it can name; ``update_metadata`` is judged item by item, so a warning lands
#: on the item that used the deprecated form. An operation that carries only
#: response-field warnings judges no request and names none.
WARNING_CARRIERS: Final[Mapping[str, type[BaseModel] | None]] = {
    "search": DiscoverRequest,
    "ingest_document": IngestRequest,
    "update_metadata": BulkMetadataItem,
    "list_pending_metadata": None,
}

#: The response components each operation's service reports to
#: ``response_field_warnings``, and so the only components a response-field
#: deprecation of that operation can name.
RESPONSE_FIELD_COMPONENTS: Final[Mapping[str, tuple[type[BaseModel], ...]]] = {
    "search": (DocumentSummaryLight,),
    "list_pending_metadata": (DocumentSummaryLight,),
}

#: The date from which an ``updated_at`` concurrency token may stop being
#: accepted (CAS-ADR-038, CAS-ADR-008 clause 8).
TIMESTAMP_TOKEN_ADAPTATION: Final[dt.date] = dt.date(2026, 12, 1)

#: Every deprecated form currently served. An entry names the operation id the
#: REST operation and the MCP tool share, and stays until the adaptation ships.
DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        operation="ingest_document",
        form=DeprecatedForm.FORMAT,
        parameter="expected_head_version",
        value="timestamp",
        replacement="the predecessor's version_token",
        earliest_adaptation=TIMESTAMP_TOKEN_ADAPTATION,
    ),
    Deprecation(
        operation="update_metadata",
        form=DeprecatedForm.FORMAT,
        parameter="expected_version",
        value="timestamp",
        replacement="the document's version_token",
        earliest_adaptation=TIMESTAMP_TOKEN_ADAPTATION,
    ),
    *(
        Deprecation(
            operation=operation,
            form=DeprecatedForm.RESPONSE_FIELD,
            field="DocumentSummaryLight.processing_coverage",
            replacement=(
                'get_document (or response_mode="full" on a smaller page) for the full object'
            ),
            earliest_adaptation=LIGHT_ROW_COVERAGE_ADAPTATION,
        )
        for operation in ("search", "list_pending_metadata")
    ),
)


def validate_registry(entries: Iterable[Deprecation]) -> None:
    """Refuse a declaration no response could carry, or one missing its subject."""
    for entry in entries:
        if entry.operation not in WARNING_CARRIERS:
            raise DeprecationRegistryError(
                f"{entry.operation!r} has no warnings field to carry a deprecation; "
                "give its response one and consult warnings_for (request forms) or "
                "response_field_warnings (response fields) in its service first"
            )
        if entry.form is DeprecatedForm.RESPONSE_FIELD:
            component, _, name = (entry.field or "").partition(".")
            if not component or not name:
                raise DeprecationRegistryError(
                    f"a response-field deprecation of {entry.operation!r} names no response "
                    "field as Component.field"
                )
            reported = {m.__name__: m for m in RESPONSE_FIELD_COMPONENTS.get(entry.operation, ())}
            model = reported.get(component)
            if model is None:
                raise DeprecationRegistryError(
                    f"{entry.operation!r} reports no {component} component to "
                    "response_field_warnings"
                )
            if name not in model.model_fields:
                raise DeprecationRegistryError(f"{component} has no field {name!r}")
            continue
        if entry.form is not DeprecatedForm.OPERATION and not entry.parameter:
            raise DeprecationRegistryError(
                f"a {entry.form} deprecation of {entry.operation!r} names no parameter"
            )
        request_model = WARNING_CARRIERS[entry.operation]
        if request_model is None:
            raise DeprecationRegistryError(
                f"{entry.operation!r} judges no request and carries only response-field warnings"
            )
        if entry.parameter and entry.parameter not in request_model.model_fields:
            raise DeprecationRegistryError(
                f"{entry.operation!r} takes no parameter {entry.parameter!r}"
            )
        if entry.form is DeprecatedForm.VALUE and entry.value is None:
            raise DeprecationRegistryError(
                f"a value deprecation of {entry.operation!r} names no value"
            )
        if entry.form is DeprecatedForm.FORMAT and entry.value not in VALUE_FORMATS:
            raise DeprecationRegistryError(
                f"a format deprecation of {entry.operation!r} names no known format: "
                f"{entry.value!r}"
            )


def warnings_for(operation: str, request: BaseModel) -> list[str]:
    """The deprecation warnings a call to ``operation`` with ``request`` receives."""
    return [
        entry.warning()
        for entry in DEPRECATIONS
        if entry.operation == operation and entry.used_by(request)
    ]


def response_field_warnings(operation: str, carried: set[str]) -> list[str]:
    """The deprecation warnings a response of ``operation`` carrying ``carried`` receives.

    ``carried`` names the components the final response holds, such as
    ``DocumentSummaryLight`` for a page of light rows.
    """
    return [
        entry.warning()
        for entry in DEPRECATIONS
        if entry.operation == operation and entry.carried_by(carried)
    ]


validate_registry(DEPRECATIONS)

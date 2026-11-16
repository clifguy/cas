"""A call that uses a deprecated form carries a warning, the same on REST and MCP.

CAS-ADR-008 clause 8 has a deprecation reach the caller who never reads the
specification: the response to a call using a deprecated operation, parameter,
value or default carries a warning naming the form, its replacement and the
earliest date the adaptation may ship. The warning is decided beneath both
request surfaces (CAS-ADR-052) from one declaration, so the two surfaces cannot
disagree about it.

Anti-coincidental-pass discipline:

* Every surface test installs its own declaration in place of the shipped
  registry; a surface that emitted nothing, or emitted for every call, fails
  the paired control that omits the deprecated form.
* Both surfaces' warnings are compared with each other and with a hand-written
  expected string, so the two agreeing on nothing, or on the same wrong text,
  fails.
* The MCP ingest tool passes ``None`` for an omitted ``created_by`` where REST
  omits the field, and the parameter case sets a field explicitly to its
  default. Detection by which fields were set, rather than by value, warns on
  the MCP arm of the ingest control and on the explicit default, and fails both.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml

from sage._tool_naming import SERVER_ASSIGNMENT
from sage.config import VaultConfig
from sage.models.schemas import BulkMetadataItem, DiscoverRequest, IngestRequest
from sage.services import deprecations
from sage.services.deprecations import (
    WARNING_CARRIERS,
    DeprecatedForm,
    Deprecation,
    DeprecationRegistryError,
    response_field_warnings,
    validate_registry,
    warnings_for,
)
from tests.helpers.write_attribution import VAULT, client, mcp_call, mcp_running, running_app, seed

EARLIEST = dt.date(2027, 1, 15)

MODE_DEFAULT = Deprecation(
    operation="search",
    form=DeprecatedForm.DEFAULT,
    parameter="mode",
    replacement="an explicit mode",
    earliest_adaptation=EARLIEST,
)
MODE_DEFAULT_WARNING = (
    "Deprecated: the default of mode is deprecated; use an explicit mode instead. "
    "The change may ship from 2027-01-15."
)
CREATED_BY = Deprecation(
    operation="ingest_document",
    form=DeprecatedForm.PARAMETER,
    parameter="created_by",
    replacement="the authenticated principal",
    earliest_adaptation=EARLIEST,
)
CREATED_BY_WARNING = (
    "Deprecated: the created_by parameter is deprecated; use the authenticated principal "
    "instead. The change may ship from 2027-01-15."
)

CORE_SPEC = Path(__file__).parents[2] / "docs/fs/sage/sage_core_api.openapi.yaml"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_the_shipped_registry_is_valid_and_holds_its_declared_forms() -> None:
    validate_registry(deprecations.DEPRECATIONS)
    assert {
        (d.operation, d.form, d.parameter, d.value, d.field) for d in deprecations.DEPRECATIONS
    } == {
        ("ingest_document", DeprecatedForm.FORMAT, "expected_head_version", "timestamp", None),
        ("update_metadata", DeprecatedForm.FORMAT, "expected_version", "timestamp", None),
        (
            "search",
            DeprecatedForm.RESPONSE_FIELD,
            None,
            None,
            "DocumentSummaryLight.processing_coverage",
        ),
        (
            "list_pending_metadata",
            DeprecatedForm.RESPONSE_FIELD,
            None,
            None,
            "DocumentSummaryLight.processing_coverage",
        ),
        *(
            (
                operation,
                DeprecatedForm.RESPONSE_FIELD,
                None,
                None,
                "ProcessingIdentity.configuration",
            )
            for operation in deprecations.COVERAGE_OPERATIONS
        ),
    }


def test_every_warning_carrier_is_a_published_operation() -> None:
    spec = yaml.safe_load(CORE_SPEC.read_text())
    rest_operations = {
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert set(WARNING_CARRIERS) <= set(SERVER_ASSIGNMENT) | rest_operations
    assert {"search", "ingest_document", "update_metadata", "list_pending_metadata"} <= set(
        WARNING_CARRIERS
    )


LIGHT_COVERAGE = Deprecation(
    operation="search",
    form=DeprecatedForm.RESPONSE_FIELD,
    field="DocumentSummaryLight.processing_coverage",
    replacement="coverage_summary",
    earliest_adaptation=EARLIEST,
)
LIGHT_COVERAGE_WARNING = (
    "Deprecated: the DocumentSummaryLight.processing_coverage response field is deprecated; "
    "use coverage_summary instead. The change may ship from 2027-01-15."
)


def test_a_response_field_deprecation_warns_only_when_the_response_carries_it(registry) -> None:
    """Judged on the components the response carries, never on the request."""
    registry(LIGHT_COVERAGE)

    assert response_field_warnings("search", {"DocumentSummaryLight"}) == [LIGHT_COVERAGE_WARNING]
    assert response_field_warnings("search", {"DocumentSummary"}) == []
    assert response_field_warnings("search", set()) == []
    assert response_field_warnings("list_pending_metadata", {"DocumentSummaryLight"}) == []
    # A request, whatever it asks for, never selects a response-field warning.
    assert warnings_for("search", DiscoverRequest(mode="catalog", response_mode="light")) == []


TOKEN_FORMAT = Deprecation(
    operation="update_metadata",
    form=DeprecatedForm.FORMAT,
    parameter="expected_version",
    value="timestamp",
    replacement="the document's version_token",
    earliest_adaptation=EARLIEST,
)


def test_a_format_deprecation_warns_only_on_a_value_of_that_format(registry) -> None:
    """A timestamp warns; a version token, a non-timestamp string and omission do not."""
    registry(TOKEN_FORMAT)

    assert warnings_for(
        "update_metadata",
        BulkMetadataItem(
            document_id="0123abcd_doc", expected_version="2026-10-10T16:44:01.489319Z"
        ),
    ) == [
        "Deprecated: a timestamp value of expected_version is deprecated; use the "
        "document's version_token instead. The change may ship from 2027-01-15."
    ]
    for token in ("7", "2026-10-10", "STALE_VERSION", "2026-13-40T00:00:00Z"):
        item = BulkMetadataItem(document_id="0123abcd_doc", expected_version=token)
        assert warnings_for("update_metadata", item) == [], token
    assert warnings_for("update_metadata", BulkMetadataItem(document_id="0123abcd_doc")) == []


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (
            Deprecation("chain", DeprecatedForm.OPERATION, "traverse", EARLIEST),
            "no warnings field",
        ),
        (Deprecation("no_such_tool", DeprecatedForm.OPERATION, "x", EARLIEST), "no warnings field"),
        (Deprecation("search", DeprecatedForm.PARAMETER, "x", EARLIEST), "names no parameter"),
        (
            Deprecation("search", DeprecatedForm.VALUE, "x", EARLIEST, parameter="mode"),
            "names no value",
        ),
        (
            Deprecation("search", DeprecatedForm.DEFAULT, "x", EARLIEST, parameter="mdoe"),
            "takes no parameter 'mdoe'",
        ),
        (
            Deprecation(
                "update_metadata",
                DeprecatedForm.FORMAT,
                "x",
                EARLIEST,
                parameter="expected_version",
                value="epoch",
            ),
            "names no known format",
        ),
        (
            Deprecation(
                "update_metadata",
                DeprecatedForm.FORMAT,
                "x",
                EARLIEST,
                parameter="expected_head_version",
                value="timestamp",
            ),
            "takes no parameter 'expected_head_version'",
        ),
        (
            Deprecation("search", DeprecatedForm.RESPONSE_FIELD, "x", EARLIEST),
            "names no response field",
        ),
        (
            Deprecation(
                "search",
                DeprecatedForm.RESPONSE_FIELD,
                "x",
                EARLIEST,
                field="processing_coverage",
            ),
            "names no response field",
        ),
        (
            Deprecation(
                "chain",
                DeprecatedForm.RESPONSE_FIELD,
                "x",
                EARLIEST,
                field="Document.processing_coverage",
            ),
            "no warnings field",
        ),
        (
            Deprecation(
                "search",
                DeprecatedForm.RESPONSE_FIELD,
                "x",
                EARLIEST,
                field="DocumentSumaryLight.processing_coverage",
            ),
            "reports no DocumentSumaryLight component",
        ),
        (
            Deprecation(
                "search",
                DeprecatedForm.RESPONSE_FIELD,
                "x",
                EARLIEST,
                field="DocumentSummaryLight.coverage_sumary",
            ),
            "has no field 'coverage_sumary'",
        ),
        (
            Deprecation(
                "ingest_document",
                DeprecatedForm.RESPONSE_FIELD,
                "x",
                EARLIEST,
                field="DocumentSummaryLight.processing_coverage",
            ),
            "'ingest_document' reports no DocumentSummaryLight component",
        ),
        (
            Deprecation(
                "list_pending_metadata",
                DeprecatedForm.FORMAT,
                "x",
                EARLIEST,
                parameter="offset",
                value="timestamp",
            ),
            "judges no request",
        ),
    ],
    ids=[
        "carrier-less-operation",
        "unknown-operation",
        "parameter-unnamed",
        "value-unnamed",
        "parameter-misspelled",
        "format-unknown",
        "format-parameter-on-the-wrong-operation",
        "response-field-unnamed",
        "response-field-without-component",
        "response-field-on-carrier-less-operation",
        "response-field-component-misspelled",
        "response-field-name-misspelled",
        "response-field-on-an-operation-reporting-no-component",
        "request-form-on-an-operation-judging-no-request",
    ],
)
def test_the_registry_refuses_a_declaration_it_cannot_honour(
    entry: Deprecation, reason: str
) -> None:
    with pytest.raises(DeprecationRegistryError, match=reason):
        validate_registry((entry,))


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    def install(*entries: Deprecation) -> None:
        validate_registry(entries)
        monkeypatch.setattr(deprecations, "DEPRECATIONS", entries)

    return install


def test_an_operation_deprecation_warns_on_every_call(registry) -> None:
    registry(Deprecation("search", DeprecatedForm.OPERATION, "find", EARLIEST))

    assert warnings_for("search", DiscoverRequest(query="q")) == [
        "Deprecated: the search operation is deprecated; use find instead. "
        "The change may ship from 2027-01-15."
    ]
    assert warnings_for("ingest_document", IngestRequest(source="a.md")) == []


def test_a_parameter_deprecation_warns_only_when_the_parameter_is_used(registry) -> None:
    registry(
        Deprecation(
            "search",
            DeprecatedForm.PARAMETER,
            "response_mode",
            EARLIEST,
            parameter="include_abstracts",
        )
    )

    assert warnings_for("search", DiscoverRequest(query="q", include_abstracts=True))
    assert warnings_for("search", DiscoverRequest(query="q")) == []
    # A surface that passes every argument explicitly sends the default: unused.
    assert warnings_for("search", DiscoverRequest(query="q", include_abstracts=False)) == []


def test_a_value_deprecation_warns_only_for_that_value(registry) -> None:
    registry(
        Deprecation(
            "search",
            DeprecatedForm.VALUE,
            "semantic",
            EARLIEST,
            parameter="mode",
            value="keyword",
        )
    )

    (warning,) = warnings_for("search", DiscoverRequest(query="q", mode="keyword"))
    assert warning.startswith("Deprecated: the value 'keyword' of mode is deprecated")
    assert warnings_for("search", DiscoverRequest(query="q", mode="semantic")) == []


def test_a_default_deprecation_warns_whether_the_default_is_omitted_or_null(registry) -> None:
    registry(MODE_DEFAULT)

    assert warnings_for("search", DiscoverRequest(query="q")) == [MODE_DEFAULT_WARNING]
    assert warnings_for("search", DiscoverRequest(query="q", mode=None)) == [MODE_DEFAULT_WARNING]
    assert warnings_for("search", DiscoverRequest(query="q", mode="keyword")) == []


# ---------------------------------------------------------------------------
# Both surfaces
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(minimal_vault_config_dict) -> AsyncIterator[object]:
    async with running_app(VaultConfig.model_validate(minimal_vault_config_dict), None) as app:
        yield app


async def _rest(app, path: str, body: dict) -> dict:
    async with client(app) as c:
        resp = await c.post(f"/sage_vaults/{VAULT}{path}", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def test_search_carries_the_same_warning_on_both_surfaces(app, registry) -> None:
    registry(MODE_DEFAULT)

    async with mcp_running(app):
        rest = await _rest(app, "/discover", {"query": "alpha"})
        mcp = await mcp_call(app, None, "search", {"vault_id": VAULT, "query": "alpha"})
        rest_explicit = await _rest(app, "/discover", {"query": "alpha", "mode": "keyword"})
        mcp_explicit = await mcp_call(
            app, None, "search", {"vault_id": VAULT, "query": "alpha", "mode": "keyword"}
        )

    rest_warnings = (rest.get("hints") or {}).get("warnings") or []
    mcp_warnings = (mcp.get("hints") or {}).get("warnings") or []
    assert MODE_DEFAULT_WARNING in rest_warnings
    assert [w for w in rest_warnings if w.startswith("Deprecated:")] == [
        w for w in mcp_warnings if w.startswith("Deprecated:")
    ]
    for response in (rest_explicit, mcp_explicit):
        warnings = (response.get("hints") or {}).get("warnings") or []
        assert not [w for w in warnings if w.startswith("Deprecated:")], warnings


async def test_a_deprecation_joins_the_search_warnings_it_finds_there(app, registry) -> None:
    """A keyword query of several terms matching nothing already carries an advisory."""
    registry(
        Deprecation(
            "search", DeprecatedForm.VALUE, "semantic", EARLIEST, parameter="mode", value="keyword"
        )
    )

    rest = await _rest(app, "/discover", {"query": "zzqx yyqw", "mode": "keyword"})

    warnings = rest["hints"]["warnings"]
    assert warnings[-1].startswith("Deprecated: the value 'keyword' of mode")
    assert len(warnings) >= 2, "the existing advisory was overwritten, not joined"


async def test_ingest_carries_the_same_warning_on_both_surfaces(
    app, registry, tmp_vault_dir
) -> None:
    registry(CREATED_BY)

    async with mcp_running(app):
        rest = await _rest(
            app,
            "/documents",
            {"source": seed(tmp_vault_dir, "r.md"), "source_type": "markdown", "created_by": "c"},
        )
        mcp = await mcp_call(
            app,
            None,
            "ingest_document",
            {
                "vault_id": VAULT,
                "source": seed(tmp_vault_dir, "m.md"),
                "source_type": "markdown",
                "created_by": "c",
            },
        )
        plain = await mcp_call(
            app,
            None,
            "ingest_document",
            {"vault_id": VAULT, "source": seed(tmp_vault_dir, "p.md"), "source_type": "markdown"},
        )

    assert rest["warnings"] == [CREATED_BY_WARNING]
    assert mcp["warnings"] == [CREATED_BY_WARNING]
    assert "warnings" not in plain

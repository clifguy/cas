"""The per-file error-code vocabulary a batch ingest reports is derived and gated.

A batch ingest catches each file's failure and reports it inside the success
summary as a ``BatchIngestFileError`` whose ``code`` is the code the
single-document ingest raised. The contract lists the codes that may appear
there, and this module holds that list to a derivation rather than to a second
hand-kept copy:

    vocabulary == declared(ingest_document) - UNREACHABLE_PER_FILE - BATCH_BOUNDARY

Two further gates make the derivation sound rather than merely consistent.

- **The ingest operation declares what its path raises.** A code the ingest
  service raises but the operation never declares would otherwise be missing
  from both lists at once, and the equality above would stay green over it.
  The ingest path is walked from ``IngestionService.ingest`` through the
  methods and module functions it calls, and every error it constructs must
  have its code declared on the operation.
- **An exclusion stays true.** Each code left out of the vocabulary is keyed to
  the request fields it depends on, and the batch's single request
  construction must not pass any of them. A batch that starts passing one
  makes the exclusion false, and the gate names the codes that became
  reachable.

Declarations are read from the committed core specification, which the router
is held to response for response by the OpenAPI conformance gate.

Limits of the walk, stated so a green run is not read as more than it is: it
follows calls within the ingestion module only, so a code raised in another
module on the path is not seen -- including a typed-alias refusal raised while
the request model validates; it does not see an error re-raised through a
variable, which is how the vault-source store refusals arrive (they are typed
at the binding and declared as their own statuses); and it skips an error
whose code is a constructor argument it cannot resolve to a literal.

Limits of the derivation: a code counts as declared wherever an error
response's description names it, so a passing mention inside another code's
paragraph declares it too; and an exclusion is held only to the batch not
passing its fields -- that the code cannot arise without them is read from the
service, not checked here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from sage.api.errors import _TYPED_ALIAS_CODES
from sage.models.schemas import IngestRequest
from tests.sage.test_mcp_tool_conformance import _all_registered_tools

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ERRORS_MODULE = _REPO_ROOT / "sage" / "api" / "errors.py"
_INGESTION_MODULE = _REPO_ROOT / "sage" / "services" / "ingestion.py"
_BATCH_MODULE = _REPO_ROOT / "sage" / "services" / "batch_ingest.py"
_SPECS = {
    "sage_core": _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml",
    "cas_app": _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml",
}

_INGEST_OPERATION = "ingest_document"
#: Each batch operation with the spec that carries it.
_BATCH_OPERATIONS = (("sage_core", "batch_ingest_documents"), ("cas_app", "bulk_ingest_document"))
_BACKTICKED = re.compile(r"`{1,2}([a-z][a-z0-9_]*)`{1,2}")

#: Codes the single-document ingest declares that no batch file can raise,
#: keyed to the request fields the code depends on. A batch builds each file's
#: request without any of them, which ``test_excluded_codes_stay_unreachable``
#: holds true.
UNREACHABLE_PER_FILE: dict[str, tuple[str, ...]] = {
    "document_not_found": ("predecessor_id",),
    "supersede_target_not_active": ("predecessor_id",),
    "identical_content_supersede": ("predecessor_id",),
    "invalid_document_id": ("predecessor_id", "document_id"),
    "stale_chain_head": ("expected_head_version",),
    "expected_head_version_requires_predecessor": ("expected_head_version",),
    "force_reingest_path_mismatch": ("force",),
    "force_reingest_pin_mismatch": ("force", "document_id"),
    "relocated_from_provenance_mismatch": ("relocated_from",),
    "relocation_source_undelivered": ("relocated_from",),
    "invalid_sha256": ("relocated_from",),
}

#: Codes the single-document ingest declares that a batch refuses once, for the
#: whole call, before any file is attempted -- so they surface as the batch
#: operation's own status and never as a per-file entry.
BATCH_BOUNDARY: frozenset[str] = frozenset({"invalid_vault_id"})


# ---------------------------------------------------------------------------
# Error classes and their codes
# ---------------------------------------------------------------------------


def _error_classes() -> list[ast.ClassDef]:
    tree = ast.parse(_ERRORS_MODULE.read_text(encoding="utf-8"))
    return [node for node in tree.body if isinstance(node, ast.ClassDef)]


def _init_of(cls: ast.ClassDef) -> ast.FunctionDef | None:
    return next(
        (s for s in cls.body if isinstance(s, ast.FunctionDef) and s.name == "__init__"),
        None,
    )


def _super_code_arg(init: ast.FunctionDef) -> ast.expr | None:
    """The code argument an ``__init__`` passes to ``super().__init__``, if any."""
    for node in ast.walk(init):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__init__"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
            and node.args
        ):
            return node.args[0]
    return None


def _raised_code_by_class() -> dict[str, str]:
    """Error class name to the literal code it raises, inheritance resolved.

    A class without an ``__init__`` of its own raises its base's code. A class
    whose ``__init__`` passes anything but a literal has no single code and is
    left out.
    """
    own: dict[str, str | None] = {}
    bases: dict[str, list[str]] = {}
    parametric: set[str] = set()
    for cls in _error_classes():
        bases[cls.name] = [b.id for b in cls.bases if isinstance(b, ast.Name)]
        init = _init_of(cls)
        arg = _super_code_arg(init) if init is not None else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            own[cls.name] = arg.value
        else:
            own[cls.name] = None
            if init is not None:
                parametric.add(cls.name)

    def resolve(name: str, seen: frozenset[str]) -> str | None:
        if name not in own or name in seen or name in parametric:
            return None
        if own[name] is not None:
            return own[name]
        return next(
            (code for base in bases[name] if (code := resolve(base, seen | {name}))),
            None,
        )

    return {name: code for name in own if (code := resolve(name, frozenset()))}


def _code_families() -> dict[str, tuple[int, str, str]]:
    """Error classes whose code is one constructor argument plus a fixed suffix.

    Maps the class to ``(position, parameter, suffix)`` for an ``__init__``
    passing ``f"{parameter}<suffix>"`` as its code, so a construction site
    naming that argument as a literal resolves to a concrete code.
    """
    families: dict[str, tuple[int, str, str]] = {}
    for cls in _error_classes():
        init = _init_of(cls)
        arg = _super_code_arg(init) if init is not None else None
        if not (
            isinstance(arg, ast.JoinedStr)
            and len(arg.values) == 2
            and isinstance(arg.values[0], ast.FormattedValue)
            and isinstance(arg.values[0].value, ast.Name)
            and isinstance(arg.values[1], ast.Constant)
        ):
            continue
        parameter = arg.values[0].value.id
        params = [a.arg for a in init.args.args[1:]]
        if parameter in params:
            families[cls.name] = (params.index(parameter), parameter, arg.values[1].value)
    return families


# ---------------------------------------------------------------------------
# The ingest path
# ---------------------------------------------------------------------------


def _raised_on_ingest_path() -> set[str]:
    """Codes of the errors constructed on the path from ``IngestionService.ingest``.

    Follows ``self.<method>(...)`` calls into the service's own methods and
    bare calls into functions defined in the same module, including calls
    made inside ``with`` items and nested functions, which ``ast.walk``
    reaches.
    """
    tree = ast.parse(_INGESTION_MODULE.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "IngestionService"
    )
    methods = {
        node.name: node
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    by_class = _raised_code_by_class()
    families = _code_families()

    codes: set[str] = set()
    visited: set[tuple[str, str]] = set()
    pending: list[tuple[str, str]] = [("method", "ingest")]
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        kind, name = key
        for node in ast.walk(methods[name] if kind == "method" else functions[name]):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr in methods
            ):
                pending.append(("method", func.attr))
            elif isinstance(func, ast.Name) and func.id in functions:
                pending.append(("function", func.id))
            elif isinstance(func, ast.Name) and func.id in by_class:
                codes.add(by_class[func.id])
            elif isinstance(func, ast.Name) and func.id in families:
                position, parameter, suffix = families[func.id]
                keywords = {k.arg: k.value for k in node.keywords}
                value = (
                    node.args[position] if len(node.args) > position else keywords.get(parameter)
                )
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    codes.add(f"{value.value}{suffix}")
    return codes


# ---------------------------------------------------------------------------
# Contract readers
# ---------------------------------------------------------------------------


def _load(spec_name: str) -> dict[str, Any]:
    return yaml.safe_load(_SPECS[spec_name].read_text(encoding="utf-8"))


def _catalog() -> frozenset[str]:
    """Every token a description is read as naming a code by.

    The literal codes, the typed-alias family, and the family codes the ingest
    path resolves at its construction sites. Intersecting with it keeps field
    names and other backticked prose out of the declared sets.
    """
    return (
        frozenset(_raised_code_by_class().values()) | _TYPED_ALIAS_CODES | _raised_on_ingest_path()
    )


def _codes_in(text: str, catalog: frozenset[str]) -> set[str]:
    return {token for token in _BACKTICKED.findall(text) if token in catalog}


def _operation(spec: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for item in (spec.get("paths") or {}).values():
        for operation in item.values():
            if isinstance(operation, dict) and operation.get("operationId") == operation_id:
                return operation
    raise AssertionError(f"no operation {operation_id!r} in the spec")


def _declared(spec: dict[str, Any], operation_id: str, catalog: frozenset[str]) -> set[str]:
    """Codes named in the operation's non-2xx response descriptions."""
    responses = _operation(spec, operation_id).get("responses") or {}
    return {
        code
        for status, response in responses.items()
        if not str(status).startswith("2")
        for code in _codes_in(response.get("description") or "", catalog)
    }


def _vocabulary(spec: dict[str, Any], catalog: frozenset[str]) -> set[str]:
    entry = spec["components"]["schemas"]["BatchIngestFileError"]
    return _codes_in(entry["properties"]["code"]["description"], catalog)


def _derived(core: dict[str, Any], catalog: frozenset[str]) -> set[str]:
    declared = _declared(core, _INGEST_OPERATION, catalog)
    return declared - set(UNREACHABLE_PER_FILE) - BATCH_BOUNDARY


@pytest.fixture(scope="module")
def core() -> dict[str, Any]:
    return _load("sage_core")


@pytest.fixture(scope="module")
def catalog() -> frozenset[str]:
    return _catalog()


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


def test_code_map_resolves_known_classes():
    """The class-to-code map reads the literals it depends on.

    Every derivation below intersects with this catalog, so an empty or
    partial map would make them pass over nothing.
    """
    by_class = _raised_code_by_class()
    assert by_class.get("DuplicateContentError") == "duplicate_content"
    assert by_class.get("Tier3UniqueConstraintViolation") == "tier3_unique_constraint_violation"
    assert by_class.get("VaultSourceStoreUnavailableError") == "vault_source_store_unavailable"
    assert "InvalidTypedAliasError" not in by_class, "a parametric code has no literal to map"
    assert len(by_class) >= 15, f"code map collapsed to {len(by_class)} entries"
    assert _code_families().get("RelocationProvenanceMismatchError", (None,))[1:] == (
        "field",
        "_provenance_mismatch",
    )


def test_the_walk_reaches_the_ingest_path_and_only_it():
    """The walk follows the ingest path and does not sweep the whole module.

    Collecting nothing passes the declaration gate vacuously; collecting every
    construction in the module turns it into a check on other operations. The
    positive set is spread across the service's helpers, and the negative set
    is raised by sibling methods of the same class that ingest never calls.
    """
    raised = _raised_on_ingest_path()
    reached = {
        "duplicate_content",
        "stale_chain_head",
        "invalid_doc_type",
        "tier3_schema_violation",
        "reserved_transition",
        "adapter_not_found",
        "vault_source_path_refused",
        "relocated_from_provenance_mismatch",
    }
    assert reached <= raised, f"walk missed {sorted(reached - raised)}"
    siblings_only = {
        "no_projection",
        "reabstract_document_already_in_flight",
        "recompute_pipeline_already_in_flight",
    }
    assert not (siblings_only & raised), (
        f"walk left the ingest path: {sorted(siblings_only & raised)}"
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_every_code_the_ingest_path_raises_is_declared(core, catalog):
    """The ingest operation declares every code its service path constructs.

    The per-file vocabulary is derived from this declaration, so a code
    missing here would be missing from the batch contract too without the
    derivation noticing.
    """
    undeclared = _raised_on_ingest_path() - _declared(core, _INGEST_OPERATION, catalog)
    assert not undeclared, (
        f"{_INGEST_OPERATION} raises codes its responses do not declare: {sorted(undeclared)}"
    )


@pytest.mark.parametrize("spec_name", sorted(_SPECS))
def test_per_file_vocabulary_is_derived_from_the_ingest_operation(spec_name, core, catalog):
    """Each spec's per-file vocabulary equals the derivation, in both directions.

    Read from each committed spec separately rather than trusted to the
    description-parity gate to carry one spec to the other. Every backticked
    token in the description must be a recognized code, so a misspelled or
    invented one cannot sit in the list unread by the comparison.
    """
    spec = _load(spec_name)
    description = spec["components"]["schemas"]["BatchIngestFileError"]["properties"]["code"][
        "description"
    ]
    unrecognized = sorted(set(_BACKTICKED.findall(description)) - catalog)
    assert not unrecognized, (
        f"{spec_name}: BatchIngestFileError.code names non-codes {unrecognized}"
    )
    listed = _vocabulary(spec, catalog)
    derived = _derived(core, catalog)
    assert listed == derived, (
        f"{spec_name}: BatchIngestFileError.code lists {sorted(listed)}; "
        f"missing {sorted(derived - listed)}, not reachable per file {sorted(listed - derived)}"
    )


def test_excluded_codes_stay_unreachable():
    """The batch's request never carries a field an excluded code depends on."""
    tree = ast.parse(_BATCH_MODULE.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "IngestRequest"
    ]
    assert len(calls) == 1, f"expected one IngestRequest construction, found {len(calls)}"
    passed = {keyword.arg for keyword in calls[0].keywords}
    assert None not in passed, "a ** expansion hides which fields the request carries"
    assert "source" in passed, f"read the wrong construction: {sorted(passed)}"

    known = set(IngestRequest.model_fields)
    for code, fields in UNREACHABLE_PER_FILE.items():
        assert set(fields) <= known, (
            f"{code}: {sorted(set(fields) - known)} are not IngestRequest fields"
        )
    reachable = sorted(
        code for code, fields in UNREACHABLE_PER_FILE.items() if set(fields) & passed
    )
    assert not reachable, (
        f"the batch now passes {sorted(passed)}, which makes {reachable} reachable per file"
    )


def test_exclusion_tables_are_not_stale(core, catalog):
    """Every exclusion names a declared code, and the boundary codes are the batch's own."""
    declared = _declared(core, _INGEST_OPERATION, catalog)
    stale = sorted((set(UNREACHABLE_PER_FILE) | BATCH_BOUNDARY) - declared)
    assert not stale, f"exclusions naming codes {_INGEST_OPERATION} does not declare: {stale}"
    for spec_name, operation_id in _BATCH_OPERATIONS:
        missing = sorted(BATCH_BOUNDARY - _declared(_load(spec_name), operation_id, catalog))
        assert not missing, f"{operation_id} does not declare its boundary codes {missing}"


def test_mcp_bulk_tool_names_the_same_per_file_vocabulary(core, catalog):
    """The bulk tool's docstring names exactly the per-file vocabulary."""
    doc = _all_registered_tools()["bulk_ingest_document"].description or ""
    paragraph = re.search(r"Per-file precondition surface:(.*?)\n\s*\n", doc, re.S)
    assert paragraph, "the bulk tool docstring has no per-file precondition paragraph"
    listed = _codes_in(paragraph.group(1), catalog)
    derived = _derived(core, catalog)
    assert listed == derived, (
        f"bulk_ingest_document docstring: missing {sorted(derived - listed)}, "
        f"not reachable per file {sorted(listed - derived)}"
    )


def test_batch_operations_declare_no_per_file_status():
    """A per-file failure never becomes a batch status.

    Declaring the store-refusal statuses, or any other per-file status, on a
    batch operation would name responses it cannot return.
    """
    for spec_name, operation_id in _BATCH_OPERATIONS:
        statuses = set(_operation(_load(spec_name), operation_id).get("responses") or {})
        assert statuses == {"200", "400", "404"}, f"{operation_id} declares {sorted(statuses)}"

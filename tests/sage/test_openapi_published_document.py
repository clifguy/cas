"""Conformance tests for the document a deployment actually publishes.

`GET /openapi.json` is served without a token, so it is the artifact an
outside developer reads to build a client -- they have no access to this
repository. FastAPI generates that document from the live routes, which
means its prose comes from handler docstrings and function names, while
the authored prose lives in the committed specs under `docs/fs/`
(CAS-ADR-008). The two drift silently: the generator invents an
`operationId` per route and leaves `description` empty wherever a handler
carries no docstring.

These tests pin the published document to the committed specs for prose
only -- `summary`, `description`, `operationId`, `tags`, the top-level
`info.description`, and the tag block. Structure (paths, parameters,
request bodies, responses, component schemas) stays route-derived, so the
document still describes the routes the deployment is running rather than
the routes a spec file claims.

The published document spans both committed specs: the SAGE Core API and
the `/app/*` backend-for-frontend surface are served by one application
and documented as one document.

Run via: pytest tests/sage/test_openapi_published_document.py
"""

import re
from pathlib import Path

import pytest
import yaml

from sage.app import build_openapi_document, create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
CAS_APP_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml"
REST_GUIDE_PATH = _REPO_ROOT / "docs" / "api" / "sage-rest-api.md"

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# Facts the guide used to carry that a caller cannot reconstruct from the
# operation inventory alone. Each must survive in the published document's
# `info.description`, so an expansion that drops one is caught rather than
# matching itself.
_INFO_DESCRIPTION_MARKERS = (
    "vault",
    "error",
    "code",
    "never deleted",
    "transfer",
)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _spec_operations(spec: dict) -> dict[tuple[str, str], dict]:
    """{(path, method): operation} for one committed spec."""
    out: dict[tuple[str, str], dict] = {}
    for path, path_item in (spec.get("paths") or {}).items():
        for method, operation in (path_item or {}).items():
            if method.lower() in _HTTP_METHODS:
                out[(path, method.lower())] = operation or {}
    return out


@pytest.fixture(scope="module")
def committed_operations() -> dict[tuple[str, str], dict]:
    """Union of both committed specs' operations, keyed by (path, method).

    The two specs are disjoint by URL prefix (enforced by
    test_specs_respect_url_prefix_boundaries), so the union has no
    contested keys.
    """
    merged: dict[tuple[str, str], dict] = {}
    for spec_path in (SAGE_CORE_SPEC_PATH, CAS_APP_SPEC_PATH):
        merged.update(_spec_operations(yaml.safe_load(spec_path.read_text())))
    return merged


@pytest.fixture(scope="module")
def committed_tags() -> dict[str, str]:
    """{tag name: description} declared across both committed specs."""
    out: dict[str, str] = {}
    for spec_path in (SAGE_CORE_SPEC_PATH, CAS_APP_SPEC_PATH):
        spec = yaml.safe_load(spec_path.read_text())
        for tag in spec.get("tags") or []:
            out[tag["name"]] = tag.get("description", "") or ""
    return out


@pytest.fixture(scope="module")
def published() -> dict:
    """The document a default deployment serves at /openapi.json."""
    return create_app().openapi()


@pytest.fixture(scope="module")
def published_operations(published: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for path, path_item in (published.get("paths") or {}).items():
        for method, operation in (path_item or {}).items():
            if method.lower() in _HTTP_METHODS:
                out[(path, method.lower())] = operation or {}
    return out


# ---------------------------------------------------------------------------
# Every published operation carries usable prose
# ---------------------------------------------------------------------------


# FastAPI's generated fallback, e.g.
# `discover_sage_vaults__vault_id__discover_post`. A generated client turns
# these into method names, so they reach the caller verbatim.
_GENERATED_OPERATION_ID_RE = re.compile(r"__|_(get|post|put|patch|delete)$")


def test_every_published_operation_declares_prose(
    published_operations: dict[tuple[str, str], dict],
):
    """Every operation in the published document carries a non-empty
    summary and description and an authored operationId.

    A caller who fetches only this document has no other source for what
    an operation does; an empty description or a generated operationId
    leaves them reading path strings.
    """
    assert published_operations, "the published document declares no operations"

    issues: list[str] = []
    for (path, method), operation in sorted(published_operations.items()):
        label = f"{method.upper():6s} {path}"
        if not _norm_ws(operation.get("summary")):
            issues.append(f"{label}: no summary")
        if not _norm_ws(operation.get("description")):
            issues.append(f"{label}: no description")
        operation_id = operation.get("operationId") or ""
        if not operation_id:
            issues.append(f"{label}: no operationId")
        elif _GENERATED_OPERATION_ID_RE.search(operation_id):
            issues.append(f"{label}: generated operationId {operation_id!r}")

    assert not issues, "\n".join(issues)


# ---------------------------------------------------------------------------
# Published prose matches the committed specs
# ---------------------------------------------------------------------------


def test_published_operation_prose_matches_committed_specs(
    published_operations: dict[tuple[str, str], dict],
    committed_operations: dict[tuple[str, str], dict],
):
    """Published summary/description/operationId/tags equal the committed
    specs' text for every published operation.

    Compared whitespace-normalized: the specs use YAML folded scalars, so
    line wrapping is an authoring detail, not part of the contract.
    """
    issues: list[str] = []
    for key, operation in sorted(published_operations.items()):
        path, method = key
        label = f"{method.upper():6s} {path}"
        committed = committed_operations.get(key)
        if committed is None:
            issues.append(f"{label}: published but documented by no committed spec")
            continue
        for field in ("summary", "description", "operationId"):
            got, want = _norm_ws(operation.get(field)), _norm_ws(committed.get(field))
            if got != want:
                issues.append(f"{label}: {field}\n    published: {got!r}\n    committed: {want!r}")
        got_tags, want_tags = operation.get("tags") or [], committed.get("tags") or []
        if got_tags != want_tags:
            issues.append(f"{label}: tags\n    published: {got_tags}\n    committed: {want_tags}")

    assert not issues, "\n".join(issues)


def test_published_info_description_matches_committed_spec(published: dict):
    """The published `info.description` equals the SAGE Core spec's, and
    carries the cross-cutting facts a caller cannot infer from the
    operation inventory.

    The marker check is what makes this bite: comparing the published text
    to its own source would pass no matter how much of it was dropped.
    """
    committed = yaml.safe_load(SAGE_CORE_SPEC_PATH.read_text())
    want = _norm_ws((committed.get("info") or {}).get("description"))
    got = _norm_ws((published.get("info") or {}).get("description"))

    assert got == want, (
        f"published info.description diverges\n  published: {got!r}\n  committed: {want!r}"
    )

    lowered = got.lower()
    missing = [m for m in _INFO_DESCRIPTION_MARKERS if m.lower() not in lowered]
    assert not missing, (
        f"published info.description omits {missing}; a caller with no repository "
        f"access has no other source for these"
    )


def test_published_tags_are_declared_and_described(
    published: dict,
    published_operations: dict[tuple[str, str], dict],
    committed_tags: dict[str, str],
):
    """The published tag block matches the committed specs', and every tag
    an operation references is declared there with a description.

    A tag an operation uses but nothing declares renders as a bare group
    label with no explanation of what the group is.
    """
    published_tags = {
        t["name"]: t.get("description", "") or "" for t in published.get("tags") or []
    }
    assert published_tags == committed_tags, (
        f"published tag block diverges from the committed specs\n"
        f"  published-only: {sorted(set(published_tags) - set(committed_tags))}\n"
        f"  committed-only: {sorted(set(committed_tags) - set(published_tags))}"
    )

    referenced = {tag for op in published_operations.values() for tag in op.get("tags") or []}
    undeclared = sorted(referenced - set(published_tags))
    assert not undeclared, f"operations reference undeclared tags: {undeclared}"

    undescribed = sorted(name for name in referenced if not _norm_ws(published_tags.get(name)))
    assert not undescribed, f"declared tags carry no description: {undescribed}"


# ---------------------------------------------------------------------------
# What the overlay must NOT do
# ---------------------------------------------------------------------------


def _base_document_with_response_description(text: str) -> dict:
    """A minimal generated-shaped document for one real Core API operation."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "SAGE Core API", "version": "2.1", "description": "generated"},
        "paths": {
            "/sage_vaults/{vault_id}/discover": {
                "post": {
                    "operationId": "discover_sage_vaults__vault_id__discover_post",
                    "summary": "Discover",
                    "responses": {
                        "200": {"description": "generated success text"},
                        "404": {"description": text},
                    },
                }
            }
        },
    }


def test_overlay_leaves_response_descriptions_alone():
    """The prose overlay never writes response descriptions.

    `test_live_openapi_matches_yaml_error_envelope` compares the published
    document's non-2xx response descriptions against the YAML. If the
    overlay copied whole operation objects, that comparison would be the
    YAML against itself -- green, and proving nothing. The overlay must
    write named prose fields only.
    """
    sentinel = "SENTINEL: this text exists in no committed spec."
    base = _base_document_with_response_description(sentinel)

    document = build_openapi_document(base, None)

    responses = document["paths"]["/sage_vaults/{vault_id}/discover"]["post"]["responses"]
    assert responses["404"]["description"] == sentinel, (
        "the overlay overwrote a response description; the error-envelope "
        "conformance test is now comparing the spec against itself"
    )
    assert responses["200"]["description"] == "generated success text"


def test_overlay_enriches_prose_on_the_same_document():
    """Positive control for the test above: the overlay does reach the
    operation whose responses it must leave alone.

    Without this, a no-op overlay would satisfy the guard trivially.
    """
    base = _base_document_with_response_description("untouched")

    document = build_openapi_document(base, None)

    operation = document["paths"]["/sage_vaults/{vault_id}/discover"]["post"]
    assert operation["operationId"] == "search"
    assert _norm_ws(operation.get("description")), "the overlay wrote no description"


def test_overlay_ignores_spec_operations_absent_from_routes(published: dict):
    """Operations a committed spec declares but the app does not route stay
    out of the published document.

    The editors endpoints are forward declarations: documented, not built.
    Publishing them would tell a caller the deployment serves something it
    does not.
    """
    editors = "/sage_vaults/{vault_id}/documents/{document_id}/editors"
    published_paths = published.get("paths") or {}

    assert editors not in published_paths, (
        "the published document declares an operation the app does not route; "
        "the overlay is injecting spec paths rather than enriching live ones"
    )


@pytest.mark.parametrize("attribute", ["_SAGE_CORE_SPEC_PATH", "_CAS_APP_SPEC_PATH"])
def test_overlay_requires_the_committed_specs(attribute: str, monkeypatch: pytest.MonkeyPatch):
    """A missing committed spec fails loudly rather than publishing a
    document stripped of its prose.

    An image built without the spec files would otherwise serve a thin
    document that looks complete -- every path present, every explanation
    gone -- with nothing to signal the loss.

    Both specs are checked independently: a fallback added around one read
    would otherwise hide behind the other read still raising.
    """
    import sage.app as sage_app

    sage_app._load_published_prose.cache_clear()
    monkeypatch.setattr(sage_app, attribute, _REPO_ROOT / "docs" / "fs" / "absent.yaml")
    try:
        with pytest.raises(OSError):
            sage_app._load_published_prose()
    finally:
        sage_app._load_published_prose.cache_clear()


# ---------------------------------------------------------------------------
# The prose guide restates nothing the published document already carries
# ---------------------------------------------------------------------------


# "42 operations", "39 paths", "18 of the 39 paths", "one authenticated
# endpoint" -- a number qualifying a countable API noun.
_COUNT_WORD = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|dozens?)"
_API_NOUN = r"(?:operations?|paths?|endpoints?)"
_COUNT_CLAIM_RE = re.compile(
    # A count, then the noun it counts, with room for adjectives between:
    # "18 of the 39 paths", "one authenticated endpoint".
    rf"\b{_COUNT_WORD}\b(?:\s+\w+){{0,3}}?\s+\b{_API_NOUN}\b"
    # The label form a summary bullet uses: "**Operations:** 42". Bound to a
    # colon rather than proximity, so prose that happens to put a number near
    # the word "endpoint" ("the token endpoint works against exactly one
    # deployment") is not mistaken for a count of endpoints.
    rf"|\b{_API_NOUN}\b\s*\**\s*:\s*\**\s*{_COUNT_WORD}\b",
    re.IGNORECASE,
)

_METHOD_TABLE_ROW_RE = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|", re.MULTILINE)


def test_rest_guide_claims_no_operation_or_path_counts():
    """The client guide states no operation, path, or endpoint count.

    Counts in prose have no gate keeping them honest, and this file's did
    go stale: the shipped figures stopped matching the served document.
    Removing the claim removes the drift; there is nothing left to verify.
    """
    guide = REST_GUIDE_PATH.read_text()

    offenders = [
        f"  line {i}: {line.strip()}"
        for i, line in enumerate(guide.splitlines(), start=1)
        if _COUNT_CLAIM_RE.search(line)
    ]

    assert not offenders, "the guide makes a count claim nothing gates:\n" + "\n".join(offenders)


def test_rest_guide_does_not_restate_the_operation_inventory():
    """The client guide carries no method/path inventory table.

    The published document is the inventory. A second copy in prose is the
    one that goes stale, because only the published document is gated
    against the live routes.
    """
    guide = REST_GUIDE_PATH.read_text()

    rows = _METHOD_TABLE_ROW_RE.findall(guide)
    assert not rows, (
        f"the guide restates the operation inventory in {len(rows)} method-table "
        f"row(s); the published document already carries it"
    )
    assert "operationId" not in guide, (
        "the guide references operationId values, which live in the published document"
    )

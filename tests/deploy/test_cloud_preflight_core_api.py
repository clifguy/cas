"""The preflight's read-only Core API sweep.

Holds what the sweep probes, that it never issues a mutating request or reports
a response body, and that its shape markers bind to the published contract
rather than to a stub.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Final

import pytest
import yaml

from tests.deploy import _preflight_harness
from tests.deploy._preflight_harness import (
    _ABSENT_DOC,
    _ABSENT_VAULT,
    _DOCUMENT_BODY,
    _NEEDS_RUNTIME,
    _OPENAPI_SPEC,
    _PARSE_BODY,
    _SCRIPT,
    _STATS_BODY,
    _VAULT_CONFIG_SCHEMA,
    _base_env,
    _detail,
    _green,
    _run,
    _script_text,
    _verdicts,
    serve,
)

# --------------------------------------------------------------------------- #
# H. Read-only Core API sweep (deployed REST coverage beyond /sage_vaults)     #
#                                                                             #
# The REST adapter is barely exercised against a live tenant: the web client   #
# covers it only when a human uses it, and the MCP tools call the service      #
# layer in process rather than over HTTP. These checks measure that surface    #
# instead of assuming it. Every probe is a read or a pure computation, and     #
# that property is itself asserted below -- at the wire and in the script text #
# -- rather than left to a source comment.                                     #
# --------------------------------------------------------------------------- #
_SWEEP_CHECKS = "core_api_vault_reads,core_api_document_reads,core_api_parse_filename"

#: Every (method, path-shape) the sweep is permitted to issue. Anything outside
#: this set mutates vault state and must never appear on the wire.
_READ_ONLY_ALLOWLIST: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", "/sage_vaults"),
        ("GET", "/stats"),
        ("GET", "/config"),
        ("GET", "/pending-metadata"),
        ("GET", "/staging-edges"),
        ("GET", "/document"),
        ("GET", "/headings"),
        ("POST", "/discover"),
        ("POST", "/traverse"),
        ("POST", "/parse-filename"),
    }
)

#: Route fragments that mutate. None may appear inside a sweep check's body.
_MUTATING_FRAGMENTS: Final[tuple[str, ...]] = (
    "/edges",
    "/lifecycles",
    "/metadata",
    ":batch",
    "/confirm",
    "/dismiss",
    "/reabstract",
    "/open",
    "/export",
    "/hash-check",
    "/eval-retrieval",
    "/refresh-views",
    "/maintenance/",
)


def _classify(method: str, path: str) -> tuple[str, str]:
    """Reduce a recorded request to the (method, path-shape) the allowlist keys on."""
    p = path.split("?", 1)[0]
    if p == "/sage_vaults":
        return method, "/sage_vaults"
    for leaf in ("/stats", "/config", "/pending-metadata", "/staging-edges", "/headings"):
        if p.endswith(leaf):
            return method, leaf
    for leaf in ("/discover", "/traverse", "/parse-filename"):
        if p.endswith(leaf):
            return method, leaf
    if "/documents/" in p:
        return method, "/document"
    return method, p


def _join_continuations(text: str) -> str:
    """Fold ``\\``-continued shell lines into one physical line, so a line-wise
    scan reads the statement the author wrote rather than its formatting."""
    return re.sub(r"\\\n\s*", " ", text)


#: A command substitution whose pipeline reduces its input to a bounded token --
#: an id, a status, a single field. Interpolating a response body *through* one
#: of these is extraction, not disclosure, so the leak scan below strips such
#: spans before looking for the body.
_BOUNDED_EXTRACTORS: Final[tuple[str, ...]] = ("grep -o", "sed", "cut", "awk", "tr ", "head -c")

#: The response-body globals a check must never publish. ``HTTP_BODY`` is the raw
#: payload; ``body`` is the conventional local a check copies it into.
_BODY_TOKENS: Final[tuple[str, ...]] = ("$HTTP_BODY", "${HTTP_BODY}", "$body", "${body}")


def _strip_comment_lines(text: str) -> str:
    """Drop whole-line ``#`` comments so prose naming a function or a variable
    cannot be mistaken for a call or an assignment."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _all_function_bodies(text: str) -> dict[str, str]:
    """Every shell function in ``text``, keyed by name.

    Spans run from the function's ``name() {`` line to the closing ``}`` at
    column 0 -- the script's own formatting, so no parser is needed.
    """
    stripped = _strip_comment_lines(text)
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"^([a-z_0-9]+)\(\) \{.*?$(.*?)^\}$", stripped, re.S | re.M)
    }


def _reachable_from(roots: Iterable[str], bodies: dict[str, str]) -> dict[str, str]:
    """The call-graph closure of ``roots`` over ``bodies``.

    Keying the sweep's structural gates on reachability rather than on a
    hand-maintained name list is the point: the helper that actually composes a
    failure string is one call away from the check that reports it, and a list
    written when the check was authored does not learn about a helper added
    later. Word-boundary matching is exact here because ``_`` is a word
    character -- ``http_get`` does not match inside ``http_get_browser``.
    """
    seen: dict[str, str] = {}
    queue = [name for name in roots if name in bodies]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen[name] = bodies[name]
        for candidate in bodies:
            if candidate not in seen and re.search(rf"\b{re.escape(candidate)}\b", bodies[name]):
                queue.append(candidate)
    return seen


def _sweep_roots(text: str) -> list[str]:
    """The check functions the registry wires to the sweep's check ids -- read
    out of the ``register`` lines rather than restated here, so a renamed check
    cannot silently drop out of the scan."""
    return re.findall(r"^register\s+core_api_\w+\s+(\w+)", _strip_comment_lines(text), re.M)


def _sweep_function_bodies() -> dict[str, str]:
    """Every function the sweep checks reach, keyed by name."""
    text = _script_text()
    roots = _sweep_roots(text)
    assert roots, "the registry wires no core_api_* check to a function"
    reachable = _reachable_from(roots, _all_function_bodies(text))
    assert set(roots) <= set(reachable), "a registered sweep check is not defined in the harness"
    return reachable


def _reported_variables(bodies: dict[str, str]) -> set[str]:
    """``DETAIL_MSG`` plus every variable interpolated into a ``DETAIL_MSG``.

    Derived rather than listed: a check does not build its matrix line in one
    statement, it accumulates text in a helper variable and interpolates that.
    Whatever flows into ``DETAIL_MSG`` is as published as ``DETAIL_MSG`` itself,
    so the leak scan has to cover it -- and has to keep covering it when a
    later accumulator is added under a name nobody wrote down here.
    """
    reported = {"DETAIL_MSG"}
    for body in bodies.values():
        for line in _join_continuations(body).splitlines():
            m = re.search(r"DETAIL_MSG=\"(.*)\"\s*$", line)
            if not m:
                continue
            reported.update(re.findall(r"\$\{?([A-Za-z_][A-Za-z_0-9]*)\}?", m.group(1)))
    return reported


def _body_leak_findings(bodies: dict[str, str]) -> list[str]:
    """Assignments that would publish a response body on the matrix line.

    The sweep reads ``GET .../config``, which returns the vault's full
    configuration, and preflight output lands in a deploy log. A finding is any
    assignment to a reported variable that interpolates the body outside a
    bounded extraction -- so pulling an id out of a payload is allowed and
    pasting the payload into a message is not.

    ``DETAIL_MSG`` is exempt from that allowance: it *is* the matrix line, and no
    extraction is narrow enough to justify composing it out of a response body.
    Scoping the allowance to the derived variables that actually need it keeps
    this scan from relaxing the rule it inherited while widening the surface.
    """
    reported = _reported_variables(bodies)
    findings: list[str] = []
    for name, body in bodies.items():
        for line in _join_continuations(body).splitlines():
            m = re.match(r"\s*(?:local\s+)?([A-Za-z_][A-Za-z_0-9]*)=", line)
            if not m or m.group(1) not in reported:
                continue
            if m.group(1) == "DETAIL_MSG":
                residue = line
            else:
                residue = re.sub(
                    r"\$\((?:[^()]|\([^()]*\))*\)",
                    lambda s: (
                        "" if any(e in s.group(0) for e in _BOUNDED_EXTRACTORS) else s.group(0)
                    ),
                    line,
                )
            for token in _BODY_TOKENS:
                if token in residue:
                    findings.append(f"{name}: {line.strip()}")
                    break
    return findings


# --- Side-effect freedom (the acceptance criterion, made testable) ---------- #
def _post_targets(body: str) -> list[str]:
    """Every URL an ``http_post`` in ``body`` actually targets.

    Shell variables assigned inside the function are substituted first, so the
    assertion is about the endpoint the call reaches rather than about whether
    the author happened to inline the URL at the call site. A gate that only
    matched the literal call line would pass or fail on formatting.
    """
    assignments = dict(re.findall(r"(?:local\s+)?(\w+)=\"([^\"]*)\"", body))
    # One expansion pass resolves the nesting these helpers actually use
    # (a url built from a base, which is built from an env var).
    for _ in range(3):
        for key, value in list(assignments.items()):
            assignments[key] = re.sub(
                r"\$\{?(\w+)\}?", lambda m: assignments.get(m.group(1), m.group(0)), value
            )
    targets: list[str] = []
    # A call may wrap onto a continuation line; join them before matching.
    for line in _join_continuations(body).splitlines():
        m = re.search(r'http_post\s+"([^"]*)"', line)
        if m:
            targets.append(
                re.sub(
                    r"\$\{?(\w+)\}?", lambda g: assignments.get(g.group(1), g.group(0)), m.group(1)
                )
            )
    return targets


def test_core_api_sweep_text_carries_no_mutating_route() -> None:
    """Structural leg: no sweep check names a mutating route, and its only POSTs
    go to the three side-effect-free endpoints. Always on -- needs no network.
    """
    for name, body in _sweep_function_bodies().items():
        for fragment in _MUTATING_FRAGMENTS:
            assert fragment not in body, f"{name} names the mutating route {fragment}"
        for target in _post_targets(body):
            assert any(
                endpoint in target for endpoint in ("/discover", "/traverse", "/parse-filename")
            ), f"{name} POSTs to something other than the read-only endpoints: {target}"
        assert "http_put" not in body, f"{name} issues a PUT"
        assert "-X DELETE" not in body, f"{name} issues a DELETE"


# --- Shape markers bound to the authoritative response schema -------------- #
#: Each ``sweep_get`` label, and the Core API operation whose 200 response its
#: shape marker is asserting. Mirroring a marker between the shipped script and
#: a stub proves only that the two copies agree; resolving it against the
#: committed document proves it names something the deployment actually returns.
_SWEEP_MARKER_TABLE: Final[dict[str, tuple[str, str]]] = {
    "/stats": ("get", "/sage_vaults/{vault_id}/stats"),
    "/config": ("get", "/sage_vaults/{vault_id}/config"),
    "/pending-metadata": ("get", "/sage_vaults/{vault_id}/pending-metadata"),
    "/staging-edges": ("get", "/sage_vaults/{vault_id}/staging-edges"),
    "/documents/{id}": ("get", "/sage_vaults/{vault_id}/documents/{document_id}"),
    "/documents/{id}/headings": (
        "get",
        "/sage_vaults/{vault_id}/documents/{document_id}/headings",
    ),
}

#: GET /config returns an untyped dict, so the OpenAPI document defers its shape
#: to the vault-config JSON Schema. That schema is this label's authority.
_SCHEMA_BACKED_LABELS: Final[frozenset[str]] = frozenset({"/config"})

#: Each stub response body, and the operation whose 200 schema it stands in for.
#: Binding the stub side too is what stops the pair drifting together: a
#: response model that gains a required field reds the stub that omits it.
_STUB_BODY_AUTHORITY: Final[dict[str, tuple[str, str]]] = {
    "_STATS_BODY": ("get", "/sage_vaults/{vault_id}/stats"),
    "_CONFIG_BODY": ("get", "/sage_vaults/{vault_id}/config"),
    "_PENDING_METADATA_BODY": ("get", "/sage_vaults/{vault_id}/pending-metadata"),
    "_DOCUMENT_BODY": ("get", "/sage_vaults/{vault_id}/documents/{document_id}"),
    "_HEADINGS_BODY": ("get", "/sage_vaults/{vault_id}/documents/{document_id}/headings"),
    "_TRAVERSE_BODY": ("post", "/sage_vaults/{vault_id}/traverse"),
    "_PARSE_BODY": ("post", "/sage_vaults/{vault_id}/parse-filename"),
    "_DISCOVER_OK": ("post", "/sage_vaults/{vault_id}/discover"),
}

_SPEC_CACHE: dict[str, dict] = {}


def _openapi_document() -> dict:
    if "spec" not in _SPEC_CACHE:
        _SPEC_CACHE["spec"] = yaml.safe_load(_OPENAPI_SPEC.read_text(encoding="utf-8"))
    return _SPEC_CACHE["spec"]


def _vault_config_schema() -> dict:
    if "vault_config" not in _SPEC_CACHE:
        _SPEC_CACHE["vault_config"] = json.loads(_VAULT_CONFIG_SCHEMA.read_text(encoding="utf-8"))
    return _SPEC_CACHE["vault_config"]


def _deref(schema: dict) -> dict:
    """Follow a ``$ref`` into ``components/schemas``."""
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = ref.rsplit("/", 1)[-1]
    resolved = _openapi_document()["components"]["schemas"].get(name)
    assert resolved is not None, f"the document has no component schema {name}"
    return resolved


# The branch of a composed 200 response that the preflight's own calls meet. The
# document read answers with a download recipe only when the call names
# write_to_path, which neither the sweep nor its stub does, so the document arm
# is the response they are held to.
_RESPONSE_BRANCH: dict[tuple[str, str], str] = {
    ("get", "/sage_vaults/{vault_id}/documents/{document_id}"): "DocumentWithContent",
}


def _response_schema(method: str, path: str) -> dict:
    """The 200 response schema of one operation, with a ``$ref`` followed once.

    A response composed of alternatives is resolved to the branch
    ``_RESPONSE_BRANCH`` names for the operation; one it names none for fails
    here, since no single shape could stand for it.
    """
    operation = _openapi_document()["paths"].get(path, {}).get(method)
    assert operation is not None, f"the document has no {method.upper()} {path}"
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branch = _RESPONSE_BRANCH.get((method, path))
            assert branch is not None, (
                f"{method.upper()} {path} composes its 200 response; name the branch "
                "the preflight meets in _RESPONSE_BRANCH"
            )
            matches = [m for m in schema[key] if m.get("$ref", "").endswith(f"/{branch}")]
            assert len(matches) == 1, f"{method.upper()} {path} has no {branch} branch"
            return _deref(matches[0])
    return _deref(schema)


def _required_and_properties(schema: dict) -> tuple[set[str], set[str]]:
    """The required and declared property names of a schema, flattening one
    level of ``allOf`` -- the composition FastAPI emits for a model that extends
    another, and where several of the sweep's fields actually live."""
    required: set[str] = set(schema.get("required", []))
    properties: set[str] = set(schema.get("properties", {}))
    for member in schema.get("allOf", []):
        member_required, member_properties = _required_and_properties(_deref(member))
        required |= member_required
        properties |= member_properties
    return required, properties


def _authority_for(label: str) -> tuple[set[str], set[str], dict]:
    """``(required, properties, schema)`` for a sweep label."""
    if label in _SCHEMA_BACKED_LABELS:
        schema = _vault_config_schema()
    else:
        schema = _response_schema(*_SWEEP_MARKER_TABLE[label])
    required, properties = _required_and_properties(schema)
    return required, properties, schema


def _sweep_get_call_sites(text: str) -> list[tuple[str, str]]:
    """``(label, marker)`` for every ``sweep_get`` call in ``text``.

    The marker is returned with its shell quoting stripped, so the assertion is
    about the pattern the check greps for rather than about how it was quoted.
    """
    pattern = re.compile(r"""sweep_get\s+"[^"]*"\s+"([^"]*)"\s+('[^']*'|"(?:\\.|[^"\\])*")""")
    sites: list[tuple[str, str]] = []
    for label, raw in pattern.findall(_join_continuations(_strip_comment_lines(text))):
        marker = raw[1:-1]
        if raw.startswith('"'):
            marker = marker.replace('\\"', '"')
        sites.append((label, marker))
    return sites


def test_sweep_marker_table_covers_every_sweep_get() -> None:
    """Every shipped call site is bound, and no table entry outlives its call
    site. Also pins the count: an extractor that found nothing would make every
    binding assertion below pass vacuously.
    """
    sites = _sweep_get_call_sites(_script_text())
    assert len(sites) == 6, f"expected 6 sweep_get call sites, found {len(sites)}: {sites}"
    assert {label for label, _ in sites} == set(_SWEEP_MARKER_TABLE), (
        f"call sites {sorted({label for label, _ in sites})} vs table {sorted(_SWEEP_MARKER_TABLE)}"
    )


def test_sweep_marker_paths_exist_in_the_openapi_document() -> None:
    """Every bound operation is one the committed document actually declares --
    so a renamed or retired route reds the gate here rather than on a tenant."""
    for label, (method, path) in _SWEEP_MARKER_TABLE.items():
        schema = _response_schema(method, path)
        assert schema, f"{label}: {method.upper()} {path} declares no 200 schema"


@pytest.mark.parametrize("label,marker", _sweep_get_call_sites(_SCRIPT.read_text(encoding="utf-8")))
def test_sweep_marker_resolves_against_its_response_schema(label: str, marker: str) -> None:
    """Each shape marker names something the operation's 200 response is
    guaranteed to carry.

    Two marker forms, one rule each: a JSON field marker must be a *required*
    property (an optional one would let a healthy response miss it); and the
    echoed-document-id marker must have a required ``id`` to echo.
    """
    required, _, _ = _authority_for(label)
    if "$DOC_FIRST_ID" in marker:
        assert "id" in required, f"{label}: the echoed id is not a required response property"
        return
    field = marker.strip('"')
    assert field in required, (
        f"{label}: marker '{field}' is not a required property of the 200 response "
        f"(required: {sorted(required)})"
    )


def test_config_marker_is_a_required_vault_config_section() -> None:
    """The one label whose response the document types only as an object: its
    marker is resolved against the vault-config schema instead."""
    required, _, _ = _authority_for("/config")
    assert "edge_inference" in required


def test_marker_binding_detects_a_phantom_field() -> None:
    """The teeth test: a marker naming a field no response model declares is
    rejected. Without it, an authority that resolved to an empty required set
    would pass every marker above.
    """
    with pytest.raises(AssertionError):
        test_sweep_marker_resolves_against_its_response_schema("/stats", '"totally_not_a_field"')


def test_marker_binding_rejects_a_declared_but_optional_field() -> None:
    """The teeth test the phantom-field case cannot supply.

    A phantom name is absent from both the required list and the property list,
    so a resolver that returned *properties* instead of *required* would reject
    it too -- and would then accept a marker naming a merely-optional field. A
    healthy deployment may omit such a field, so the sweep would red on a
    correct tenant, which is the exact defect class this binding exists to
    prevent. `last_optimize` is declared on the stats response and not required.
    """
    _, properties, _ = _authority_for("/stats")
    assert "last_optimize" in properties, (
        "the probe field is no longer declared; pick another optional property"
    )
    with pytest.raises(AssertionError):
        test_sweep_marker_resolves_against_its_response_schema("/stats", '"last_optimize"')


def test_body_leak_detector_does_not_exempt_a_detail_msg_extraction() -> None:
    """The matrix line takes no extraction allowance.

    Deriving an accumulator's value from a payload through a bounded pipeline is
    legitimate; composing the reported line itself that way is not, and the scan
    this one replaced had no such allowance. Without this case, widening the
    surface would have quietly relaxed the rule on its most exposed variable.
    """
    leaking_detail = """
check_probe() {
  DETAIL_MSG="$(printf '%s' "$HTTP_BODY" | grep -oE '.*')"
  return 1
}
"""
    assert _body_leak_findings(_all_function_bodies(leaking_detail))


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ('{"headings":[],"title":"Probe"}', "document_id"),
        ('{"document_id":"x","title":"Probe","headings":[],"invented":1}', "invented"),
    ],
    ids=["omits-required", "invents-property"],
)
def test_stub_conformance_detects_a_drifted_stub(mutation: str, expected: str) -> None:
    """The teeth test for both halves of the stub binding: a stub that drops a
    required property and one that invents a property each have to be caught, or
    the conformance check below passes on stubs it never really constrained.
    """
    required, properties = _required_and_properties(
        _response_schema(*_STUB_BODY_AUTHORITY["_HEADINGS_BODY"])
    )
    keys = set(json.loads(mutation))
    assert not (required <= keys and keys <= properties), (
        f"a stub drifted on '{expected}' would pass the conformance check"
    )


@pytest.mark.parametrize("stub_name", sorted(_STUB_BODY_AUTHORITY))
def test_stub_bodies_conform_to_their_response_schema(stub_name: str) -> None:
    """The other half of the binding: each stub body carries every required
    property of the response it stands in for, and invents none.

    A stub is what the behavioral tests assert against, so a stub that has
    drifted from the response model makes those tests prove something about a
    shape no deployment serves.
    """
    method, path = _STUB_BODY_AUTHORITY[stub_name]
    required, properties = _required_and_properties(_response_schema(method, path))
    if stub_name == "_CONFIG_BODY":
        required, properties = _required_and_properties(_vault_config_schema())
    payload = json.loads(getattr(_preflight_harness, stub_name))
    keys = set(payload)
    assert required <= keys, f"{stub_name} omits required {sorted(required - keys)}"
    assert keys <= properties, f"{stub_name} invents {sorted(keys - properties)}"


def test_core_api_sweep_never_reports_a_response_body() -> None:
    """Nothing the sweep reaches may put a response body on the matrix line.

    The sweep reads GET .../config, which returns the vault's full configuration.
    Preflight output lands in a deploy log, so a detail message that interpolated
    a body would publish that configuration. Every check must report the endpoint
    and the observed status instead -- the same discipline the token diagnostic
    keeps when it prints decoded claims but never the token.

    The scan runs over the call-graph closure, not over the checks alone: the
    strings a check reports are composed one call away, in a shared helper, and
    a scan that stopped at the check would read the interpolation and never the
    text being interpolated.
    """
    findings = _body_leak_findings(_sweep_function_bodies())
    assert not findings, f"a response body reaches the matrix line: {findings}"


def test_sweep_reachable_set_includes_shared_helpers() -> None:
    """The closure the structural gates scan actually contains the helpers the
    sweep composes its output in -- the coverage a name list silently lost."""
    reachable = _sweep_function_bodies()
    for helper in ("sweep_get", "resolve_probe_vault", "resolve_probe_document"):
        assert helper in reachable, f"{helper} is outside the scanned closure"
    for transport in ("http_get", "http_post"):
        assert transport in reachable, f"{transport} is outside the scanned closure"


def test_reported_variable_closure_is_derived_not_listed() -> None:
    """``SWEEP_FAILURES`` is in the reported set because the script interpolates
    it into a DETAIL_MSG, not because this file names it. A later accumulator
    joins the set the same way."""
    reported = _reported_variables(_sweep_function_bodies())
    assert "DETAIL_MSG" in reported
    assert "SWEEP_FAILURES" in reported, (
        "the accumulator the sweep reports through is outside the leak scan"
    )


_LEAKING_HELPER = """
check_core_api_probe() {
  ACCUMULATOR=""
  probe_one "$base/config"
  DETAIL_MSG="reads failed:$ACCUMULATOR"
  return 1
}

probe_one() {
  ACCUMULATOR="$ACCUMULATOR $1 $HTTP_BODY;"
  return 0
}
"""

_EXTRACTING_HELPER = """
check_core_api_probe() {
  ACCUMULATOR=""
  probe_one "$base/config"
  DETAIL_MSG="read '$ACCUMULATOR'"
  return 1
}

probe_one() {
  ACCUMULATOR="$(printf '%s' "$HTTP_BODY" | grep -oE '"[0-9a-f]{8}_[a-z0-9_]+"' | head -1)"
  return 0
}
"""


def test_body_leak_detector_fires() -> None:
    """The teeth test: a body pasted into an accumulator inside a *helper* --
    never into a DETAIL_MSG directly -- is caught. This is exactly the edit the
    name-list scan would have passed.
    """
    findings = _body_leak_findings(_all_function_bodies(_LEAKING_HELPER))
    assert findings, "the detector missed a response body reaching the matrix line"
    assert any("probe_one" in f for f in findings), findings


def test_body_leak_detector_permits_bounded_extraction() -> None:
    """The false-positive guard: pulling a document id out of a payload through
    a bounded pipeline is extraction, not disclosure. A detector that flagged it
    would force the sweep to stop resolving a document at all.
    """
    assert not _body_leak_findings(_all_function_bodies(_EXTRACTING_HELPER))


@_NEEDS_RUNTIME
def test_core_api_sweep_issues_no_mutating_request() -> None:
    """Behavioral leg: record every request a full sweep puts on the wire and
    assert each one is in the read-only allowlist. This is the assertion the
    structural scan cannot make -- it observes what the script actually did,
    not what its source says.
    """
    seen: list[tuple[str, str]] = []

    def recording(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        seen.append(_classify(method, path))
        return _green(method, path, body)

    with serve(recording) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_SWEEP_CHECKS))
    assert proc.returncode == 0, f"the sweep must pass against a healthy tenant:\n{proc.stdout}"
    assert seen, "the sweep issued no requests at all"
    outside = set(seen) - _READ_ONLY_ALLOWLIST
    assert not outside, f"the sweep issued non-read requests: {sorted(outside)}"


# --- core_api_vault_reads --------------------------------------------------- #
@_NEEDS_RUNTIME
def test_core_api_vault_reads_passes() -> None:
    """All four vault-scoped reads 200 with their shape fields -> PASS."""
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "PASS", proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize("endpoint", ["/stats", "/config", "/pending-metadata", "/staging-edges"])
def test_core_api_vault_reads_fails_on_endpoint_404(endpoint: str) -> None:
    """One endpoint 404s while the rest stay healthy -> FAIL, and the detail
    names that endpoint AND the observed code. A message that only said "a read
    failed" would leave an operator to bisect the sweep by hand.
    """

    def broken(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith(endpoint):
            return 404, '{"error":"not_found"}', {}
        return _green(method, path, body)

    with serve(broken) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    detail = _detail(proc.stdout, "core_api_vault_reads")
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout
    assert endpoint in detail, f"detail does not name the endpoint: {detail}"
    assert "404" in detail, f"detail does not name the observed code: {detail}"


@_NEEDS_RUNTIME
def test_core_api_vault_reads_fails_on_a_bare_staging_edges_array() -> None:
    """The staging queue answers with an envelope carrying its read markers, so
    a bare array is a deployment still serving the previous contract."""

    def bare_array(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/staging-edges"):
            return 200, "[]", {}
        return _green(method, path, body)

    with serve(bare_array) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout
    assert "/staging-edges" in _detail(proc.stdout, "core_api_vault_reads")


@_NEEDS_RUNTIME
def test_edge_authn_backend_fails_on_a_bare_vault_array() -> None:
    """The vault list is an envelope now, so a bare array is not credited as
    the backend answering."""

    def bare_array(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/sage_vaults":
            return 200, '[{"id":"cas","name":"CAS"}]', {}
        return _green(method, path, body)

    with serve(bare_array) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_authn_backend"))
    assert _verdicts(proc.stdout).get("edge_authn_backend") == "FAIL", proc.stdout


@_NEEDS_RUNTIME
def test_core_api_vault_reads_fails_on_a_bare_pending_metadata_array() -> None:
    """The queue answers with a page, so the bare array it once returned is a
    deployment still serving the previous contract, and the sweep says so."""

    def bare_array(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/pending-metadata"):
            return 200, "[]", {}
        return _green(method, path, body)

    with serve(bare_array) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout
    assert "/pending-metadata" in _detail(proc.stdout, "core_api_vault_reads")


@_NEEDS_RUNTIME
def test_core_api_vault_reads_fails_when_absent_vault_returns_200() -> None:
    """THE anti-coincidental test for the sweep: every real read is healthy, but
    the edge also 200s a vault that does not exist. Four green reads must not be
    credited when the endpoint cannot tell a real vault from an absent one.
    """

    def absent_vault_200(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].startswith(f"/sage_vaults/{_ABSENT_VAULT}"):
            return 200, _STATS_BODY, {}
        return _green(method, path, body)

    with serve(absent_vault_200) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout
    assert "control" in _detail(proc.stdout, "core_api_vault_reads").lower()


@_NEEDS_RUNTIME
def test_core_api_vault_reads_fails_on_wrong_shape() -> None:
    """/stats answers 200 with a body that is not a VaultStatsResponse -> FAIL.
    Proves the check reads the payload, not merely the status line.
    """

    def canned(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/stats"):
            return 200, '{"ok":true}', {}
        return _green(method, path, body)

    with serve(canned) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "FAIL", proc.stdout


@_NEEDS_RUNTIME
def test_core_api_vault_reads_skips_when_no_vault_available() -> None:
    """An empty vault registry is vault_load's failure to report, not this
    check's -- it SKIPs so one root cause does not over-paint the matrix.
    """

    def empty_vaults(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/sage_vaults":
            return 200, '{"vaults":[],"count":0}', {}
        return _green(method, path, body)

    with serve(empty_vaults) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_vault_reads"))
    assert _verdicts(proc.stdout).get("core_api_vault_reads") == "SKIP", proc.stdout


# --- core_api_document_reads ------------------------------------------------ #
@_NEEDS_RUNTIME
def test_core_api_document_reads_passes() -> None:
    """A document resolves from the catalog probe and all three document-scoped
    reads answer 200 with their shape fields -> PASS.
    """
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "PASS", proc.stdout


@_NEEDS_RUNTIME
def test_core_api_document_reads_skips_on_empty_vault() -> None:
    """A deployment whose vault holds no documents must not fail the sweep: the
    catalog probe answers 200 with nothing to read, so the check SKIPs and the
    run still exits 0.
    """

    def no_documents(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/discover") and b"deterministic" not in body:
            return 200, '{"mode":"catalog","results":[],"total_available":0}', {}
        return _green(method, path, body)

    with serve(no_documents) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "SKIP", proc.stdout
    assert proc.returncode == 0, "an empty vault must not fail the preflight run"


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_discover_broken() -> None:
    """The control that keeps the empty-vault SKIP honest: a NON-200 catalog
    probe is a broken endpoint, not an empty vault, and must FAIL. Without this
    split, a 500 would hide behind the same SKIP as a legitimately empty vault.
    """

    def discover_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/discover"):
            return 500, '{"error":"storage_unavailable"}', {}
        return _green(method, path, body)

    with serve(discover_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "500" in detail, detail


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_absent_document_returns_200() -> None:
    """Every real read is healthy, but the edge also 200s a document id that does
    not exist -- the reads prove nothing and must not be credited.
    """

    def absent_doc_200(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if _ABSENT_DOC in p or (p.endswith("/traverse") and _ABSENT_DOC.encode() in body):
            return 200, _DOCUMENT_BODY, {}
        return _green(method, path, body)

    with serve(absent_doc_200) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "control" in _detail(proc.stdout, "core_api_document_reads").lower()


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_on_traverse_error() -> None:
    """The graph leg is exercised independently of the content legs: traverse 500
    while the document reads stay green -> FAIL naming traverse and the code.
    """

    def traverse_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/traverse"):
            return 500, '{"error":"storage_unavailable"}', {}
        return _green(method, path, body)

    with serve(traverse_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "traverse" in detail, detail
    assert "500" in detail, detail


@_NEEDS_RUNTIME
def test_core_api_document_reads_tolerates_no_projection() -> None:
    """A document that exists but has no stored projection is a legitimate state
    on a healthy tenant -- ingestion mid-pipeline, or a document awaiting
    reabstraction -- and the headings route reports it as 404 `no_projection`.

    The probe reads whichever document a limit:1 catalog page returns, so which
    document it lands on is not the harness's to choose. Failing there would red
    a healthy tenant on a property of its first catalog row.
    """

    def no_projection(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/headings"):
            return 404, '{"code":"no_projection","message":"No projection stored"}', {}
        return _green(method, path, body)

    with serve(no_projection) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "PASS", proc.stdout
    assert "no_projection" in detail, f"the tolerated outcome is not reported: {detail}"
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_headings_reports_document_not_found() -> None:
    """The same status, a different meaning: the document read just succeeded on
    this id, so `document_not_found` from the headings route is a real fault.
    Tolerance is keyed on the error code, never on the status alone -- otherwise
    it would swallow every 404 the route can produce.
    """

    def wrong_code(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/headings"):
            return 404, '{"code":"document_not_found","message":"No such document"}', {}
        return _green(method, path, body)

    with serve(wrong_code) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "/headings" in detail, detail


@_NEEDS_RUNTIME
def test_core_api_document_reads_fails_when_headings_500s() -> None:
    """The tolerance must not have stopped the check reading the headings leg at
    all: an endpoint fault still FAILs, naming the route and the code.
    """

    def headings_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/headings"):
            return 500, '{"code":"internal"}', {}
        return _green(method, path, body)

    with serve(headings_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    detail = _detail(proc.stdout, "core_api_document_reads")
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout
    assert "/headings" in detail, detail
    assert "500" in detail, detail


@_NEEDS_RUNTIME
def test_no_projection_tolerance_is_scoped_to_the_headings_leg() -> None:
    """Only the headings route can legitimately answer `no_projection`. The same
    code from the document read means the store cannot return a record it just
    listed in the catalog -- a fault, and it must stay red.
    """

    def document_no_projection(
        method: str, path: str, body: bytes
    ) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if "/documents/" in p and not p.endswith("/headings"):
            return 404, '{"code":"no_projection","message":"No projection stored"}', {}
        return _green(method, path, body)

    with serve(document_no_projection) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_document_reads"))
    assert _verdicts(proc.stdout).get("core_api_document_reads") == "FAIL", proc.stdout


# --- core_api_parse_filename ------------------------------------------------ #
@_NEEDS_RUNTIME
def test_core_api_parse_filename_passes() -> None:
    """The pure-computation endpoint answers 200 with a parse result -> PASS.

    The assertion is on object shape only, never on a non-null title: the service
    returns an all-null response when the vault configures no
    filename_extraction pattern, which is a legitimate tenant configuration.
    """
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_parse_filename"))
    assert _verdicts(proc.stdout).get("core_api_parse_filename") == "PASS", proc.stdout


@_NEEDS_RUNTIME
def test_core_api_parse_filename_fails_when_malformed_body_accepted() -> None:
    """The request-validation control: a body whose source_type is not a
    SourceType member must be rejected. An edge that 200s it is not processing
    request bodies, so the healthy-looking parse above proves nothing.
    """

    def accepts_anything(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/parse-filename"):
            return 200, _PARSE_BODY, {}
        return _green(method, path, body)

    with serve(accepts_anything) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_parse_filename"))
    assert _verdicts(proc.stdout).get("core_api_parse_filename") == "FAIL", proc.stdout
    assert "control" in _detail(proc.stdout, "core_api_parse_filename").lower()


@_NEEDS_RUNTIME
def test_core_api_parse_filename_fails_on_non_200() -> None:
    """A 500 from the parser endpoint -> FAIL naming the observed code."""

    def parse_500(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/parse-filename"):
            return 500, '{"error":"internal"}', {}
        return _green(method, path, body)

    with serve(parse_500) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="core_api_parse_filename"))
    detail = _detail(proc.stdout, "core_api_parse_filename")
    assert _verdicts(proc.stdout).get("core_api_parse_filename") == "FAIL", proc.stdout
    assert "500" in detail, detail

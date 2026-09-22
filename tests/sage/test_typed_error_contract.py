"""Error codes select typed details on REST and the live MCP contract."""

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from sage.api.errors import InvalidLifecycleTransitionError, InvalidParameterError
from sage.build_info import VERSION_WITH_BUILD
from sage.models.schemas import ErrorResponse
from sage.models.wire import to_wire

ROOT = Path(__file__).resolve().parents[2]
META_KEY = "org.sage/errorSchema"


@lru_cache
def specification(name: str) -> dict:
    return yaml.safe_load((ROOT / "docs/fs" / name).read_text())


def validator(spec: dict) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        {"$ref": "#/components/schemas/ErrorResponse", "components": spec["components"]}
    )


@lru_cache
def live_specification() -> dict:
    from sage.app import create_app

    return create_app().openapi()


@pytest.mark.parametrize("name", ["sage/sage_core_api.openapi.yaml", "cas_app_api.openapi.yaml"])
def test_code_selects_detail_and_rejects_invalid_known_family(name: str) -> None:
    check = validator(specification(name))
    error = InvalidLifecycleTransitionError("active", "restore", ["archive"])
    wire = to_wire(ErrorResponse(code=error.code, message=error.message, detail=error.detail))
    check.validate(wire)
    for detail in ({"document_id": "bad"}, {**error.detail, "valid_actions": 42}, {}):
        with pytest.raises(jsonschema.ValidationError):
            check.validate({**wire, "detail": detail})
    with pytest.raises(jsonschema.ValidationError):
        check.validate({k: v for k, v in wire.items() if k != "detail"})
    # A rejected value is arbitrary JSON, not the valid-input type.
    invalid = InvalidParameterError("limit", {"not": "an integer"}, "integer required")
    check.validate(
        to_wire(ErrorResponse(code=invalid.code, message=invalid.message, detail=invalid.detail))
    )


def test_derived_model_selects_detail_before_union_validation() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse(
            code="invalid_lifecycle_transition", message="refused", detail={"document_id": "x"}
        )
    with pytest.raises(ValidationError):
        ErrorResponse(
            code="invalid_lifecycle_transition",
            message="refused",
            detail={"current_state": "active", "attempted_action": "restore", "valid_actions": 42},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["sage", "sage_maint"])
async def test_live_tools_list_publishes_locally_resolvable_error_contract(surface: str) -> None:
    from sage.mcp_server import build_partitioned_server
    from scripts.dump_mcp_catalog import _tool_entry

    server = build_partitioned_server(surface)
    from mcp.types import ListToolsRequest

    result = await server._mcp_server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )
    tools = result.root.tools
    assert "_meta" in result.model_dump(mode="json", by_alias=True)["tools"][0]
    assert tools
    for tool in tools:
        metadata = tool.model_dump(by_alias=True).get("_meta") or {}
        assert META_KEY in metadata
        schema = metadata[META_KEY]
        jsonschema.Draft202012Validator.check_schema(schema)
        check = jsonschema.Draft202012Validator(schema)
        check.validate(
            {
                "error": "invalid_parameter",
                "message": "bad",
                "detail": {"parameter": "limit", "value": [], "constraint": "integer"},
            }
        )
        with pytest.raises(jsonschema.ValidationError):
            check.validate(
                {"error": "invalid_parameter", "message": "bad", "detail": {"vault_id": "x"}}
            )
        assert _tool_entry(tool)["_meta"] == metadata
        assert "org.sage/errorSchema" not in (tool.outputSchema or {})


def emitted_errors() -> Iterator[Any]:
    """Exercise every concrete exception constructor, with and without optional context."""
    import inspect
    import types
    import typing
    from collections.abc import Iterable
    from datetime import datetime, timezone

    from sage.api import errors

    def sample(annotation: Any) -> Any:
        origin = typing.get_origin(annotation)
        if origin in (typing.Union, types.UnionType):
            return sample(next(a for a in typing.get_args(annotation) if a is not type(None)))
        if origin in (list, set, Iterable):
            values = [sample(typing.get_args(annotation)[0])]
            return set(values) if origin is set else values
        if origin is dict:
            key_type, value_type = typing.get_args(annotation)
            return {sample(key_type): sample(value_type)}
        return {
            str: "value",
            bool: True,
            int: 7,
            object: {"arbitrary": [None, True, 42]},
            dict: {"custom": 1},
            datetime: datetime(2026, 1, 1, tzinfo=timezone.utc),
        }[annotation]

    for name, cls in vars(errors).items():
        if (
            not isinstance(cls, type)
            or not issubclass(cls, errors.SAGEError)
            or cls is errors.SAGEError
        ):
            continue
        signature = inspect.signature(cls)
        hints = typing.get_type_hints(cls.__init__)
        for with_optional in (False, True):
            args = {
                key: sample(hints[key])
                for key, parameter in signature.parameters.items()
                if with_optional or parameter.default is inspect.Parameter.empty
            }
            if name == "DocumentNotFoundError":
                args.pop("detail", None)
            if name == "InvalidTypedAliasError":
                args.update(code="invalid_vault_id", argument="vault_id")
            if name == "MissingFieldError":
                args["field"] = "query"
            if name == "UnexpectedFieldError":
                args["field"] = "successor_id"
            if name == "RelocationProvenanceMismatchError":
                args["field"] = "relocated_to"
            if name in ("ListFieldAddConflictError", "ListFieldRemoveConflictError"):
                args["field"] = "tags"
            if name == "UndeclaredKeyError" and "see_also" in args:
                args["see_also"] = [
                    {"key": "value", "operation": "update_lifecycles", "location": "items[].action"}
                ]
            if name == "ModeParameterMismatchError":
                args["detail"] = {
                    "mode": "catalog",
                    "target": "documents",
                    "forbidden_param": "query",
                    "allowed_modes": ["semantic", "keyword"],
                }
            yield pytest.param(cls(**args), id=f"{name}-optional-{with_optional}")
    yield pytest.param(
        errors.DocumentNotFoundError(
            "bad",
            {
                "document_id": "bad",
                "id_well_formed": False,
                "ever_existed": False,
                "slug_matches_catalog": False,
            },
        ),
        id="read-diagnostics",
    )
    yield pytest.param(
        errors.InvalidTypedAliasError(
            "invalid_sha256", "files.2.sha256", {"malformed": None}, "sha256 digest"
        ),
        id="batch-digest-location",
    )
    yield pytest.param(
        errors.InvalidTypedAliasError("invalid_sha256", "sha256", "malformed", "sha256 digest"),
        id="single-digest-location",
    )
    yield pytest.param(
        errors.RelocationProvenanceMismatchError("relocated_from", "sha256:aa", "sha256:bb"),
        id="relocation-destination-half",
    )


@pytest.mark.parametrize("error", list(emitted_errors()))
def test_every_emitted_family_round_trips_and_is_reachable(error: Any) -> None:
    from sage.mcp_server import _error_response
    from sage.models.error_contract import CODE_SCHEMAS

    assert error.code in CODE_SCHEMAS, "a concrete emitted family cannot use the extension fallback"
    expected = {
        "code": error.code,
        "message": error.message,
        "read_meta": {
            "success": False,
            "body_present": False,
            "server_build": VERSION_WITH_BUILD,
        },
    }
    if error.detail is not None:
        expected["detail"] = error.detail
    actual = to_wire(ErrorResponse(code=error.code, message=error.message, detail=error.detail))
    assert actual == expected
    for spec in (
        specification("sage/sage_core_api.openapi.yaml"),
        specification("cas_app_api.openapi.yaml"),
        live_specification(),
    ):
        check = validator(spec)
        check.validate(actual)
        if error.detail:
            with pytest.raises(jsonschema.ValidationError):
                check.validate({**actual, "detail": {"fabricated_detail_key": 1}})
    mcp_expected = {"error": error.code, "message": error.message}
    if error.detail is not None:
        mcp_expected["detail"] = error.detail
    assert _error_response(error) == mcp_expected


def _optional_detail_keys(code: str) -> set[str]:
    """The detail keys a family declares without requiring them."""
    from sage.models.error_contract import CODE_SCHEMAS, SCHEMAS

    def resolve(node: dict) -> dict:
        return SCHEMAS[node["$ref"].rsplit("/", 1)[-1]] if "$ref" in node else node

    envelope = resolve({"$ref": CODE_SCHEMAS[code]})
    detail = resolve(envelope.get("properties", {}).get("detail", {}))
    optional: set[str] = set()
    for arm in detail.get("anyOf", detail.get("oneOf", [detail])):
        arm = resolve(arm)
        optional |= set(arm.get("properties", {})) - set(arm.get("required", []))
    return optional


def test_every_declared_optional_detail_key_is_emitted() -> None:
    """The reverse of the round-trip gate: a family declares no key nothing sets.

    A key is optional because a constructor sets it only when given the
    argument that carries it. ``emitted_errors`` builds every constructor a
    second time with each defaulted argument supplied, so a conditional key is
    covered by the constructor's own optional parameter rather than listed as
    an exception. A key no constructor can set is a contract wider than the
    behaviour, which a generated client would branch on for nothing.

    Where one constructor serves several codes, chosen by an argument, the
    reflection above builds one of them and a hand-written emission builds
    each other. Such a constructor refuses an optional argument whose key the
    chosen code does not declare, so the hand-written emission omitting it is
    the only shape that code can take rather than one the fixture chose.
    """
    from sage.models.error_contract import CODE_SCHEMAS

    # The resolver reads optional keys at all: through a $ref, through an
    # inline union, and not the required ones. A resolver answering empty
    # would pass everything below.
    assert "hint" in _optional_detail_keys("invalid_parameter")
    assert "parameter" not in _optional_detail_keys("invalid_parameter")
    assert "sha256" in _optional_detail_keys("invalid_sha256")

    emitted: dict[str, set[str]] = {}
    for param in emitted_errors():
        (error,) = param.values
        emitted.setdefault(error.code, set()).update(error.detail or {})
    never_set = sorted(
        (code, key)
        for code in CODE_SCHEMAS
        for key in _optional_detail_keys(code)
        if key not in emitted.get(code, set())
    )
    assert never_set == [], f"declared optional detail keys no emission sets: {never_set}"


def test_the_destination_half_refuses_a_second_digest() -> None:
    """``relocated_from_provenance_mismatch`` declares one digest, so its
    constructor refuses a second rather than emitting a key the family forbids.

    The origin half, which does account for two, still carries it.
    """
    from sage.api.errors import RelocationProvenanceMismatchError

    with pytest.raises(ValueError):
        RelocationProvenanceMismatchError(
            "relocated_from", "sha256:aa", "sha256:bb", also_accounted="sha256:cc"
        )
    origin = RelocationProvenanceMismatchError(
        "relocated_to", "sha256:aa", "sha256:bb", also_accounted="sha256:cc"
    )
    assert origin.detail["also_accounted_content_hash"] == "sha256:cc"


def test_packaged_projection_and_application_mirror_match_authority() -> None:
    from sage.models.error_contract import SCHEMAS
    from scripts.generate_error_contract import build_contract

    assert SCHEMAS == build_contract()
    app = specification("cas_app_api.openapi.yaml")["components"]["schemas"]
    assert {name: app[name] for name in SCHEMAS} == SCHEMAS


def test_live_rest_openapi_and_model_schema_publish_the_discriminated_envelope() -> None:
    from fastapi import FastAPI

    from sage.models.schemas import _ERROR_ENVELOPE_ADAPTER

    app = FastAPI()

    @app.get("/probe", responses={400: {"model": ErrorResponse}})
    def probe() -> dict:
        return {}

    spec = app.openapi()
    check = validator(spec)
    check.validate(
        {
            "code": "invalid_parameter",
            "message": "bad",
            "detail": {"parameter": "x", "constraint": "integer", "value": None},
        }
    )
    bad = {"code": "invalid_parameter", "message": "bad", "detail": {"wrong": 1}}
    with pytest.raises(jsonschema.ValidationError):
        check.validate(bad)
    with pytest.raises(ValidationError):
        _ERROR_ENVELOPE_ADAPTER.validate_python(bad)
    # A downstream extension cannot rescue a malformed known code.
    custom = {"code": "custom_extension", "message": "bad", "detail": {"custom": None}}
    check.validate(custom)
    assert to_wire(ErrorResponse(**custom))["detail"] == {"custom": None}


@pytest.mark.parametrize("surface", ["sage", "sage_maint"])
@pytest.mark.asyncio
async def test_protocol_error_metadata_reaches_named_detail_fields(surface: str) -> None:
    from sage.mcp_server import build_partitioned_server
    from sage.models.error_contract import tool_error_schema

    tools = await build_partitioned_server(surface).list_tools()
    for tool in tools:
        schema = tool.meta[META_KEY]
        assert schema == tool_error_schema(tool.description or "", tool.name)

        # Every reference is local and resolvable, including unused optional arms.
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "$ref" in node:
                    assert node["$ref"].startswith("#/$defs/")
                    assert node["$ref"].split("/")[-1] in schema["$defs"]
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(schema)


def test_nullable_required_and_optional_context_preserves_existing_wire() -> None:
    from sage.api.errors import ForceReingestPinMismatchError, HeadingNotFoundError

    error = ForceReingestPinMismatchError("id", None, "digest", None)
    body = to_wire(ErrorResponse(code=error.code, message=error.message, detail=error.detail))
    assert body["detail"] == {
        "document_id": "id",
        "pinned_source_content_hash": None,
        "source_content_hash": "digest",
        "existing_document_id": None,
    }
    error = HeadingNotFoundError("heading", "id", available_headings=[], candidate_matches=["near"])
    body = to_wire(ErrorResponse(code=error.code, message=error.message, detail=error.detail))
    assert body["detail"] == {
        "heading_path": "heading",
        "document_id": "id",
        "available_headings": [],
        "candidate_matches": ["near"],
    }
    error = HeadingNotFoundError("heading", "id")
    body = to_wire(ErrorResponse(code=error.code, message=error.message, detail=error.detail))
    assert body["detail"] == {"heading_path": "heading", "document_id": "id"}


def test_union_conformance_walkers_reject_missing_fields_descriptions_and_refs() -> None:
    from sage.models.schemas import _ERROR_MODELS
    from tests.sage.test_openapi_conformance import (
        _check_pydantic_yaml_description_parity,
        _flatten_yaml_properties,
    )

    spec = specification("sage/sage_core_api.openapi.yaml")
    fields = _flatten_yaml_properties(spec["components"]["schemas"]["ErrorResponse"], spec)
    assert {"code", "message", "detail", "read_meta"} <= fields.keys()
    correct = {"InvalidParameterErrorDetail": {"constraint": "Constraint."}}
    assert _check_pydantic_yaml_description_parity(correct, _ERROR_MODELS, "core") == []
    wrong = {"InvalidParameterErrorDetail": {"constraint": "An invented description."}}
    assert _check_pydantic_yaml_description_parity(wrong, _ERROR_MODELS, "core")
    assert "constraint" in _ERROR_MODELS["InvalidParameterErrorDetail"].model_fields
    with pytest.raises(ValidationError):
        _ERROR_MODELS["InvalidParameterErrorDetail"].model_validate({"parameter": "x", "value": 1})
    import copy

    broken = copy.deepcopy(spec)
    del broken["components"]["schemas"]["InvalidParameterErrorDetail"]
    with pytest.raises(
        jsonschema.exceptions._WrappedReferencingError, match="InvalidParameterErrorDetail"
    ):
        validator(broken).validate(
            {
                "code": "invalid_parameter",
                "message": "bad",
                "detail": {"parameter": "x", "value": 1, "constraint": "bad"},
            }
        )


def test_unknown_extension_without_detail_preserves_omission() -> None:
    body = to_wire(ErrorResponse(code="downstream_extension", message="bad"))
    assert "detail" not in body
    for spec in (specification("sage/sage_core_api.openapi.yaml"), live_specification()):
        validator(spec).validate(body)


def test_root_strictness_control_rejects_a_permissive_mapping() -> None:
    from pydantic import RootModel

    from tests.sage.test_rest_request_strictness_conformance import _root_refuses_unknown_keys

    assert not _root_refuses_unknown_keys(RootModel[dict[str, Any]])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,canonical,alias,extra",
    [
        ("get_document", "document_id", "doc_id", {}),
        ("read_projection", "document_id", "doc_id", {}),
        ("read_section", "document_id", "doc_id", {"heading_path": ""}),
        ("list_headings", "document_id", "doc_id", {}),
        ("traverse", "start_id", "document_id", {}),
        ("chain", "document_id", "doc_id", {"edge_type": "supersedes"}),
    ],
)
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_advertised_schema_accepts_actual_identifier_refusals(
    tool_name: str,
    canonical: str,
    alias: str,
    extra: dict,
    ambiguous: bool,
) -> None:
    import json

    from sage.mcp_server import build_partitioned_server

    server = build_partitioned_server("sage")
    tool = next(tool for tool in await server.list_tools() if tool.name == tool_name)
    arguments = {"vault_id": "test", **extra}
    if ambiguous:
        arguments.update({canonical: "12345678_document", alias: "87654321_document"})
    content = await server.call_tool(tool_name, arguments)
    body = json.loads(content[0].text)
    assert body["error"] == (
        "ambiguous_document_identifier" if ambiguous else "missing_document_identifier"
    )
    jsonschema.Draft202012Validator(tool.meta[META_KEY]).validate(body)


@pytest.mark.asyncio
async def test_cross_tool_prose_does_not_declare_an_unreachable_refusal() -> None:
    from sage.mcp_server import build_partitioned_server

    tools = {tool.name: tool for tool in await build_partitioned_server("sage").list_tools()}
    schema = tools["list_headings"].meta[META_KEY]
    codes = {
        schema["$defs"][ref["$ref"].rsplit("/", 1)[-1]]["properties"]["error"]["const"]
        for ref in schema["oneOf"]
    }
    assert "document_not_found" in codes
    assert "no_projection" in codes
    assert "heading_not_found" not in codes


def test_live_discriminator_resolves_every_authoritative_code() -> None:
    source = specification("sage/sage_core_api.openapi.yaml")["components"]["schemas"]
    live = live_specification()["components"]["schemas"]
    discriminator = live["ErrorResponse"]["discriminator"]
    assert discriminator["propertyName"] == "code"
    assert set(discriminator["mapping"]) == set(source["ErrorResponse"]["discriminator"]["mapping"])
    for code, ref in discriminator["mapping"].items():
        assert ref.startswith("#/components/schemas/")
        assert live[ref.rsplit("/", 1)[-1]]["properties"]["code"]["const"] == code
    # Unknown extensions are deliberately outside the closed known-code mapping.
    assert "downstream_extension" not in discriminator["mapping"]
    validator(live_specification()).validate({"code": "downstream_extension", "message": "bad"})


def test_root_strictness_requires_an_accepted_specimen_for_every_branch() -> None:
    from pydantic import BaseModel, ConfigDict, RootModel

    from tests.sage.test_rest_request_strictness_conformance import _root_refuses_unknown_keys

    class Permissive(BaseModel):
        model_config = ConfigDict(extra="allow")
        document_id: str

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        heading_path: str

    for annotation in (Permissive, Permissive | Strict, Strict | Permissive):
        model = RootModel[annotation]
        admitted = model.model_validate({"document_id": "12345678_document", "fabricated_key": 1})
        assert admitted.model_dump()["fabricated_key"] == 1
        assert not _root_refuses_unknown_keys(model)
    assert _root_refuses_unknown_keys(RootModel[Strict])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,extra",
    [
        ("get_document", {}),
        ("read_projection", {}),
        ("read_section", {"heading_path": ""}),
        ("list_headings", {}),
    ],
)
async def test_advertised_schema_accepts_actual_missing_document_service_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    extra: dict,
) -> None:
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sage import mcp_server
    from sage.services.documents import DocumentsService
    from sage.services.utilities import UtilitiesService

    graph = SimpleNamespace(
        get_document=AsyncMock(return_value=None), list_all_documents=AsyncMock(return_value=[])
    )
    services = SimpleNamespace(
        documents_service=DocumentsService(graph, None),
        utilities_service=UtilitiesService(graph, None, None, None),
    )
    monkeypatch.setitem(mcp_server._vaults, "test", services)
    server = mcp_server.build_partitioned_server("sage")
    tool = next(tool for tool in await server.list_tools() if tool.name == tool_name)
    content = await server.call_tool(
        tool_name,
        {
            "vault_id": "test",
            "document_id": "12345678_document",
            **extra,
        },
    )
    body = json.loads(content[0].text)
    assert body["error"] == "document_not_found"
    assert body["detail"]["ever_existed"] is False
    graph.get_document.assert_awaited_with("12345678_document")
    jsonschema.Draft202012Validator(tool.meta[META_KEY]).validate(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["sage", "sage_maint"])
async def test_mcp_discriminator_mappings_select_every_advertised_family(surface: str) -> None:
    from sage.mcp_server import build_partitioned_server

    for tool in await build_partitioned_server(surface).list_tools():
        schema = tool.meta[META_KEY]
        mapping = schema["discriminator"]["mapping"]
        assert set(mapping.values()) == {branch["$ref"] for branch in schema["oneOf"]}
        for code, ref in mapping.items():
            assert ref.startswith("#/$defs/")
            assert schema["$defs"][ref.rsplit("/", 1)[-1]]["properties"]["error"]["const"] == code


def test_unregistered_error_contract_cannot_silently_publish_common_families() -> None:
    from sage.models.error_contract import tool_error_schema

    with pytest.raises(KeyError, match="undeclared_tool"):
        tool_error_schema("invalid_parameter", "undeclared_tool")

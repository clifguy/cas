"""An empty error ``detail`` is an absent one, on every surface that renders it.

``detail``'s keys vary by error code, so an empty dict carries nothing the code
has not already said, and the published Core API contract states that ``detail``
is omitted when empty. The rule has one home -- ``SAGEError`` normalizes an
empty ``detail`` to null where the error is raised -- and every renderer then
applies the ordinary null rule from ``sage.models.wire``.

Four renderers carry an error's ``detail`` to a caller: the MCP envelope, the
Core API exception handler, the per-item envelope of the bulk operations, and
the per-file entry of a batch ingest. Each is exercised at an empty, a
populated, and a null ``detail``. The populated case is the positive control: a
fix that dropped ``detail`` everywhere would pass the empty case alone.

Key presence is asserted with ``in``, never through ``.get``, which reads a
missing key and an explicit null alike.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from sage.api.errors import SAGEError, register_exception_handlers, validation_error_envelope
from sage.mcp_server import _error_response
from sage.models.wire import to_wire
from sage.services._bulk_envelope import sage_error_to_envelope
from sage.services.batch_ingest import FileDescriptor, _error_entry

DETAIL_VALUES = [
    pytest.param({}, id="empty"),
    pytest.param({"k": 1}, id="populated"),
    pytest.param(None, id="null"),
]


def _probe_error(detail: dict | None) -> SAGEError:
    return SAGEError("probe_code", "probe message", 400, detail=detail)


async def _render_rest(detail: dict | None) -> dict:
    """Raise the probe error from a route and return the Core API body."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe")
    async def probe() -> None:
        raise _probe_error(detail)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/probe")
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "probe_code", body
    return body


def _render_mcp(detail: dict | None) -> dict:
    return _error_response(_probe_error(detail))


def _render_bulk(detail: dict | None) -> dict:
    return sage_error_to_envelope(_probe_error(detail))


def _render_batch_ingest(detail: dict | None) -> dict:
    fd = FileDescriptor(file_path="/in/probe.md", source_type="markdown")
    return to_wire(_error_entry(0, "probe.md", fd, _probe_error(detail)))


SYNC_RENDERERS = [
    pytest.param(_render_mcp, id="mcp"),
    pytest.param(_render_bulk, id="bulk"),
    pytest.param(_render_batch_ingest, id="batch_ingest"),
]


def test_an_empty_detail_is_normalized_to_null_at_construction():
    """The rule's single home: the error itself, before any renderer sees it."""
    assert _probe_error({}).detail is None


@pytest.mark.parametrize("render", SYNC_RENDERERS)
def test_an_empty_detail_is_omitted(render):
    """An empty detail reaches no caller as an empty object."""
    payload = render({})
    assert "detail" not in payload, payload


async def test_an_empty_detail_is_omitted_on_the_core_api():
    """The Core API handler omits it too, as its contract says."""
    body = await _render_rest({})
    assert "detail" not in body, body


@pytest.mark.parametrize("render", SYNC_RENDERERS)
def test_a_populated_detail_is_carried(render):
    """Positive control: the omission is confined to the empty case."""
    assert render({"k": 1})["detail"] == {"k": 1}


async def test_a_populated_detail_is_carried_on_the_core_api():
    """Positive control on the Core API."""
    assert (await _render_rest({"k": 1}))["detail"] == {"k": 1}


@pytest.mark.parametrize("render", SYNC_RENDERERS)
def test_a_null_detail_is_omitted_not_sent_as_null(render):
    """No renderer emits ``"detail": null``."""
    payload = render(None)
    assert "detail" not in payload, payload


async def test_a_null_detail_is_omitted_on_the_core_api():
    """The Core API omits an optional null."""
    body = await _render_rest(None)
    assert "detail" not in body, body


@pytest.mark.parametrize("detail", DETAIL_VALUES)
async def test_mcp_and_core_api_agree_on_detail(detail):
    """The two transports carry ``detail`` identically."""
    mcp = _render_mcp(detail)
    rest = await _render_rest(detail)
    assert ("detail" in mcp) == ("detail" in rest), (mcp, rest)
    if "detail" in mcp:
        assert mcp["detail"] == rest["detail"]


class _MismatchWithoutContext(BaseModel):
    """Raises the ``mode_parameter_mismatch`` custom error with no context.

    Its translation builds ``detail`` by filtering the error's context, so an
    error raised without one is the path that yields an empty dict.
    """

    @model_validator(mode="after")
    def _reject(self) -> _MismatchWithoutContext:
        raise PydanticCustomError("mode_parameter_mismatch", "probe mismatch")


def test_a_context_free_mode_mismatch_carries_no_detail():
    """The one translation that can build an empty detail yields none."""
    with pytest.raises(ValidationError) as caught:
        _MismatchWithoutContext()

    envelope = validation_error_envelope(caught.value)
    assert envelope.code == "mode_parameter_mismatch"
    assert envelope.detail is None

    payload = _error_response(caught.value)
    assert payload["error"] == "mode_parameter_mismatch"
    assert "detail" not in payload, payload

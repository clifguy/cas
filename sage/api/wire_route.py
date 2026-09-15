"""A route class that sends a response model under the published null rule.

The published contracts decide whether a key appears in a response body: a
field declared required keeps its key when null, and an optional one is
omitted (``sage.models.wire``). FastAPI's own response serialization keeps
every null, so a model returned through it has a different wire shape on the
Core API than on the MCP surface. This route class renders the declared
response model through the same rule instead.

Only the body changes. The value is validated against the route's declared
response model and dumped as that type, as FastAPI does, so a wider object
never publishes fields the contract does not declare. The route's status code,
a status or headers set on an injected ``Response``, a sync endpoint's
threadpool dispatch, and a ``Response`` the endpoint returns itself are all
kept as FastAPI keeps them, and the OpenAPI document is generated from the same
declaration.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import ResponseValidationError
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError
from starlette.responses import JSONResponse, Response

from sage.models.wire import prune_optional_nulls

__all__ = ["WireRoute", "render_response"]


@functools.cache
def _cached_adapter(annotation: Any) -> TypeAdapter:
    return TypeAdapter(annotation)


def _adapter(annotation: Any) -> TypeAdapter:
    try:
        return _cached_adapter(annotation)
    except TypeError:
        # An unhashable annotation cannot be a cache key; build it each time.
        return TypeAdapter(annotation)


def render_response(annotation: Any, value: Any) -> Any:
    """The body a route declaring ``annotation`` sends for ``value``.

    Validated and dumped as the declared type, in JSON mode and by alias, then
    pruned against the validated value: the prune reads each field's
    declaration from the objects themselves, which is what lets it follow a
    union, a list or a mapping of models.

    Raises:
        ResponseValidationError: ``value`` does not satisfy the declared type,
            the error FastAPI raises for the same failure.
    """
    adapter = _adapter(annotation)
    try:
        validated = adapter.validate_python(value, from_attributes=True)
    except ValidationError as exc:
        raise ResponseValidationError(errors=exc.errors(), body=value) from exc
    return prune_optional_nulls(
        validated, adapter.dump_python(validated, mode="json", by_alias=True)
    )


class WireRoute(APIRoute):
    """An API route whose response model is rendered by ``render_response``.

    Set as ``route_class`` on a router. A route declaring no response model, or
    whose endpoint is a generator, is left to FastAPI unchanged.
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if inspect.isgeneratorfunction(endpoint) or inspect.isasyncgenfunction(endpoint):
            super().__init__(path, endpoint, **kwargs)
            return

        route = self
        endpoint_is_coroutine = inspect.iscoroutinefunction(endpoint)

        # ``functools.wraps`` keeps ``__wrapped__``, through which FastAPI reads
        # the endpoint's signature, its module globals for string annotations,
        # its docstring and its name. The wrapper itself is a coroutine
        # function, which is how FastAPI decides to await it.
        @functools.wraps(endpoint)
        async def render(*args: Any, **values: Any) -> Any:
            if endpoint_is_coroutine:
                result = await endpoint(*args, **values)
            else:
                result = await run_in_threadpool(endpoint, *args, **values)
            if isinstance(result, Response) or route.response_model is None:
                return result
            return route._wire_response(result, values)

        super().__init__(path, render, **kwargs)

    def _wire_response(self, result: Any, values: dict[str, Any]) -> JSONResponse:
        # A ``Response`` injected into the endpoint carries any status or
        # headers the endpoint set on it, which FastAPI applies to the response
        # it builds; a response returned directly would drop them.
        injected = next((v for v in values.values() if isinstance(v, Response)), None)
        status_code = self.status_code or 200
        if injected is not None and injected.status_code:
            status_code = injected.status_code
        response = JSONResponse(
            render_response(self.response_model, result), status_code=status_code
        )
        if injected is not None:
            response.headers.raw.extend(
                (k, v) for k, v in injected.headers.raw if k.lower() != b"content-length"
            )
        return response

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
a status, header or cookie set on the per-request ``Response`` by the endpoint
or by any dependency, a sync endpoint's threadpool dispatch, and a ``Response``
the endpoint returns itself are all kept as FastAPI keeps them, and the OpenAPI
document is generated from the same declaration. The options FastAPI offers for
shaping a body it serializes itself -- field include and exclude, alias and
unset-field control, a non-JSON response class -- have no effect on a body
rendered here, so a route declaring one is refused when it is built rather than
left to behave as though the option were absent.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from fastapi.concurrency import run_in_threadpool
from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.utils import get_typed_return_annotation, get_typed_signature
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
            the error FastAPI raises for the same failure. The error carries
            where and why the value was refused and none of the value itself,
            so reporting it never writes the server's data to a log.
    """
    adapter = _adapter(annotation)
    try:
        validated = adapter.validate_python(value, from_attributes=True)
    except ValidationError as exc:
        raise ResponseValidationError(
            errors=exc.errors(include_input=False, include_url=False)
        ) from exc
    return prune_optional_nulls(
        validated, adapter.dump_python(validated, mode="json", by_alias=True)
    )


# The name under which the per-request response is injected into an endpoint
# that does not declare one itself.
_INJECTED_RESPONSE = "_wire_route_response"


def _response_parameter(signature: inspect.Signature) -> str | None:
    """The name of a parameter the endpoint declares to receive the response."""
    for parameter in signature.parameters.values():
        annotation = parameter.annotation
        if isinstance(annotation, type) and issubclass(annotation, Response):
            return parameter.name
    return None


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

        # FastAPI hands one per-request response to the endpoint and to every
        # dependency, and fills it into the parameter annotated ``Response``.
        # FastAPI records one such parameter per callable, so an endpoint that
        # declares its own is read through that one; otherwise the wrapper
        # declares a keyword-only parameter of its own and withholds it from the
        # endpoint.
        signature = get_typed_signature(endpoint)
        declared = _response_parameter(signature)
        response_name = declared or _INJECTED_RESPONSE

        # ``functools.wraps`` keeps the endpoint's name and docstring, which
        # FastAPI reads for the operation. The wrapper itself is a coroutine
        # function, which is how FastAPI decides to await it.
        @functools.wraps(endpoint)
        async def render(*args: Any, **values: Any) -> Any:
            injected = values[response_name] if declared else values.pop(response_name)
            if endpoint_is_coroutine:
                result = await endpoint(*args, **values)
            else:
                result = await run_in_threadpool(endpoint, *args, **values)
            if isinstance(result, Response) or route.response_model is None:
                return result
            return route._wire_response(result, injected)

        parameters = list(signature.parameters.values())
        if declared is None:
            parameters.append(
                inspect.Parameter(
                    _INJECTED_RESPONSE, inspect.Parameter.KEYWORD_ONLY, annotation=Response
                )
            )
        return_annotation = get_typed_return_annotation(endpoint)
        render.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            parameters,
            return_annotation=(
                inspect.Signature.empty if return_annotation is None else return_annotation
            ),
        )

        super().__init__(path, render, **kwargs)
        if self.response_model is not None:
            self._refuse_unapplied_options()

    def _refuse_unapplied_options(self) -> None:
        """Refuse a declared option that rendering the body here would ignore."""
        unapplied = [
            name
            for name, value, default in (
                ("response_model_include", self.response_model_include, None),
                ("response_model_exclude", self.response_model_exclude, None),
                ("response_model_by_alias", self.response_model_by_alias, True),
                ("response_model_exclude_unset", self.response_model_exclude_unset, False),
                ("response_model_exclude_defaults", self.response_model_exclude_defaults, False),
                ("response_model_exclude_none", self.response_model_exclude_none, False),
            )
            if value != default
        ]
        response_class = self.response_class
        if isinstance(response_class, DefaultPlaceholder):
            response_class = response_class.value
        if not issubclass(response_class, JSONResponse):
            unapplied.append("response_class")
        if unapplied:
            raise ValueError(
                f"{self.path}: WireRoute renders the response body itself and does not "
                f"apply {', '.join(unapplied)}; declare the route without them"
            )

    def _wire_response(self, result: Any, injected: Response) -> JSONResponse:
        # The per-request response carries any status, header or cookie the
        # endpoint or a dependency set on it, which FastAPI applies to the
        # response it builds; a response returned directly would drop them.
        status_code = injected.status_code or self.status_code or 200
        response = JSONResponse(
            render_response(self.response_model, result), status_code=status_code
        )
        response.headers.raw.extend(
            (k, v) for k, v in injected.headers.raw if k.lower() != b"content-length"
        )
        return response

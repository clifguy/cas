#!/usr/bin/env python3
"""Headless MCP round-trip probe for the cloud preflight gate.

Drives a real JSON-RPC handshake over the SAGE MCP Streamable HTTP transport
so the post-deploy preflight can prove the maintenance and ordinary MCP
surfaces are not merely reachable but actually *processing* requests through
the edge.

The transport is the single-endpoint Streamable HTTP form -- the one a
standards MCP client speaks: every JSON-RPC message is a ``POST`` to the mount
URL itself (the byte-exact, slash-less path the edge's protected-resource
metadata advertises as the OAuth resource), and the response arrives in the
POST response body -- as a plain JSON document, or as a short SSE-framed body
on a server that streams its POST responses; the probe accepts both framings.

Redirects are refused, loudly. A ``3xx`` on the initialize POST is the
signature of a backend that cannot serve the exact mount path (the
trailing-slash-redirect class of failure) -- a real MCP client does not follow
a POST redirect, so the probe must not either. Python's default urllib opener
would silently follow a ``301/302/303`` and convert the POST into a GET,
crediting whatever 200-shaped page it lands on; the probe installs a
redirect-refusing opener so every ``3xx`` surfaces as a named
``redirect_refused`` verdict instead.

Two modes:

* ``handshake`` -- ``initialize`` only; success means the surface completed the
  MCP handshake. Used to prove an authenticated maintenance call reaches the
  backend.
* ``roundtrip`` -- ``initialize`` + ``tools/list`` (a well-formed result) + an
  unknown JSON-RPC method that must come back as a JSON-RPC ``error`` object.
  That last leg is an anti-coincidental negative control: it proves the surface
  parses and dispatches requests rather than blanket-statusing them. (An unknown
  *tool name* would come back as a normal result envelope, not an ``error`` --
  only an unknown *method* yields a JSON-RPC error, so the control uses one.)

Standard library only -- the gate runs on a stock CI runner without the project
virtualenv, so this imports nothing outside the standard library. The bearer
token is read from ``AUTH_TOKEN`` in the environment (never argv, so it never
lands in the process table); an empty token sends no ``Authorization`` header,
which is how an auth-off server is driven in-process.

Output: a single ``key=value`` verdict line on stdout. Exit ``0`` iff the mode's
assertions all held; non-zero otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

_INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "cas-preflight", "version": "1.0"},
    },
}
_INITIALIZED: dict[str, Any] = {"jsonrpc": "2.0", "method": "notifications/initialized"}
_TOOLS_LIST: dict[str, Any] = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
#: An unknown JSON-RPC *method* (not an unknown tool) -- the only shape that
#: elicits a JSON-RPC ``error`` object, which is what the negative control needs.
_UNKNOWN_METHOD: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "preflight/does-not-exist",
    "params": {},
}


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every 3xx into an ``HTTPError`` instead of following it.

    The default handler follows redirects -- and downgrades a redirected POST
    to a GET on 301/302/303 -- which would let a redirecting mount masquerade
    as a served transport. Returning ``None`` makes urllib raise the original
    3xx as an ``HTTPError``, which the caller names in its verdict.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _parse_body(body: bytes, content_type: str) -> dict[str, Any] | None:
    """Extract the JSON-RPC message from a POST response body.

    ``application/json`` is the plain-document framing. ``text/event-stream``
    is the framing of a server that streams its POST responses: scan the
    ``data:`` lines for the first JSON object carrying an ``id``.
    """
    if content_type.startswith("application/json"):
        try:
            msg = json.loads(body)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None
    if content_type.startswith("text/event-stream"):
        for line in body.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[len("data:") :].strip())
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") is not None:
                return msg
    return None


class _PostFailure(Exception):
    """A POST that did not yield a JSON-RPC message; carries the verdict detail."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _post(
    url: str,
    auth: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    session_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST one JSON-RPC message; return ``(parsed_response, session_id)``.

    A notification (no ``id``) expects a bodyless ``202``-class ack and
    returns ``(None, session_id)``. The session id is echoed back on every
    subsequent request when the server minted one (a stateless server does
    not; both are valid). Raises ``_PostFailure`` with a self-diagnosing
    verdict fragment on any HTTP error -- including every refused ``3xx``.
    """
    headers = dict(auth)
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read()
            new_session = resp.headers.get("Mcp-Session-Id") or session_id
            if payload.get("id") is None:
                return None, new_session
            msg = _parse_body(body, resp.headers.get("Content-Type", ""))
            return msg, new_session
    except urllib.error.HTTPError as exc:
        # Surface the response body, not just the status: an edge rejection
        # (e.g. a 421 ``Invalid Host header``) names its own cause, so the
        # verdict line is self-diagnosing. Collapse whitespace to one line and
        # cap the length so the verdict stays a single key=value record. A
        # refused redirect carries its own name plus the Location target.
        if 300 <= exc.code < 400:
            location = exc.headers.get("Location", "?") if exc.headers else "?"
            raise _PostFailure(
                f"post_status={exc.code} redirect_refused location={location}"
            ) from exc
        body_text = " ".join(exc.read().decode("utf-8", "replace").split())[:120]
        raise _PostFailure(f"post_status={exc.code} post_body={body_text}") from exc
    except Exception as exc:  # noqa: BLE001
        raise _PostFailure(f"post_error={type(exc).__name__}") from exc


def _init_ok(msg: dict[str, Any] | None) -> bool:
    return (
        isinstance(msg, dict)
        and isinstance(msg.get("result"), dict)
        and isinstance(msg["result"].get("serverInfo"), dict)
    )


def _server_name(msg: dict[str, Any] | None) -> str:
    if isinstance(msg, dict) and isinstance(msg.get("result"), dict):
        info = msg["result"].get("serverInfo")
        if isinstance(info, dict):
            return str(info.get("name", "?"))
    return "?"


def _tools_list_ok(msg: dict[str, Any] | None) -> bool:
    return (
        isinstance(msg, dict)
        and isinstance(msg.get("result"), dict)
        and isinstance(msg["result"].get("tools"), list)
        and len(msg["result"]["tools"]) > 0
    )


def _is_error(msg: dict[str, Any] | None) -> bool:
    return isinstance(msg, dict) and isinstance(msg.get("error"), dict) and "code" in msg["error"]


def _emit(mode: str, mount: str, detail: str) -> None:
    print(f"mode={mode} mount={mount} {detail}")


def probe(base_url: str, mount: str, mode: str, timeout: float) -> int:
    """Run the probe; return an exit code (0 = all assertions held)."""
    mount = "/" + mount.strip("/")
    url = base_url.rstrip("/") + mount
    token = os.environ.get("AUTH_TOKEN", "")
    auth: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        init, session_id = _post(url, auth, _INITIALIZE, timeout, None)
        if not _init_ok(init):
            _emit(mode, mount, "initialize=fail")
            return 1
        server = _server_name(init)
        try:
            _post(url, auth, _INITIALIZED, timeout, session_id)
        except _PostFailure:
            # The initialized notification is best-effort: some servers close
            # the ack early. The load-bearing assertions are the id-carrying
            # round-trips before and after it.
            pass

        if mode == "handshake":
            _emit(mode, mount, f"initialize=ok server={server}")
            return 0

        tools, session_id = _post(url, auth, _TOOLS_LIST, timeout, session_id)
        if not _tools_list_ok(tools):
            _emit(mode, mount, "initialize=ok tools_list=fail")
            return 1

        unknown, _ = _post(url, auth, _UNKNOWN_METHOD, timeout, session_id)
        if not _is_error(unknown):
            _emit(mode, mount, "initialize=ok tools_list=ok negctrl=fail")
            return 1

        _emit(mode, mount, f"initialize=ok tools_list=ok negctrl=error server={server}")
        return 0
    except _PostFailure as failure:
        _emit(mode, mount, failure.detail)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="scheme+host of the SAGE edge")
    parser.add_argument("--mount", required=True, help="MCP mount path, e.g. /mcp or /mcp_maint")
    parser.add_argument(
        "--mode",
        choices=("handshake", "roundtrip"),
        default="roundtrip",
        help="handshake = initialize only; roundtrip = + tools/list + negative control",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="overall deadline (seconds)")
    args = parser.parse_args(argv)
    return probe(args.base_url, args.mount, args.mode, args.timeout)


if __name__ == "__main__":
    sys.exit(main())

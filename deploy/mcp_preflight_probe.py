#!/usr/bin/env python3
"""Headless MCP round-trip probe for the cloud preflight gate.

Drives a real JSON-RPC handshake over the SAGE MCP HTTP+SSE transport so the
post-deploy preflight can prove the maintenance and ordinary MCP surfaces are
not merely reachable but actually *processing* requests through the edge.

The transport is the two-endpoint Server-Sent-Events form: a ``GET`` opens an
event stream, the server's first event (``endpoint``) names a per-session
``POST`` URL, JSON-RPC requests are POSTed there (each acknowledged ``202``), and
every JSON-RPC *response* is delivered back on the open ``GET`` stream. A single
POST is therefore not a round-trip; this probe opens the stream, reads the
endpoint event, POSTs the requests, and correlates each response off the stream
by its JSON-RPC id.

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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
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


class _StreamState:
    """Shared state between the SSE reader thread and the orchestrating thread."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.endpoint: str | None = None
        self.responses: dict[Any, dict[str, Any]] = {}
        self.closed = False


def _read_stream(resp: Any, state: _StreamState) -> None:
    """Consume the SSE event stream, recording the endpoint URL and every
    JSON-RPC response (keyed by id) so the orchestrator can await them.

    SSE framing: ``event:`` / ``data:`` lines, frames separated by a blank line.
    The first frame is ``event: endpoint``; JSON-RPC responses arrive as
    subsequent (``message``) frames. A frame with no ``event:`` line defaults to
    a message.
    """
    event: str | None = None
    data_lines: list[str] = []
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data_lines:
                    _dispatch(event, "\n".join(data_lines), state)
                event = None
                data_lines = []
                continue
            if line.startswith(":"):
                continue  # SSE comment / heartbeat
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
    except Exception:  # noqa: BLE001, S110 -- a broken stream is just a closed stream here
        pass
    finally:
        with state.cond:
            state.closed = True
            state.cond.notify_all()


def _dispatch(event: str | None, data: str, state: _StreamState) -> None:
    with state.cond:
        if event == "endpoint":
            state.endpoint = data
        else:
            try:
                msg = json.loads(data)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("id") is not None:
                state.responses[msg["id"]] = msg
        state.cond.notify_all()


def _wait_endpoint(state: _StreamState, deadline: float) -> str | None:
    with state.cond:
        while state.endpoint is None and not state.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            state.cond.wait(remaining)
        return state.endpoint


def _wait_response(state: _StreamState, rid: Any, deadline: float) -> dict[str, Any] | None:
    with state.cond:
        while rid not in state.responses and not state.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            state.cond.wait(remaining)
        return state.responses.get(rid)


def _post(url: str, auth: dict[str, str], payload: dict[str, Any], timeout: float) -> None:
    """Fire a JSON-RPC message at the session endpoint. The response is delivered
    on the SSE stream, not here, so the ``202`` ack is all we read.
    """
    headers = dict(auth)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as exc:
        exc.read()
    except Exception:  # noqa: BLE001, S110 -- a failed POST surfaces as a missing response downstream
        pass


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
    sse_url = base_url.rstrip("/") + mount + "/sse"
    token = os.environ.get("AUTH_TOKEN", "")
    auth: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.monotonic() + timeout

    get_headers = dict(auth)
    get_headers["Accept"] = "text/event-stream"
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(sse_url, headers=get_headers, method="GET"), timeout=timeout
        )
    except urllib.error.HTTPError as exc:
        _emit(mode, mount, f"sse_status={exc.code}")
        return 1
    except Exception as exc:  # noqa: BLE001
        _emit(mode, mount, f"sse_open_error={type(exc).__name__}")
        return 1

    state = _StreamState()
    reader = threading.Thread(target=_read_stream, args=(resp, state), daemon=True)
    reader.start()
    try:
        endpoint = _wait_endpoint(state, deadline)
        if not endpoint:
            _emit(mode, mount, "no_endpoint_event")
            return 1
        messages_url = urllib.parse.urljoin(sse_url, endpoint)

        _post(messages_url, auth, _INITIALIZE, timeout)
        init = _wait_response(state, 1, deadline)
        if not _init_ok(init):
            _emit(mode, mount, "initialize=fail")
            return 1
        server = _server_name(init)
        _post(messages_url, auth, _INITIALIZED, timeout)

        if mode == "handshake":
            _emit(mode, mount, f"initialize=ok server={server}")
            return 0

        _post(messages_url, auth, _TOOLS_LIST, timeout)
        if not _tools_list_ok(_wait_response(state, 2, deadline)):
            _emit(mode, mount, "initialize=ok tools_list=fail")
            return 1

        _post(messages_url, auth, _UNKNOWN_METHOD, timeout)
        if not _is_error(_wait_response(state, 3, deadline)):
            _emit(mode, mount, "initialize=ok tools_list=ok negctrl=fail")
            return 1

        _emit(mode, mount, f"initialize=ok tools_list=ok negctrl=error server={server}")
        return 0
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001, S110 -- closing a spent stream best-effort
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="scheme+host of the SAGE edge")
    parser.add_argument("--mount", required=True, help="MCP mount path, e.g. /mcp or /mcp_admin")
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

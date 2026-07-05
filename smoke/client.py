"""HTTP client layer for the API smoke driver.

Wraps httpx with the three call shapes the sweep needs — plain REST,
Server-Sent Events (SSE) streams, and MCP JSON-RPC over the stateless
streamable-HTTP mounts — plus the check recorder that produces the
coverage matrix.

Standing assertion rules live here so no individual check can weaken
them: batch envelopes are always inspected per item, SSE checks always
require incremental delivery and a terminal event, and MCP success
means a non-error envelope, never a bare HTTP 200.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx

REQUEST_ID_HEADER = "x-smoke-request-id"


class CheckFailure(AssertionError):
    """A smoke assertion failed. Carries the evidence string."""


def expect(condition: bool, evidence: str) -> None:
    if not condition:
        raise CheckFailure(evidence)


def _is_tool_error(payload: dict) -> bool:
    """MCP tool error envelopes carry a string `error` (or `error_code`) slug."""
    return bool(isinstance(payload.get("error"), str) or isinstance(payload.get("error_code"), str))


@dataclass
class SseEvent:
    """One SSE data event with its arrival time (monotonic seconds)."""

    data: dict
    arrived_at: float


@dataclass
class SseStream:
    """Collected SSE events plus stream timing for buffering detection."""

    events: list[SseEvent]
    opened_at: float
    closed_at: float

    def first_event_lead(self) -> float:
        """Seconds between the first event's arrival and stream close.

        A healthy incremental stream delivers its first event well before
        the stream closes; a buffered stream delivers everything in one
        terminal flush and the lead collapses toward zero.
        """
        if not self.events:
            return 0.0
        return self.closed_at - self.events[0].arrived_at

    def payloads(self) -> list[dict]:
        return [e.data for e in self.events]


@dataclass
class CheckResult:
    check_id: str
    title: str
    surface: str  # rest | mcp | mcp_admin | bff
    db_class: str  # relational | vector | both | none
    rw: str  # read | write
    status: str  # PASS | FAIL | ERROR | SKIP
    detail: str
    covers_rest: list[tuple[str, str]]
    covers_tools: list[str]
    request_ids: list[str] = field(default_factory=list)


class Recorder:
    """Accumulates check results and coverage claims."""

    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def run(
        self,
        check_id: str,
        title: str,
        fn: Callable[[], str],
        *,
        surface: str,
        db_class: str,
        rw: str,
        covers_rest: list[tuple[str, str]] | None = None,
        covers_tools: list[str] | None = None,
        request_ids: list[str] | None = None,
    ) -> bool:
        """Execute one check. Returns True when it passed."""
        try:
            detail = fn()
            status = "PASS"
        except CheckFailure as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001 -- a crashed check must not kill the sweep
            status, detail = "ERROR", f"{type(exc).__name__}: {exc}"
        self.results.append(
            CheckResult(
                check_id=check_id,
                title=title,
                surface=surface,
                db_class=db_class,
                rw=rw,
                status=status,
                detail=detail,
                covers_rest=covers_rest or [],
                covers_tools=covers_tools or [],
                request_ids=request_ids or [],
            )
        )
        marker = {"PASS": "ok", "SKIP": "--"}.get(status, "XX")
        print(
            f"  [{marker}] {check_id} {title}: {status}"
            + ("" if status == "PASS" else f" — {detail}")
        )
        return status == "PASS"

    def skip(
        self,
        check_id: str,
        title: str,
        reason: str,
        *,
        surface: str,
        db_class: str,
        rw: str,
        covers_rest: list[tuple[str, str]] | None = None,
        covers_tools: list[str] | None = None,
    ) -> None:
        self.results.append(
            CheckResult(
                check_id=check_id,
                title=title,
                surface=surface,
                db_class=db_class,
                rw=rw,
                status="SKIP",
                detail=reason,
                covers_rest=covers_rest or [],
                covers_tools=covers_tools or [],
            )
        )
        print(f"  [--] {check_id} {title}: SKIP — {reason}")

    def claimed_rest(self) -> set[tuple[str, str]]:
        return {op for r in self.results for op in r.covers_rest}

    def claimed_tools(self) -> set[str]:
        return {t for r in self.results for t in r.covers_tools}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


class SmokeClient:
    """One client per target deployment (SAGE edge, local or cloud)."""

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: Callable[[], str | None] = lambda: None,
        timeout: float = 120.0,
    ) -> None:
        self._token_provider = token_provider
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.last_request_id: str | None = None
        self._rpc_seq = 0

    def close(self) -> None:
        self.http.close()

    def _headers(self, extra: dict | None = None) -> dict:
        self.last_request_id = str(uuid.uuid4())
        headers = {REQUEST_ID_HEADER: self.last_request_id}
        token = self._token_provider()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------- REST
    def req(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.http.request(method, path, headers=self._headers(), **kwargs)

    @staticmethod
    def body(resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise CheckFailure(
                f"non-JSON body (HTTP {resp.status_code}): {resp.text[:200]!r}"
            ) from exc

    def expect_status(self, resp: httpx.Response, *codes: int) -> dict:
        if resp.status_code not in codes:
            raise CheckFailure(f"HTTP {resp.status_code}, expected {codes}: {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return self.body(resp)

    @staticmethod
    def expect_error_code(resp: httpx.Response, status: int, error_code: str) -> dict:
        if resp.status_code != status:
            raise CheckFailure(f"HTTP {resp.status_code}, expected {status}: {resp.text[:300]}")
        body = resp.json()
        got = body.get("code")
        if got is None:
            detail = body.get("detail", {})
            got = detail.get("error_code") if isinstance(detail, dict) else None
        if got != error_code:
            raise CheckFailure(f"error code {got!r}, expected {error_code!r}: {resp.text[:300]}")
        return body

    @staticmethod
    def expect_batch(body: dict, *, success: int, errors: int = 0) -> list[dict]:
        """Per-item inspection of a bulk envelope. Never trust HTTP 200 alone."""
        expect(
            body.get("success_count") == success and body.get("error_count") == errors,
            f"batch counts success={body.get('success_count')} error={body.get('error_count')},"
            f" expected {success}/{errors}",
        )
        items = body.get("results") or body.get("items") or []
        expect(len(items) == success + errors, f"envelope items {len(items)} != {success + errors}")
        return items

    # -------------------------------------------------------------- SSE
    def sse(
        self,
        method: str,
        path: str,
        *,
        read_timeout: float = 900.0,
        **kwargs,
    ) -> SseStream:
        """POST and consume an SSE stream, timestamping each data event."""
        events: list[SseEvent] = []
        opened = time.monotonic()
        timeout = httpx.Timeout(30.0, read=read_timeout)
        with self.http.stream(
            method, path, headers=self._headers(), timeout=timeout, **kwargs
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                raise CheckFailure(f"SSE HTTP {resp.status_code}: {resp.text[:300]}")
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                resp.read()
                raise CheckFailure(
                    f"not an event stream (content-type {content_type!r}): {resp.text[:200]!r}"
                )
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    events.append(SseEvent(json.loads(line[5:].strip()), time.monotonic()))
        return SseStream(events=events, opened_at=opened, closed_at=time.monotonic())

    # -------------------------------------------------------------- MCP
    def _rpc(self, mount: str, method: str, params: dict) -> dict:
        self._rpc_seq += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_seq, "method": method, "params": params}
        headers = self._headers(
            {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        )
        resp = self.http.post(mount, json=payload, headers=headers)
        if resp.status_code != 200:
            raise CheckFailure(f"MCP {mount} HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if "error" in body:
            raise CheckFailure(f"MCP {mount} rpc error: {body['error']}")
        return body["result"]

    def mcp_initialize(self, mount: str) -> dict:
        return self._rpc(
            mount,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "cas-api-smoke", "version": "0"},
            },
        )

    def mcp_list_tools(self, mount: str) -> list[str]:
        result = self._rpc(mount, "tools/list", {})
        return sorted(t["name"] for t in result["tools"])

    def mcp_call(self, mount: str, tool: str, arguments: dict) -> dict:
        """Call a tool; return its decoded payload. Raises CheckFailure on error envelopes."""
        result = self._rpc(mount, "tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise CheckFailure(f"tool {tool} isError: {json.dumps(result)[:400]}")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            decoded = json.loads(joined) if joined else {}
        except json.JSONDecodeError:
            decoded = {"text": joined}
        if isinstance(decoded, dict) and _is_tool_error(decoded):
            raise CheckFailure(f"tool {tool} error envelope: {joined[:400]}")
        return decoded if isinstance(decoded, dict) else {"value": decoded}

    def mcp_call_expect_error(self, mount: str, tool: str, arguments: dict) -> str:
        """Call a tool expecting an error envelope; return its text."""
        result = self._rpc(mount, "tools/call", {"name": tool, "arguments": arguments})
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        structured = result.get("structuredContent")
        try:
            decoded = json.loads(joined) if joined else {}
        except json.JSONDecodeError:
            decoded = {}
        is_error = (
            result.get("isError")
            or (isinstance(structured, dict) and _is_tool_error(structured))
            or (isinstance(decoded, dict) and _is_tool_error(decoded))
        )
        if not is_error:
            raise CheckFailure(f"tool {tool} unexpectedly succeeded: {joined[:200]}")
        return joined or json.dumps(structured)


def poll(
    fn: Callable[[], dict | None],
    *,
    timeout: float,
    interval: float = 2.0,
    what: str = "condition",
) -> dict:
    """Poll fn until it returns a truthy value or the deadline passes."""
    deadline = time.monotonic() + timeout
    last: Iterator | None = None
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        last = value
        time.sleep(interval)
    raise CheckFailure(f"timed out after {timeout:.0f}s waiting for {what} (last={last!r})")

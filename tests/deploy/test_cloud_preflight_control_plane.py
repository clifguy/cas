"""The preflight's control-plane checks.

The live Postgres major, read through a seamed probe, and the check that the
retired Admin surface stays retired.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from tests.deploy._preflight_harness import (
    _NEEDS_RUNTIME,
    _base_env,
    _detail,
    _green,
    _run,
    _verdicts,
    _write_probe_stub,
    serve,
)


# --------------------------------------------------------------------------- #
# I. Live Postgres major (control-plane read through a seamed probe)           #
#                                                                             #
# The check makes no HTTP call: it runs the seamed probe and compares its      #
# reading with the declared major. Driven here through stub probes, so the     #
# PASS, FAIL, SKIP and blank-reading arms are all exercised offline.           #
# --------------------------------------------------------------------------- #
def _pg_probe(tmp_path: Path, *, stdout: str = "", exit_code: int = 0) -> str:
    """Write a stub probe printing ``stdout`` and exiting ``exit_code``."""
    script = tmp_path / "pg-probe.sh"
    script.write_text(
        f"#!/usr/bin/env bash\nprintf '%s' {shlex.quote(stdout)}\nexit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def _pg_env(url: str, **overrides: str) -> dict[str, str]:
    env = _base_env(url, PREFLIGHT_CHECKS="postgres_major")
    env["PREFLIGHT_EXPECTED_PG_MAJOR"] = "16"
    env["PREFLIGHT_RESOURCE_GROUP"] = "rg-cas-test"
    env.update(overrides)
    return env


@_NEEDS_RUNTIME
def test_postgres_major_passes_when_live_matches_declared(tmp_path: Path) -> None:
    """The probe reads the declared major off the live server -> PASS."""
    with serve(_green) as url:
        proc = _run(
            _pg_env(url, PREFLIGHT_POSTGRES_VERSION_PROBE_CMD=_pg_probe(tmp_path, stdout="16"))
        )
    assert _verdicts(proc.stdout).get("postgres_major") == "PASS", proc.stdout
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_postgres_major_fails_when_live_differs(tmp_path: Path) -> None:
    """THE regression this check exists for: the repository is self-consistent
    about one major while the deployed server runs another. Every repo-internal
    gate stays green; only a live reading reports it.
    """
    with serve(_green) as url:
        proc = _run(
            _pg_env(url, PREFLIGHT_POSTGRES_VERSION_PROBE_CMD=_pg_probe(tmp_path, stdout="17"))
        )
    detail = _detail(proc.stdout, "postgres_major")
    assert _verdicts(proc.stdout).get("postgres_major") == "FAIL", proc.stdout
    assert "17" in detail and "16" in detail, detail
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_postgres_major_fails_on_a_blank_reading(tmp_path: Path) -> None:
    """Anti-coincidental control: a probe that prints nothing but exits 0 -- a
    broken az session -- must not compare equal to the expectation. Without the
    numeric guard this arm would PASS by comparing two blanks.
    """
    with serve(_green) as url:
        proc = _run(
            _pg_env(url, PREFLIGHT_POSTGRES_VERSION_PROBE_CMD=_pg_probe(tmp_path, stdout=""))
        )
    detail = _detail(proc.stdout, "postgres_major")
    assert _verdicts(proc.stdout).get("postgres_major") == "FAIL", proc.stdout
    assert "control" in detail.lower(), detail


@_NEEDS_RUNTIME
def test_postgres_major_fails_when_the_probe_errors(tmp_path: Path) -> None:
    """A probe that cannot read a major (no server, or two) FAILs rather than
    SKIPping: an unknown deployed major is not the same claim as a correct one.
    """
    with serve(_green) as url:
        proc = _run(
            _pg_env(
                url,
                PREFLIGHT_POSTGRES_VERSION_PROBE_CMD=_pg_probe(tmp_path, stdout="", exit_code=1),
            )
        )
    assert _verdicts(proc.stdout).get("postgres_major") == "FAIL", proc.stdout


@_NEEDS_RUNTIME
def test_postgres_major_skips_without_the_seam() -> None:
    """An operator shell with no az session cannot ask; unset seam -> SKIP."""
    with serve(_green) as url:
        proc = _run(_pg_env(url))
    assert _verdicts(proc.stdout).get("postgres_major") == "SKIP", proc.stdout
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_postgres_major_skips_without_an_expectation(tmp_path: Path) -> None:
    """The seam armed but no declared major supplied -> SKIP, not a vacuous PASS
    against an empty expectation.
    """
    with serve(_green) as url:
        proc = _run(
            _pg_env(
                url,
                PREFLIGHT_POSTGRES_VERSION_PROBE_CMD=_pg_probe(tmp_path, stdout="16"),
                PREFLIGHT_EXPECTED_PG_MAJOR="",
            )
        )
    assert _verdicts(proc.stdout).get("postgres_major") == "SKIP", proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize(
    "broken",
    [
        "none",
        "metadata",
        "mount",
        "slash",
        "sse",
        "post",
        "challenge",
        "blanket404",
        "canonical",
        "authenticated",
        "anonymous",
    ],
)
def test_retired_admin_preflight_requires_live_supported_surface(
    tmp_path: Path, broken: str
) -> None:
    """A dead edge, stale metadata, or an active old mount cannot credit retirement."""
    probe = _write_probe_stub(
        tmp_path, "mode=roundtrip mount=/mcp_maint initialize=ok", 1 if broken == "canonical" else 0
    )

    observed: dict[str, set[str]] = {}

    def stub(
        method: str, path: str, body: bytes, accept: str, origin: str, authorization: str
    ) -> tuple[int, str, dict[str, str]]:
        if method == "GET":
            observed.setdefault(path, set()).add(authorization)
        if path == "/mcp_admin" and method == "GET":
            if broken == "authenticated" and authorization:
                return 200, "{}", {}
            if broken == "anonymous" and not authorization:
                return 200, "{}", {}
        if broken == "blanket404":
            return 404, "{}", {}
        if path == "/.well-known/oauth-protected-resource/mcp_admin" and broken == "metadata":
            return 200, '{"resource":"legacy"}', {}
        if path == "/mcp_admin" and broken == "mount":
            return 200, '{"jsonrpc":"2.0","id":1,"result":{}}', {}
        if (path == "/mcp_admin/" and broken == "slash") or (
            path == "/mcp_admin/sse" and broken == "sse"
        ):
            return 200, "{}", {}
        if path == "/mcp_admin" and method == "POST" and broken == "post":
            return 200, '{"jsonrpc":"2.0","id":1,"result":{}}', {}
        if path == "/mcp_admin" and broken == "challenge":
            return 404, "{}", {"WWW-Authenticate": 'Bearer resource_metadata="legacy"'}
        return _green(method, path, body)

    with serve(stub) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS="mcp_admin_retired", PREFLIGHT_MCP_PROBE_CMD=probe)
        )
    expected = "PASS" if broken == "none" else "FAIL"
    assert _verdicts(proc.stdout).get("mcp_admin_retired") == expected, proc.stdout
    assert (proc.returncode == 0) == (broken == "none")
    if broken == "none":
        for path in (
            "/mcp_admin",
            "/mcp_admin/",
            "/mcp_admin/sse",
            "/.well-known/oauth-protected-resource/mcp_admin",
        ):
            assert observed[path] == {"", "Bearer test-token"}

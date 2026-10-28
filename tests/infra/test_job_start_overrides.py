"""Repository-wide gate: every Container Apps job start is a plain start.

A start that passes container overrides needs ``Microsoft.App/jobs/write`` as
well as ``Microsoft.App/jobs/start/action``, and rebuilds the execution's
container from the overrides rather than the deployed template. Jobs that need
per-run input set it on the template first (``az containerapp job update``)
and then start plainly. This gate reads every tracked workflow, script, driver
and runbook code block outside the test tree, finds each job start — CLI,
Python argv or direct ARM — and requires the plain form. The discovered
inventory is pinned so a reader that silently finds nothing fails.

These checks read tracked source only; no Azure tooling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest

from tests.infra._job_start import (
    arm_post_sends_body,
    arm_start_posts,
    fenced_code,
    overrides,
    python_site_flags,
    python_start_sites,
    shell_flags,
    shell_invocations,
    start_path_lines,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_SHELL_SUFFIXES: Final[frozenset[str]] = frozenset({".yml", ".yaml", ".sh", ".ps1", ".bicep"})

# Every job start in tracked source outside tests/, by file and form. A new
# start site must be added here deliberately, after it passes the plain-form
# checks below.
EXPECTED_SITES: Final[dict[tuple[str, str], int]] = {
    (".github/workflows/infra.yml", "cli"): 1,
    (".github/workflows/maintenance.yml", "cli"): 1,
    ("docs/process/postgres-entra-bootstrap.md", "cli"): 1,
    ("deploy/postgres-migration.py", "argv"): 3,
    ("deploy/postgres-restore-verify.py", "argv"): 1,
    ("deploy/performance_trial.py", "arm"): 1,
}


def _tracked() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [path for path in listed if not path.startswith("tests/")]


def _sites() -> list[tuple[str, str, str, list[str] | None]]:
    """(path, form, label, flags-or-None-for-arm) for every discovered start."""
    found: list[tuple[str, str, str, list[str] | None]] = []
    for path in _tracked():
        file = REPO_ROOT / path
        suffix = file.suffix
        if suffix not in _SHELL_SUFFIXES | {".md", ".py"} or not file.is_file():
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        if suffix in _SHELL_SUFFIXES or suffix == ".md":
            body = fenced_code(text) if suffix == ".md" else text
            for number, invocation in enumerate(shell_invocations(body), start=1):
                found.append((path, "cli", f"start {number}", shell_flags(invocation)))
        elif suffix == ".py":
            try:
                for line, elements in python_start_sites(text):
                    found.append((path, "argv", f"line {line}", python_site_flags(elements)))
                for call in arm_start_posts(text):
                    found.append((path, "arm", f"line {call.lineno}", None))
            except SyntaxError:
                continue
    return found


_SITES: Final = _sites()


def test_discovered_start_sites_match_inventory() -> None:
    counts: dict[tuple[str, str], int] = {}
    for path, form, _label, _flags in _SITES:
        counts[(path, form)] = counts.get((path, form), 0) + 1
    assert counts == EXPECTED_SITES, (
        "the set of job-start sites changed; check each new site is a plain start,"
        " then update EXPECTED_SITES"
    )


@pytest.mark.parametrize(
    ("path", "label", "flags"),
    [(p, label, f) for p, form, label, f in _SITES if form != "arm"],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_cli_and_argv_starts_carry_no_override(path: str, label: str, flags: list[str]) -> None:
    assert overrides(flags) == [], (
        f"{path} ({label}) starts a job with {overrides(flags)}: an override needs"
        " jobs/write and rebuilds the container; set per-run input with"
        " `az containerapp job update` and start plainly"
    )


def test_arm_starts_send_no_body() -> None:
    for path, form, label, _flags in _SITES:
        if form != "arm":
            continue
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        line = int(label.split()[-1])
        call = next(c for c in arm_start_posts(source) if c.lineno == line)
        assert not arm_post_sends_body(call), (
            f"{path} ({label}) posts a body to a job /start: any execution template,"
            " even an empty containers array, counts as an override"
        )


def test_every_start_path_literal_is_a_recognized_arm_start() -> None:
    """A ``/start`` path posted by any other client (``httpx``, ``requests``,
    ``urllib``) would escape the ARM reader, so every such literal in tracked
    Python must sit inside a recognized ARM start call."""
    for path in _tracked():
        if not path.endswith(".py"):
            continue
        try:
            source = (REPO_ROOT / path).read_text(encoding="utf-8")
            literals = start_path_lines(source)
            recognized = {call.lineno for call in arm_start_posts(source)}
        except SyntaxError, UnicodeDecodeError:
            continue
        stray = [line for line in literals if line not in recognized]
        assert stray == [], f"{path} lines {stray}: a /start path outside a recognized ARM start"


# Positive controls: each override form the readers must refuse, and the plain
# forms they must accept, so a reader that sees no flags cannot pass vacuously.

_OVERRIDE_FLAGS: Final[tuple[str, ...]] = (
    "--image",
    "--container-name",
    "--env-vars",
    "--command",
    "--args",
    "--cpu",
    "--memory",
    "--yaml",
    "--registry-identity",
    "--env",
)


@pytest.mark.parametrize("flag", _OVERRIDE_FLAGS)
def test_shell_reader_refuses_each_override(flag: str) -> None:
    script = (
        'EXECUTION="$(az containerapp job start \\\n'
        '  --name "$JOB" --resource-group "$RG" \\\n'
        f"  {flag} value \\\n"
        '  --query name -o tsv)"\n'
    )
    [invocation] = shell_invocations(script)
    assert overrides(shell_flags(invocation)) == [flag]


def test_shell_reader_refuses_equals_form() -> None:
    [invocation] = shell_invocations("az containerapp job start -n j -g g --env-vars=A=1\n")
    assert overrides(shell_flags(invocation)) == ["--env-vars"]


def test_shell_reader_tolerates_spacing_and_powershell_continuation() -> None:
    script = "az  containerapp\tjob   start `\n  -n j -g g `\n  --command x\n"
    [invocation] = shell_invocations(script)
    assert overrides(shell_flags(invocation)) == ["--command"]


def test_shell_reader_accepts_plain_start() -> None:
    script = (
        'EXECUTION="$(az containerapp job start \\\n'
        '  --name "$JOB" --resource-group "$RG" \\\n'
        '  --query name -o tsv)"\n'
        "az containerapp job update -n j -g g --set-env-vars A=1\n"
    )
    [invocation] = shell_invocations(script)
    assert shell_flags(invocation) == ["--name", "--resource-group", "--query", "-o"]
    assert overrides(shell_flags(invocation)) == []


def test_markdown_reader_ignores_prose() -> None:
    doc = (
        "Never run `az containerapp job start --env-vars X=1`.\n\n"
        "```bash\naz containerapp job start -g rg -n job\n```\n"
    )
    [invocation] = shell_invocations(fenced_code(doc))
    assert overrides(shell_flags(invocation)) == []


def test_markdown_reader_reads_tilde_fences() -> None:
    doc = "~~~sh\naz containerapp job start -g rg -n job --cpu 1\n~~~\n"
    [invocation] = shell_invocations(fenced_code(doc))
    assert overrides(shell_flags(invocation)) == ["--cpu"]


@pytest.mark.parametrize("flag", _OVERRIDE_FLAGS)
def test_argv_reader_refuses_each_override(flag: str) -> None:
    source = f'az("containerapp", "job", "start", "--name", job, "{flag}", value)\n'
    [(_line, elements)] = python_start_sites(source)
    assert overrides(python_site_flags(elements)) == [flag]


def test_argv_reader_reads_subprocess_list_form() -> None:
    source = 'subprocess.run(["az", "containerapp", "job", "start", "-n", j, "--image", i])\n'
    [(_line, elements)] = python_start_sites(source)
    assert overrides(python_site_flags(elements)) == ["--image"]


def test_argv_reader_refuses_starred_arguments() -> None:
    [(_line, elements)] = python_start_sites(
        'az("containerapp", "job", "start", "--name", job, *extra)\n'
    )
    assert overrides(python_site_flags(elements)) == ["*"]


def test_argv_reader_accepts_plain_start() -> None:
    [(_line, elements)] = python_start_sites(
        'az("containerapp", "job", "start", "--resource-group", group, "--name", job)\n'
    )
    assert overrides(python_site_flags(elements)) == []


def test_argv_reader_ignores_job_update() -> None:
    source = 'az("containerapp", "job", "update", "--name", job, "--args", mode)\n'
    assert python_start_sites(source) == []


@pytest.mark.parametrize(
    "call_source",
    [
        'rest.arm("POST", job["id"] + "/start", body={})',
        'rest.arm("POST", job["id"] + "/start", body={"containers": []})',
        'rest.arm("POST", job["id"] + "/start", raw=payload)',
        'rest.arm("POST", job["id"] + "/start", **options)',
    ],
)
def test_arm_reader_refuses_any_body(call_source: str) -> None:
    [call] = arm_start_posts(call_source)
    assert arm_post_sends_body(call)


def test_arm_reader_accepts_bodyless_start() -> None:
    for source in (
        'rest.arm("POST", job["id"] + "/start", api="2025-07-01")',
        'rest.arm("POST", job["id"] + "/start", body=None)',
    ):
        [call] = arm_start_posts(source)
        assert not arm_post_sends_body(call)


def test_arm_reader_ignores_other_posts() -> None:
    assert arm_start_posts('rest.arm("POST", job["id"] + "/stop", body={})') == []

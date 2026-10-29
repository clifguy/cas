"""Repository-wide gate: every Container Apps job start is a plain start.

A start that passes container overrides needs ``Microsoft.App/jobs/write`` as
well as ``Microsoft.App/jobs/start/action``, and rebuilds the execution's
container from the overrides rather than the deployed template. Jobs that need
per-run input set it on the template first (``az containerapp job update``)
and then start plainly. This gate reads tracked workflows, scripts, Python
drivers and runbook code blocks outside the test tree, finds each job start in
the forms ``tests/infra/_job_start.py`` can read — CLI, ``az rest``, Python argv
or string, and direct ARM — and requires the plain form. The discovered
inventory is pinned so a reader that silently finds nothing fails.

These checks read tracked source only; no Azure tooling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest
import yaml

from tests.infra._job_start import (
    arm_post_sends_body,
    arm_start_posts,
    fenced_code,
    overrides,
    python_command_strings,
    python_site_flags,
    python_start_sites,
    rest_sends_body,
    rest_start_invocations,
    shell_flags,
    shell_invocations,
    start_path_lines,
    yaml_strings,
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


def _shell_texts(path: str, text: str) -> list[str]:
    """The shell text a file carries: fenced blocks of Markdown, every string
    of parsed YAML (so folded scalars arrive joined), or the whole file."""
    if path.endswith(".md"):
        return [fenced_code(text)]
    if path.endswith((".yml", ".yaml")):
        try:
            return yaml_strings(yaml.safe_load(text))
        except yaml.YAMLError:
            return [text]
    return [text]


def _sites() -> list[tuple[str, str, str, list[str] | None]]:
    """(path, form, label, flags) for every discovered start; ``rest`` sites
    carry ``["--body"]`` when they send a body, ``arm`` sites carry None."""
    found: list[tuple[str, str, str, list[str] | None]] = []
    for path in _tracked():
        file = REPO_ROOT / path
        suffix = file.suffix
        if suffix not in _SHELL_SUFFIXES | {".md", ".py"} or not file.is_file():
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        if suffix == ".py":
            try:
                for line, elements in python_start_sites(text):
                    found.append((path, "argv", f"line {line}", python_site_flags(elements)))
                for call in arm_start_posts(text):
                    found.append((path, "arm", f"line {call.lineno}", None))
                texts = [string for _line, string in python_command_strings(text)]
            except SyntaxError:
                continue
        else:
            texts = _shell_texts(path, text)
        cli = [i for body in texts for i in shell_invocations(body)]
        for number, invocation in enumerate(cli, start=1):
            found.append((path, "cli", f"start {number}", shell_flags(invocation)))
        rest = [i for body in texts for i in rest_start_invocations(body)]
        for number, invocation in enumerate(rest, start=1):
            body_flags = ["--body"] if rest_sends_body(invocation) else []
            found.append((path, "rest", f"rest {number}", body_flags))
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
    [(p, label, f) for p, form, label, f in _SITES if form in {"cli", "argv"}],
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


def test_rest_starts_send_no_body() -> None:
    for path, form, label, flags in _SITES:
        if form == "rest":
            assert flags == [], f"{path} ({label}) sends a body to a job /start through az rest"


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


# Positive controls: flags outside the plain-start allowlist that the readers
# must refuse, and the plain forms they must accept, so a reader that sees no
# flags cannot pass vacuously. The CLI sends an execution template for the
# image, container-name, env-vars, command, args, cpu and memory flags and for
# --yaml; --registry-identity and the --env abbreviation are refused because
# the allowlist admits nothing it does not name.

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


def test_shell_reader_refuses_word_list_expansion() -> None:
    script = 'az containerapp job start -n "$J" -g "$G" "${EXTRA_START_ARGS[@]}"\n'
    [invocation] = shell_invocations(script)
    assert overrides(shell_flags(invocation)) == ["*"]


def test_yaml_folded_scalar_reads_as_one_command() -> None:
    workflow = (
        "steps:\n  - run: >-\n      az containerapp job start -n j -g g\n      --env-vars X=1\n"
    )
    texts = yaml_strings(yaml.safe_load(workflow))
    [invocation] = [i for body in texts for i in shell_invocations(body)]
    assert overrides(shell_flags(invocation)) == ["--env-vars"]


@pytest.mark.parametrize(
    ("script", "sends"),
    [
        ('az rest --method post --url "$JOB_ID/start?api-version=2025-07-01"\n', False),
        ('az rest --method post --url "$JOB_ID/start" --body \'{"containers":[]}\'\n', True),
        ('az rest -m post -u "$JOB_ID/start" -b @template.json\n', True),
    ],
)
def test_rest_reader_finds_start_and_reads_body(script: str, sends: bool) -> None:
    [invocation] = rest_start_invocations(script)
    assert rest_sends_body(invocation) is sends


def test_rest_reader_ignores_other_rest_calls() -> None:
    assert rest_start_invocations('az rest --method get --url "$JOB_ID"\n') == []


def test_python_string_command_is_read_as_shell() -> None:
    source = (
        'subprocess.run("az containerapp job start -n x -g y --env-vars A=1", shell=True)\n'
        'subprocess.run(f"az containerapp job start -n {job} -g {group} --cpu 1", shell=True)\n'
    )
    texts = [text for _line, text in python_command_strings(source)]
    flags = [overrides(shell_flags(i)) for body in texts for i in shell_invocations(body)]
    assert flags == [["--env-vars"], ["--cpu"]]


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


def test_argv_reader_refuses_name_in_flag_position() -> None:
    [(_line, elements)] = python_start_sites(
        'az("containerapp", "job", "start", "--name", job, override_flag, "A=1")\n'
    )
    assert overrides(python_site_flags(elements)) == ["*"]


def test_argv_reader_reads_value_after_switch_as_flag_position() -> None:
    [(_line, elements)] = python_start_sites(
        'az("containerapp", "job", "start", "--no-wait", extra, "--name", job)\n'
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


def test_start_path_literal_reader_reads_query_strings() -> None:
    source = (
        'httpx.post("https://management.azure.com/x/jobs/j/start?api-version=1", json={})\n'
        'httpx.post(f"{base}/jobs/{job}/start", json={})\n'
        'log("job started")\n'
    )
    assert start_path_lines(source) == [1, 2]

"""Tests for the hosted abstraction model pin and the check that keeps it current.

The cloud profile's abstraction model is a single Bicep parameter default. An
undated model id follows updates only within its own generation, so a newer
Haiku never arrives on its own; the scheduled check compares the pin against
the Anthropic Models API and fails when a newer Haiku exists or when the pin
no longer resolves (the API reports no retirement dates, so a retired pin is
one that stops resolving).

Everything here is offline: the detector is a pure function driven with
fixture model lists, and the API edge is exercised against a fake client.
Each detector case is paired with a control so a comparison that compares
nothing cannot pass vacuously.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
import pytest
import yaml
from anthropic import InternalServerError, NotFoundError

from scripts import check_abstraction_model as check
from scripts.check_abstraction_model import ModelEntry, assess, fetch, read_pin

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MAIN_BICEP: Final[Path] = REPO_ROOT / "infra" / "main.bicep"
CONTAINER_APPS_BICEP: Final[Path] = REPO_ROOT / "infra" / "modules" / "container-apps.bicep"
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "abstraction-model-drift.yml"
DEPLOYMENT_DOC: Final[Path] = REPO_ROOT / "docs" / "process" / "azure-deployment.md"

CURRENT_HAIKU: Final[str] = "claude-haiku-5-5"
SECRET_NAME: Final[str] = "ANTHROPIC_MODELS_API_KEY"

_OLD_HAIKU = ModelEntry("claude-haiku-4-5-20251001", datetime(2025, 10, 1, tzinfo=UTC))
_NEW_HAIKU = ModelEntry("claude-haiku-5-5", datetime(2026, 9, 1, tzinfo=UTC))
# Newer than both Haikus, so a family filter that admitted it would report it
# as the model to move to.
_NEWER_OPUS = ModelEntry("claude-opus-5-5", datetime(2026, 10, 1, tzinfo=UTC))


def _bicep(declaration: str) -> str:
    return f"targetScope = 'resourceGroup'\n\n{declaration}\n\nparam other string = 'x'\n"


def _sdk_error(cls, status: int) -> Exception:
    request = httpx.Request("GET", "https://api.anthropic.com/v1/models/x")
    return cls("error", response=httpx.Response(status, request=request), body=None)


class _FakeModel:
    def __init__(self, entry: ModelEntry) -> None:
        self.id = entry.id
        self.created_at = entry.created_at


class _FakeModels:
    def __init__(self, resolve: dict[str, ModelEntry], listed: list[ModelEntry], error=None):
        self._resolve = resolve
        self._listed = listed
        self._error = error

    def retrieve(self, model_id: str) -> _FakeModel:
        if self._error is not None:
            raise self._error
        if model_id not in self._resolve:
            raise _sdk_error(NotFoundError, 404)
        return _FakeModel(self._resolve[model_id])

    def list(self) -> list[_FakeModel]:
        return [_FakeModel(entry) for entry in self._listed]


class _FakeClient:
    def __init__(self, resolve, listed, error=None) -> None:
        self.models = _FakeModels(resolve, listed, error)


# ---------------------------------------------------------------------------
# The pin itself
# ---------------------------------------------------------------------------


def test_pin_is_the_current_haiku() -> None:
    """B1: the committed default is the current Haiku."""
    assert read_pin(MAIN_BICEP.read_text(encoding="utf-8")) == CURRENT_HAIKU


def test_container_apps_module_declares_no_second_default() -> None:
    """B2: the module requires the value from its caller, so no second default
    can drift from the one ``main.bicep`` declares."""
    text = CONTAINER_APPS_BICEP.read_text(encoding="utf-8")
    declarations = re.findall(r"^\s*param\s+abstractionModel\b(.*)$", text, re.MULTILINE)

    assert len(declarations) == 1, "the module must still declare the parameter"
    assert "=" not in declarations[0], "the module must not carry its own default"


# ---------------------------------------------------------------------------
# read_pin
# ---------------------------------------------------------------------------


def test_read_pin_returns_the_declared_default() -> None:
    """R1 (control): one live declaration yields its value."""
    assert read_pin(_bicep("param abstractionModel string = 'claude-x-1'")) == "claude-x-1"


def test_read_pin_ignores_comments() -> None:
    """R2: a commented-out declaration is not the pin, and a trailing comment
    on the live one does not hide its value."""
    text = _bicep(
        "// param abstractionModel string = 'claude-old'\n"
        "param abstractionModel string = 'claude-new' // the current Haiku"
    )
    assert read_pin(text) == "claude-new"


@pytest.mark.parametrize(
    "declaration",
    [
        "param somethingElse string = 'claude-x-1'",
        "param abstractionModel string",
        "param abstractionModel string = 'a'\nparam abstractionModel string = 'b'",
    ],
    ids=["absent", "no-default", "duplicate"],
)
def test_read_pin_refuses_an_unreadable_pin(declaration: str) -> None:
    """R3: no default or several defaults is an error, never a guess."""
    with pytest.raises(ValueError):
        read_pin(_bicep(declaration))


# ---------------------------------------------------------------------------
# assess -- the detector
# ---------------------------------------------------------------------------

_LISTED: Final[list[ModelEntry]] = [_NEWER_OPUS, _NEW_HAIKU, _OLD_HAIKU]


def test_assess_current_pin_has_no_findings() -> None:
    """D1 (control): the newest Haiku pinned is clean, even though a newer
    non-Haiku model is listed."""
    assert assess(CURRENT_HAIKU, _NEW_HAIKU, _LISTED) == []


def test_assess_older_pin_names_both_ids() -> None:
    """D2: a newer Haiku than the pin is a finding naming both, compared on
    the pin's resolved id rather than its alias."""
    findings = assess("claude-haiku-4-5", _OLD_HAIKU, _LISTED)

    assert len(findings) == 1
    assert "claude-haiku-4-5" in findings[0]
    assert _NEW_HAIKU.id in findings[0]


def test_assess_orders_by_creation_date_not_list_position() -> None:
    """D3: the newest Haiku is chosen by ``created_at``; listing the older one
    first must not make it the newest."""
    reordered = [_OLD_HAIKU, _NEW_HAIKU, _NEWER_OPUS]

    assert assess(CURRENT_HAIKU, _NEW_HAIKU, reordered) == []
    assert len(assess("claude-haiku-4-5", _OLD_HAIKU, reordered)) == 1


def test_assess_tie_on_creation_date_accepts_either() -> None:
    """D3b: two Haikus created at the same instant are both newest, so a pin
    resolving to either is current whichever the listing puts first."""
    twin = ModelEntry("claude-haiku-5-5-twin", _NEW_HAIKU.created_at)

    assert assess(CURRENT_HAIKU, _NEW_HAIKU, [twin, _NEW_HAIKU, _OLD_HAIKU]) == []
    assert assess(CURRENT_HAIKU, _NEW_HAIKU, [_NEW_HAIKU, twin, _OLD_HAIKU]) == []


def test_assess_unresolvable_pin_is_a_finding() -> None:
    """D4: a pin the API no longer resolves is retired or mistyped."""
    findings = assess("claude-haiku-4-5", None, _LISTED)

    assert len(findings) == 1
    assert "claude-haiku-4-5" in findings[0]


def test_assess_empty_haiku_family_is_a_finding() -> None:
    """D5: a listing with no Haiku at all cannot confirm the pin, so it is
    reported rather than passed."""
    findings = assess(CURRENT_HAIKU, _NEW_HAIKU, [_NEWER_OPUS])

    assert len(findings) == 1


# ---------------------------------------------------------------------------
# fetch -- the API edge
# ---------------------------------------------------------------------------


def test_fetch_resolves_the_alias_and_lists_models() -> None:
    """F1: the pin is resolved through the API, which maps an alias to its
    dated id, and the listing is returned in full."""
    client = _FakeClient({"claude-haiku-4-5": _OLD_HAIKU}, _LISTED)

    resolved, listed = fetch("claude-haiku-4-5", client)

    assert resolved == _OLD_HAIKU
    assert listed == _LISTED


def test_fetch_maps_not_found_to_none() -> None:
    """F2: a 404 on the pin means unresolvable, not an error to raise."""
    client = _FakeClient({}, _LISTED)

    resolved, _ = fetch("claude-haiku-3-0", client)

    assert resolved is None


def test_fetch_raises_on_other_api_errors() -> None:
    """F3: a server error is not converted into an unresolvable pin or a
    pass; the run must fail on it."""
    client = _FakeClient({}, _LISTED, error=_sdk_error(InternalServerError, 500))

    with pytest.raises(InternalServerError):
        fetch(CURRENT_HAIKU, client)


# ---------------------------------------------------------------------------
# main -- exit status
# ---------------------------------------------------------------------------


@pytest.fixture
def _pinned_bicep(tmp_path: Path):
    def write(pin: str) -> Path:
        path = tmp_path / "main.bicep"
        path.write_text(_bicep(f"param abstractionModel string = '{pin}'"), encoding="utf-8")
        return path

    return write


def test_main_exits_zero_when_current(monkeypatch, _pinned_bicep) -> None:
    """M1 (control): a current pin exits 0."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(check, "_client", lambda: _FakeClient({CURRENT_HAIKU: _NEW_HAIKU}, _LISTED))

    assert check.main(["--bicep", str(_pinned_bicep(CURRENT_HAIKU))]) == 0


def test_main_exits_one_when_stale(monkeypatch, _pinned_bicep) -> None:
    """M2: an older pin exits 1."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        check, "_client", lambda: _FakeClient({"claude-haiku-4-5": _OLD_HAIKU}, _LISTED)
    )

    assert check.main(["--bicep", str(_pinned_bicep("claude-haiku-4-5"))]) == 1


def test_main_exits_one_when_unresolvable(monkeypatch, _pinned_bicep) -> None:
    """M3: a pin the API does not resolve exits 1."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(check, "_client", lambda: _FakeClient({}, _LISTED))

    assert check.main(["--bicep", str(_pinned_bicep("claude-haiku-3-0"))]) == 1


def test_main_refuses_to_run_without_a_key(monkeypatch, _pinned_bicep) -> None:
    """M4: no credential fails the run; it never skips to green."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(check, "_client", lambda: _FakeClient({CURRENT_HAIKU: _NEW_HAIKU}, _LISTED))

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        check.main(["--bicep", str(_pinned_bicep(CURRENT_HAIKU))])


# ---------------------------------------------------------------------------
# The scheduled workflow
# ---------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on_block(workflow: dict) -> dict:
    """PyYAML reads a bare ``on:`` key as the boolean ``True``."""
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def test_workflow_runs_on_schedule_and_dispatch_only() -> None:
    """W1: a daily schedule plus manual dispatch, and never on pull requests,
    where it would need the secret and could collide with a required check."""
    on = _on_block(_workflow())

    assert "workflow_dispatch" in on
    assert any("cron" in entry for entry in on.get("schedule") or [])
    assert "pull_request" not in on
    assert "push" not in on


def test_workflow_least_privilege_permissions() -> None:
    """W2: the check reads and reports; divergence is a failing run."""
    perms = _workflow().get("permissions") or {}

    assert perms == {"contents": "read"}


def test_workflow_binds_the_secret_only_on_the_check_step() -> None:
    """W3: the credential reaches exactly the step that runs the check.

    Read from the parsed workflow rather than its text, so a header comment
    naming the secret cannot keep this green after the binding is removed.
    """
    workflow = _workflow()
    assert SECRET_NAME not in str(workflow.get("env") or {})

    check_steps = []
    for job in (workflow.get("jobs") or {}).values():
        assert SECRET_NAME not in str(job.get("env") or {}), "secret must not be job-wide"
        for step in job.get("steps") or []:
            if "scripts.check_abstraction_model" in (step.get("run") or ""):
                check_steps.append(step)
            else:
                assert SECRET_NAME not in str(step), "secret must reach only the check step"

    assert len(check_steps) == 1, "workflow must invoke the check module once"
    env = check_steps[0].get("env") or {}
    assert f"secrets.{SECRET_NAME}" in env.get("ANTHROPIC_API_KEY", "")


# ---------------------------------------------------------------------------
# The deployment guide describes the check
# ---------------------------------------------------------------------------


def test_deployment_guide_names_the_check_and_the_secret() -> None:
    """P1: the next person bumping the pin can see what verifies it and what
    it needs."""
    text = DEPLOYMENT_DOC.read_text(encoding="utf-8")

    assert WORKFLOW.name in text
    assert SECRET_NAME in text
    assert "abstractionModel" in text

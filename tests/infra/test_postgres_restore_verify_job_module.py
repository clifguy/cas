"""Structural gate for the temporary restore-verification job module.

Locks the shape of ``infra/modules/postgres-restore-verify-job.bicep``: the
Container Apps Job that reads an isolated restore target from inside the VNet,
because the restored server has no public endpoint (CAS-ADR-042). Three
properties are load-bearing and are asserted by value rather than by presence.

The job must never be wired into the serving template, because it exists only for
the duration of a drill. It must carry no deletion lock, because a lock on drill
scaffolding turns teardown into a two-step operation that can be abandoned
halfway. And its name must not match the migration-job prefix the tenant deploy
guard scans, or an in-flight drill would block ordinary deployment and
maintenance for as long as it ran.

These checks read tracked Bicep text; the infra workflow's validate job is the
authoritative compile. Detectors live in pure helpers so the control tests can
prove each one actually fires.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"
MAIN_BICEP: Final[Path] = INFRA_DIR / "main.bicep"
MODULE: Final[Path] = INFRA_DIR / "modules" / "postgres-restore-verify-job.bicep"
GUARD: Final[Path] = REPO_ROOT / "deploy" / "postgres-migration-guard.sh"

_JOB_TYPE: Final[str] = "Microsoft.App/jobs"
_LOCK_TYPE: Final[str] = "Microsoft.Authorization/locks"
_ENTRYPOINT: Final[str] = "sage.maintenance.postgres_restore_verify"
_JOB_NAME: Final[str] = "job-pg-restore-verify-${environmentName}"

# Exactly the contract the verifier reads. A key added here without the module
# supplying it leaves the job failing at startup on a missing setting.
_REQUIRED_ENV: Final[tuple[str, ...]] = (
    "AZURE_CLIENT_ID",
    "PG_ADMIN_USER",
    "PG_FQDN",
    "PG_SERVING_FQDN",
    "PG_DATABASE",
    "SAGE_DB_ROLE",
    "BFF_DB_ROLE",
    "PG_EXPECTED_MAJOR",
    "PG_RESTORE_POINT",
    "PG_SENTINEL_SCHEMA",
    "PG_SENTINEL_BEFORE_ID",
    "PG_SENTINEL_AFTER_ID",
)

_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _strip_line_comments(text: str) -> str:
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    return re.search(rf"^resource\s+\w+\s+'{re.escape(resource_type)}@", text, re.M) is not None


def _guard_job_prefixes(guard: str) -> list[str]:
    """Job-name prefixes the tenant deploy guard blocks on, read from its query."""
    return re.findall(r"starts_with\(name,\s*'([^']+)'\)", guard)


@pytest.fixture(scope="module")
def module_text() -> str:
    return MODULE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def live(module_text: str) -> str:
    return _strip_line_comments(module_text)


def test_module_declares_a_manual_job(live: str) -> None:
    assert _declares_resource_type(live, _JOB_TYPE)
    assert "triggerType: 'Manual'" in live
    assert "replicaRetryLimit: 0" in live


def test_module_invokes_the_verification_entrypoint(live: str) -> None:
    assert f"'python', '-m', '{_ENTRYPOINT}'" in live
    # No mode argument: the verifier has exactly one behaviour and refuses argv.
    assert "args:" not in live


def test_module_supplies_every_contracted_setting(live: str) -> None:
    for key in _REQUIRED_ENV:
        assert re.search(rf"name:\s*'{key}'", live), f"module does not supply {key}"


def test_module_supplies_no_setting_the_verifier_does_not_read(live: str) -> None:
    supplied = set(re.findall(r"\{\s*name:\s*'([A-Z_]+)'", live))
    assert supplied == set(_REQUIRED_ENV)


def test_the_serving_address_is_supplied_so_the_job_can_refuse_it(live: str) -> None:
    # The refusal is only possible if the job is told what the serving server is.
    # Supplying it is what makes the verifier's own guard operative in the cloud.
    assert re.search(r"name:\s*'PG_SERVING_FQDN',\s*value:\s*servingFqdn", live)
    assert re.search(r"name:\s*'PG_FQDN',\s*value:\s*restoreFqdn", live)
    assert "param servingFqdn string" in live
    assert "param restoreFqdn string" in live


def test_expected_major_comes_from_the_declared_baseline(live: str) -> None:
    assert "loadJsonContent('../../versions.json')" in live
    assert re.search(
        r"name:\s*'PG_EXPECTED_MAJOR',\s*value:\s*versions\.postgres\.deploy_major", live
    )


def test_image_is_a_parameter_used_verbatim(live: str) -> None:
    assert "param image string" in live
    assert re.search(r"^\s*image:\s*image\s*$", live, re.M)
    assert ":latest" not in live


def test_module_declares_no_deletion_lock(live: str) -> None:
    # Drill scaffolding. A lock here would make teardown two operations, and an
    # abandoned drill would leave a locked resource behind.
    assert not _declares_resource_type(live, _LOCK_TYPE)


def test_job_name_does_not_trip_the_tenant_deploy_guard(live: str) -> None:
    assert f"name: '{_JOB_NAME}'" in live
    guard = GUARD.read_text(encoding="utf-8")
    prefixes = _guard_job_prefixes(guard)
    assert prefixes, "the guard must scan some job prefix for this test to mean anything"
    for prefix in prefixes:
        assert not _JOB_NAME.startswith(prefix), (
            f"the verification job name matches the guard prefix {prefix!r}; a drill "
            "would block ordinary deployment and maintenance while it ran"
        )


def test_module_is_not_wired_into_the_serving_template() -> None:
    main = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert "postgres-restore-verify-job.bicep" not in main


def test_module_hardcodes_no_identity_guid(live: str) -> None:
    assert not _GUID_RE.search(live)
    assert "param identityId string" in live
    assert "param identityClientId string" in live


def test_replica_timeout_is_bounded(live: str) -> None:
    match = re.search(r"replicaTimeout:\s*(\d+)", live)
    assert match is not None
    # Long enough to hash every workload table, short enough that an abandoned
    # drill releases its replica the same hour.
    assert 600 <= int(match.group(1)) <= 3600


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="requires the Bicep CLI or the Azure CLI's bicep extension",
)
def test_module_compiles(tmp_path: Path) -> None:
    outfile = tmp_path / "postgres-restore-verify-job.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(MODULE), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(MODULE), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
# ---------------------------------------------------------------------------


def test_resource_type_detector_controls() -> None:
    declared = "resource lock 'Microsoft.Authorization/locks@2020-05-01' = {\n}\n"
    commented = "// resource lock 'Microsoft.Authorization/locks@2020-05-01' = {\n"
    assert _declares_resource_type(declared, _LOCK_TYPE)
    assert not _declares_resource_type(_strip_line_comments(commented), _LOCK_TYPE)


def test_guard_prefix_detector_would_catch_a_colliding_name() -> None:
    guard = GUARD.read_text(encoding="utf-8")
    prefixes = _guard_job_prefixes(guard)
    colliding = "job-pg-migration-restore-verify-${environmentName}"
    assert any(colliding.startswith(prefix) for prefix in prefixes), (
        "the detector must flag a name that does collide, or the passing case proves nothing"
    )


def test_env_key_detector_would_catch_a_dropped_setting(live: str) -> None:
    without = live.replace("{ name: 'PG_SERVING_FQDN', value: servingFqdn }", "")
    assert not re.search(r"name:\s*'PG_SERVING_FQDN'", without)

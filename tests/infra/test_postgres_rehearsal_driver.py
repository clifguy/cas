"""Admission and compiled-deployment tests for the real migration driver."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.infra.test_postgres_migration_driver import ROOT, Azure, driver


def test_nested_deployment_cannot_overwrite_driver_parent(tmp_path: Path) -> None:
    az = Azure()
    driver().prepare(az, "prod", "group", "g17")
    call = next(c for c in az.calls if "infra/postgres-replacement.bicep" in c)
    parent = call[call.index("--name") + 1]
    output = tmp_path / "replacement.json"
    command = ["bicep", "build"] if shutil.which("bicep") else ["az", "bicep", "build", "--file"]
    subprocess.run(
        [*command, str(ROOT / "infra/postgres-replacement.bicep"), "--outfile", str(output)],
        check=True,
        capture_output=True,
    )
    template = json.loads(output.read_text())
    nested = [r for r in template["resources"] if r["type"] == "Microsoft.Resources/deployments"]
    assert nested, "must inspect the compiled nested deployment"
    # Bicep emits format() for this interpolation; substitute the actual generation.
    for resource in nested:
        expression = resource["name"]
        import re

        match = re.fullmatch(
            r"\[format\('([^']+)', parameters\('serverGeneration'\)\)\]", expression
        )
        assert match, "nested naming expression must be evaluated, not silently ignored"
        assert match[1].format("g17") != parent, "nested deployment overwrites active parent"


@pytest.mark.parametrize(
    "fence,active", [("copying:g17", False), ("verified:g17", False), ("", True)]
)
def test_rehearsal_refuses_unsafe_dispatch(fence: str, active: bool) -> None:
    module = driver()
    assert hasattr(module, "rehearse"), "driver has no serialized rehearsal action"
    az = Azure()
    az.fence, az.active_job = fence, active
    with pytest.raises(ValueError, match="fence|active"):
        module.rehearse(az, "prod", "group", "g17", "r123", "rehearse", sleep=lambda _: None)
    assert not any(
        c[:3] in [("containerapp", "job", "update"), ("containerapp", "job", "start")]
        for c in az.calls
    )
    assert not any(c[:2] == ("tag", "update") for c in az.calls)


def test_runtime_has_source_major_client_for_seed_copy() -> None:
    from tests.deploy.test_sage_container_image import _runtime_stage_text

    assert "postgresql-client-16" in _runtime_stage_text(), (
        "PG17 seed archive cannot restore to PG16 clone"
    )


def test_rehearsal_dispatches_without_changing_serving_state() -> None:
    module = driver()
    assert hasattr(module, "rehearse"), "driver has no serialized rehearsal action"
    az = Azure()

    def boundary(*args: str) -> Any:
        result = az(*args)
        if args[:3] == ("containerapp", "job", "show"):
            result["properties"]["configuration"]["replicaTimeout"] = 7200
            result["properties"]["template"]["containers"][0]["resources"] = {
                "cpu": 1,
                "memory": "2Gi",
            }
            result["properties"]["template"]["containers"][0]["env"].append(
                {"name": "PG_MIGRATION_IMAGE", "value": "registry/sage:1.0-abcdef"}
            )
        return result

    module.rehearse(boundary, "prod", "group", "g17", "r123", "rehearse", sleep=lambda _: None)
    update = next(c for c in az.calls if c[:3] == ("containerapp", "job", "update"))
    assert update[-3:] == ("--args", "rehearse", "r123")
    assert not any(
        c[:2] == ("tag", "update") or c[:2] == ("containerapp", "revision") for c in az.calls
    )


@pytest.mark.parametrize("purpose", ["deploy", "maintenance", "migration"])
def test_shared_guard_blocks_orphaned_rehearsal_job(tmp_path: Path, purpose: str) -> None:
    import os

    az = tmp_path / "az"
    az.write_text("""#!/bin/sh
case "$*" in
  "group exists "*) echo true ;;
  "group show "*) echo '' ;;
  "containerapp job list "*) echo job-pg-migration-prod ;;
  "containerapp job execution list "*) echo Running ;;
  *) exit 91 ;;
esac
""")
    az.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "deploy/postgres-migration-guard.sh"),
            purpose,
            "group",
            "g17" if purpose == "migration" else "",
        ],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "database job remains active" in result.stderr, (
        "cancelled workflow must not release an active Azure rehearsal job"
    )


def test_collector_uses_verified_console_schema_and_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = driver()
    monkeypatch.chdir(tmp_path)
    record = dict(
        job="job-pg-migration-prod",
        execution="job-pg-migration-prod-abc",
        run_id="r123",
        action="rehearse",
        image="registry/sage:fixed",
    )
    (tmp_path / "rehearsal-execution.json").write_text(json.dumps(record))
    report = dict(status="rehearsal_verified", run_id="r123", image=record["image"])
    queries = []

    def boundary(*args: str) -> Any:
        if args[:3] == ("deployment", "sub", "show"):
            return {"properties": {"outputs": {"logAnalyticsCustomerId": {"value": "workspace"}}}}
        query = args[args.index("--analytics-query") + 1]
        queries.append(query)
        assert "ContainerJobName_s" in query, "JobName_s is absent from live console-log schema"
        assert record["execution"] in query and record["run_id"] in query
        return [{"report": json.dumps(report)}]

    module.collect_rehearsal_report(boundary, "prod", sleep=lambda _: None)
    assert json.loads((tmp_path / "rehearsal-report.json").read_text())["report"] == report
    report["status"] = "failed"
    with pytest.raises(ValueError, match="does not establish success"):
        module.collect_rehearsal_report(boundary, "prod", sleep=lambda _: None)

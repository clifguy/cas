"""Predicted cloud mutations must match the exact approved experiment inventory."""

import copy

import pytest

from deploy.performance_preview import inventory
from deploy.performance_trial import manifest_parameters, validate_plan
from tests.deploy.test_performance_trial import NOW, plan


def preview_setup():
    p = validate_plan(plan(), NOW)
    p["candidate"]["abstraction_model"] = "model-pin"
    params = manifest_parameters(p, "operator", "private-ingress")
    resource_id = (
        "/subscriptions/" + p["subscription_id"] + "/resourceGroups/" + p["resource_group"]
    )
    change = {
        "changeType": "Create",
        "resourceId": resource_id,
        "after": {"tags": {"benchmarkRun": p["run_id"], "expiresAt": p["expires_at"]}},
    }
    return p, params, {"status": "Succeeded", "changes": [change]}


def test_what_if_refuses_foreign_extra_and_destructive_mutations():
    p, params, result = preview_setup()
    assert inventory(p, result, params, {})[0]["change_type"] == "Create"
    for mutation in ["foreign", "extra", "delete", "unknown"]:
        changed = copy.deepcopy(result)
        row = changed["changes"][0]
        if mutation == "foreign":
            row["resourceId"] = row["resourceId"].replace(p["resource_group"], "production")
        elif mutation == "extra":
            row["resourceId"] += "/providers/Microsoft.App/containerApps/extra"
        else:
            row["changeType"] = "Delete" if mutation == "delete" else "Unsupported"
        with pytest.raises(ValueError):
            inventory(p, changed, params, {})


def test_actual_apply_requires_matching_retained_preview(tmp_path, monkeypatch):
    from datetime import datetime

    from deploy import performance_trial as subject

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(subject, "datetime", Clock)
    p, params, result = preview_setup()
    calls = []

    def az(*args, **kwargs):
        calls.append(args)
        if args[2] == "what-if":
            return result
        return {"properties": {"outputs": {}}}

    with pytest.raises(FileNotFoundError):
        subject.apply_template(p, az, params, tmp_path / "main.bicep", output=tmp_path)
    assert calls == []
    subject.apply_template(p, az, params, tmp_path / "main.bicep", preview=True, output=tmp_path)
    subject.apply_template(p, az, params, tmp_path / "main.bicep", output=tmp_path)
    assert [x[2] for x in calls] == ["validate", "what-if", "create"]
    assert "private-ingress" not in (tmp_path / "what-if-foundation.json").read_text()
    params["parameters"]["cpu"]["value"] = "4"
    with pytest.raises(ValueError, match="differs"):
        subject.apply_template(p, az, params, tmp_path / "main.bicep", output=tmp_path)
    assert len(calls) == 3

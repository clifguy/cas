"""Exercise targeted cleanup against a stateful Azure CLI seam."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/retire_mcp_admin.py"


def _module():
    spec = importlib.util.spec_from_file_location("retire_mcp_admin", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("apply", [False, True])
def test_apim_cleanup_is_targeted_and_idempotent(apply):
    module = _module()
    operations = [
        "catch-all-get",
        "oauth-protected-resource-mcp",
        "oauth-protected-resource-mcp-maint",
        "oauth-protected-resource-mcp-admin",
    ]
    calls = []

    def az(*args):
        calls.append(args)
        if args[:4] == ("apim", "api", "operation", "list"):
            return [{"name": name} for name in operations]
        assert args[:4] == ("apim", "api", "operation", "delete")
        assert args[args.index("--operation-id") + 1] == "oauth-protected-resource-mcp-admin"
        operations.remove("oauth-protected-resource-mcp-admin")
        return None

    module.az = az
    module.retire_apim("rg", "service", "sage", apply)
    assert ("oauth-protected-resource-mcp-admin" not in operations) == apply
    if apply:
        module.retire_apim("rg", "service", "sage", True)
        assert len([call for call in calls if "delete" in call]) == 1
    assert operations[:3] == [
        "catch-all-get",
        "oauth-protected-resource-mcp",
        "oauth-protected-resource-mcp-maint",
    ]


def test_apim_cleanup_cannot_credit_noop_delete():
    module = _module()
    module.az = lambda *args: [{"name": "oauth-protected-resource-mcp-admin"}]
    with pytest.raises(RuntimeError, match="still exists"):
        module.retire_apim("rg", "service", "sage", True)


def test_cleanup_does_not_swallow_azure_failures():
    module = _module()

    def failed(*args):
        raise RuntimeError("authorization failed")

    module.az = failed
    with pytest.raises(RuntimeError, match="authorization failed"):
        module.retire_apim("rg", "service", "sage", True)


@pytest.mark.parametrize("apply", [False, True])
def test_entra_cleanup_preserves_other_identifiers_and_reads_back(apply):
    module = _module()
    original = [
        "api://app",
        "https://sage.example",
        "https://sage.example/mcp",
        "https://sage.example/mcp_maint",
        "https://sage.example/mcp_admin",
        "https://other.example/mcp_admin",
    ]
    state = original.copy()
    calls = []

    def az(*args):
        calls.append(args)
        if args[:3] == ("ad", "app", "show"):
            return state.copy()
        assert args[:3] == ("ad", "app", "update")
        state[:] = args[args.index("--identifier-uris") + 1 :]
        return None

    module.az = az
    module.retire_entra("app", "sage.example", apply)
    assert state == [
        uri for uri in original if not (apply and uri == "https://sage.example/mcp_admin")
    ]
    if apply:
        module.retire_entra("app", "sage.example", True)
        assert len([call for call in calls if "update" in call]) == 1


def test_entra_cleanup_cannot_credit_noop_update():
    module = _module()
    module.az = lambda *args: ["api://app", "https://sage.example/mcp_admin"]
    with pytest.raises(RuntimeError, match="readback"):
        module.retire_entra("app", "sage.example", True)

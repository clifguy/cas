"""Exercise the logger-credential named-value cleanup against a stateful Azure CLI seam.

API Management stores a logger's plain credential values as auto-generated
secret named values (``Logger-Credentials--<hex>``) and keeps a ``{{name}}``
reference in the logger. The cleanup deletes only those values no logger
references, and deletes nothing when any read it depends on is missing,
malformed or unresolved.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/apim_logger_credentials_cleanup.py"
SERVICE_ID = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
LOGGERS_URL = f"https://management.azure.com{SERVICE_ID}/loggers?api-version=2022-08-01"


def _module():
    spec = importlib.util.spec_from_file_location("apim_logger_credentials_cleanup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _logger_value(name: str, *, secret: bool = True, key_vault: bool = False) -> dict:
    """An auto-generated logger-credential value; its display suffix differs from its name."""
    return {
        "name": name,
        "displayName": f"Logger-Credentials--{name}x",
        "secret": secret,
        "keyVault": {"secretIdentifier": "https://kv/secrets/s"} if key_vault else None,
    }


class FakeAzure:
    """Named values and loggers on one gateway, with the calls made against them."""

    def __init__(self, values: list[dict], loggers: list[dict], pages: int = 1) -> None:
        self.values = values
        self.loggers = loggers
        self.pages = pages
        self.calls: list[tuple[str, ...]] = []
        self.ignore_deletes = False

    def __call__(self, *args: str) -> Any:
        self.calls.append(args)
        if args[:2] == ("apim", "show"):
            return SERVICE_ID
        if args[:3] == ("apim", "nv", "list"):
            return [dict(v) for v in self.values]
        if args[:3] == ("apim", "nv", "delete"):
            assert "--yes" in args
            name = args[args.index("--named-value-id") + 1]
            if not self.ignore_deletes:
                self.values = [v for v in self.values if v["name"] != name]
            return None
        if args[:3] == ("rest", "--method", "get"):
            url = args[args.index("--url") + 1]
            if url == LOGGERS_URL:
                page = 0
            else:
                assert url.startswith(LOGGERS_URL + "&page="), url
                page = int(url.rsplit("=", 1)[1])
            chunk = self.loggers[page :: self.pages]
            body: dict[str, Any] = {"value": chunk}
            if page + 1 < self.pages:
                body["nextLink"] = f"{LOGGERS_URL}&page={page + 1}"
            return body
        raise AssertionError(f"unexpected az call {args!r}")

    def deletes(self) -> list[str]:
        return [
            c[c.index("--named-value-id") + 1]
            for c in self.calls
            if c[:3] == ("apim", "nv", "delete")
        ]


def _logger(name: str, **credentials: str) -> dict:
    return {
        "name": name,
        "properties": {"loggerType": "applicationInsights", "credentials": credentials},
    }


def _gateway(pages: int = 1) -> FakeAzure:
    values = [
        _logger_value("a1"),
        _logger_value("a2"),
        _logger_value("live"),
        _logger_value("kv", key_vault=True),
        _logger_value("plain", secret=False),
        {
            "name": "sage-audience",
            "displayName": "sage-audience",
            "secret": False,
            "keyVault": None,
        },
        # An ordinary secret value no logger references: not a logger credential.
        {
            "name": "hand-set-secret",
            "displayName": "hand-set-secret",
            "secret": True,
            "keyVault": None,
        },
        {
            "name": "appinsights-logger-identity-client-id",
            "displayName": "appinsights-logger-identity-client-id",
            "secret": False,
            "keyVault": None,
        },
    ]
    loggers = [
        _logger(
            "appinsights", connectionString="InstrumentationKey=x", identityClientId="{{live}}"
        ),
        _logger("other", identityClientId="{{appinsights-logger-identity-client-id}}"),
    ]
    return FakeAzure(values, loggers, pages)


@pytest.mark.parametrize("apply", [False, True])
def test_deletes_only_unreferenced_logger_credentials(apply: bool) -> None:
    """Only unreferenced, auto-generated, non-Key Vault secret logger credentials go.

    The referenced value, a Key Vault-backed one, a non-secret one and every
    ordinary named value stay. Preview deletes nothing; a second apply is a no-op.
    """
    module = _module()
    fake = _gateway()
    module.az = fake
    planned = module.cleanup("rg", "apim", apply)
    assert sorted(planned) == ["a1", "a2"]
    remaining = {v["name"] for v in fake.values}
    if apply:
        assert sorted(fake.deletes()) == ["a1", "a2"]
        assert remaining == {
            "live",
            "kv",
            "plain",
            "sage-audience",
            "hand-set-secret",
            "appinsights-logger-identity-client-id",
        }
        assert module.cleanup("rg", "apim", True) == []
        assert len(fake.deletes()) == 2
    else:
        assert fake.deletes() == []
        assert {"a1", "a2"} <= remaining


def test_reference_by_display_name_is_protected() -> None:
    """A value referenced by its display name rather than its name is still in use."""
    module = _module()
    fake = _gateway()
    fake.loggers.append(_logger("third", identityClientId="{{Logger-Credentials--a1x}}"))
    module.az = fake
    assert module.cleanup("rg", "apim", True) == ["a2"]


def test_follows_every_page_of_loggers() -> None:
    """A reference on a later page of loggers protects its value."""
    module = _module()
    fake = _gateway(pages=2)
    fake.loggers.append(_logger("paged", identityClientId="{{a2}}"))
    module.az = fake
    assert module.cleanup("rg", "apim", True) == ["a1"]
    assert sum(1 for c in fake.calls if c[:3] == ("rest", "--method", "get")) == 2


@pytest.mark.parametrize(
    ("label", "mutate", "match"),
    [
        ("no loggers", lambda f: f.loggers.clear(), "no loggers"),
        (
            "unresolved reference",
            lambda f: f.loggers.append(_logger("x", identityClientId="{{missing}}")),
            "unresolved",
        ),
        ("malformed logger", lambda f: f.loggers.append({"name": "x"}), "shape"),
        ("malformed value", lambda f: f.values.append({"name": 3}), "shape"),
    ],
)
@pytest.mark.parametrize("apply", [False, True])
def test_fails_closed_without_deleting(label: str, mutate: Any, match: str, apply: bool) -> None:
    """An empty logger list, a reference to no named value, or a malformed read
    stops the run before any delete -- in preview as well as apply.
    """
    module = _module()
    fake = _gateway()
    mutate(fake)
    module.az = fake
    with pytest.raises(RuntimeError, match=match):
        module.cleanup("rg", "apim", apply)
    assert fake.deletes() == [], label


def test_cannot_credit_a_noop_delete() -> None:
    """A delete the readback still shows is an error, not a success."""
    module = _module()
    fake = _gateway()
    fake.ignore_deletes = True
    module.az = fake
    with pytest.raises(RuntimeError, match="still exist"):
        module.cleanup("rg", "apim", True)


def test_does_not_swallow_azure_failures() -> None:
    """A failed read surfaces; it never reads as an empty gateway."""
    module = _module()

    def az(*args: str) -> Any:
        raise subprocess.CalledProcessError(1, ["az", *args])

    module.az = az
    with pytest.raises(subprocess.CalledProcessError):
        module.cleanup("rg", "apim", True)

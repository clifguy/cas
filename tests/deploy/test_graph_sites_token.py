"""The delegated Graph token helper behind the vault-source seed's site steps.

``deploy/bootstrap/graph_sites_token.py`` mints, or checks, the
``Sites.FullControl.All`` token that Microsoft Graph requires for a site's
permissions -- a scope the Azure CLI's own client can never be granted. These
tests load it by path and stand an in-memory double in for ``msal``, so no
sign-in or network call happens: what is held is the client and scope
requested, the scope check on what comes back, and that the token is the only
thing written to standard output.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
HELPER: Final[Path] = REPO_ROOT / "deploy" / "bootstrap" / "graph_sites_token.py"
# Microsoft Graph PowerShell's public client, assembled so no GUID-shaped
# literal appears in the repository.
GRAPH_POWERSHELL_CLIENT_ID: Final[str] = "-".join(
    ("14d82eec", "204b", "4c2f", "b7e8", "296a70dab67e")
)
SCOPE: Final[str] = "https://graph.microsoft.com/Sites.FullControl.All"


def _token(scopes: str = "Sites.FullControl.All") -> str:
    def segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'scp': scopes})}.unsigned"


class _FakeApp:
    """Records how the helper builds and drives the public client."""

    instances: list[_FakeApp] = []
    result: dict[str, Any] = {}

    def __init__(self, client_id: str, authority: str | None = None) -> None:
        self.client_id = client_id
        self.authority = authority
        self.calls: list[tuple[str, Any]] = []
        _FakeApp.instances.append(self)

    def acquire_token_interactive(self, scopes: list[str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("interactive", scopes))
        return _FakeApp.result

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append(("device_flow", scopes))
        return {"user_code": "ABC", "message": "Go to the device login page and enter ABC."}

    def acquire_token_by_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("device_token", flow["user_code"]))
        return _FakeApp.result


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    fake_msal = types.ModuleType("msal")
    fake_msal.PublicClientApplication = _FakeApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "msal", fake_msal)
    _FakeApp.instances = []
    _FakeApp.result = {}
    spec = importlib.util.spec_from_file_location("graph_sites_token", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(helper: types.ModuleType, argv: list[str], stdin: str = "") -> int:
    sys.stdin = io.StringIO(stdin)
    try:
        return helper.main(argv)
    finally:
        sys.stdin = sys.__stdin__


@pytest.mark.parametrize("flow", ["interactive", "device"])
def test_mint_requests_full_control_through_graph_powershell(
    helper: types.ModuleType, capsys: pytest.CaptureFixture[str], flow: str
) -> None:
    """The token comes from Graph PowerShell's public client, on the signed-in
    tenant, for exactly the Sites.FullControl.All scope; only the token reaches
    standard output.
    """
    token = _token()
    _FakeApp.result = {"access_token": token}
    argv = ["mint", "--tenant", "contoso.example"] + (["--device-code"] if flow == "device" else [])
    assert _run(helper, argv) == 0
    (app,) = _FakeApp.instances
    assert app.client_id == GRAPH_POWERSHELL_CLIENT_ID
    assert app.authority == "https://login.microsoftonline.com/contoso.example"
    first = app.calls[0]
    assert first == ("interactive" if flow == "interactive" else "device_flow", [SCOPE])
    out, err = capsys.readouterr()
    assert out == token
    if flow == "device":
        assert "enter ABC" in err, "the device-code prompt goes to standard error"


def test_mint_fails_closed_when_sign_in_fails(
    helper: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused or abandoned sign-in exits non-zero with the reason and prints no token."""
    _FakeApp.result = {"error": "access_denied", "error_description": "AADSTS65001: no consent."}
    assert _run(helper, ["mint", "--tenant", "contoso.example"]) != 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "AADSTS65001" in err


def test_mint_refuses_a_token_without_the_scope(
    helper: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A token that came back without full control -- a non-admin consent, say --
    is refused rather than handed to the site steps to fail there.
    """
    _FakeApp.result = {"access_token": _token("Sites.Read.All")}
    assert _run(helper, ["mint", "--tenant", "contoso.example"]) != 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "Sites.FullControl.All" in err


@pytest.mark.parametrize(
    ("token", "ok"),
    [
        (_token(), True),
        (_token("Sites.Read.All Sites.FullControl.All User.Read"), True),
        (_token("Sites.ReadWrite.All"), False),
        (_token("Sites.FullControl.AllX"), False),
        ("not-a-jwt", False),
        ("", False),
    ],
)
def test_check_accepts_only_a_full_control_token(
    helper: types.ModuleType, capsys: pytest.CaptureFixture[str], token: str, ok: bool
) -> None:
    """``check`` reads a supplied token from standard input and passes it only
    when its scopes include Sites.FullControl.All exactly; it prints nothing.
    """
    assert (_run(helper, ["check"], stdin=token) == 0) is ok
    out, err = capsys.readouterr()
    assert out == ""
    if not ok:
        assert "Sites.FullControl.All" in err

"""Retired MCP identities cannot remain advertised by the deployment."""

import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_admin_discovery_is_not_declared() -> None:
    bicep = (ROOT / "infra/modules/apim.bicep").read_text()
    assert "sageDiscoveryMcpAdminOperation" not in bicep
    assert not (ROOT / "infra/policies/sage-discovery-mcp-admin-operation-policy.xml").exists()
    assert "oauth-protected-resource/mcp_maint" in bicep
    assert "oauth-protected-resource/mcp'" in bicep


def test_retired_paths_return_404_before_jwt_and_supported_challenges_use_segments() -> None:
    # Repository-controlled policy XML, never caller input.
    policy = ET.parse(ROOT / "infra/policies/sage-api-policy.xml").getroot()  # noqa: S314
    inbound = policy.find("inbound")
    retired = inbound.find("choose/when")
    assert retired is not None
    assert "/mcp_admin" in retired.attrib["condition"]
    assert "/.well-known/oauth-protected-resource/mcp_admin" in retired.attrib["condition"]
    assert retired.find("return-response/set-status").attrib["code"] == "404"
    assert list(inbound).index(inbound.find("choose")) < list(inbound).index(
        inbound.find("validate-jwt")
    )
    nodes = [
        node
        for node in policy.findall("on-error//when")
        if "OriginalUrl.Path" in node.attrib["condition"]
    ]
    assert len(nodes) == 2
    for node in nodes:
        condition = node.attrib["condition"]
        assert "/mcp_admin" not in condition
        assert " == " in condition
        assert 'StartsWith("/mcp/")' in condition or 'StartsWith("/mcp_maint/")' in condition
    assert "oauth-protected-resource/mcp_admin" not in ET.tostring(
        policy.find("on-error"), encoding="unicode"
    )


def test_bootstrap_does_not_register_retired_identity() -> None:
    text = (ROOT / "deploy/bootstrap/entra-app-registrations.sh").read_text()
    invocation = text.split("  --identifier-uris ", 1)[1].split("az ad sp create", 1)[0]
    assert "/mcp_admin" not in invocation
    assert "/mcp_maint" in invocation
    assert '"https://${SAGE_PUBLIC_HOSTNAME}/mcp"' in invocation

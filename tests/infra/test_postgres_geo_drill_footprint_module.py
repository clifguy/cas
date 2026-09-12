"""Structural gate for the geo restore drill's destination-region footprint.

Locks the shape of ``infra/modules/postgres-geo-drill-footprint.bicep``: the
throwaway network, private DNS zone and Container Apps environment a geo restore
of a private-access server needs in the region it lands in (CAS-ADR-042). The
drill driver deploys it into a resource group of the drill's own and deletes that
group afterwards, so four properties are load-bearing and asserted by value.

The module must create everything it uses and reference nothing that already
exists, because the destination region's other networks belong to other
workloads. Every resource must carry the drill's ownership tag under the key the
driver reads, because the driver refuses to delete a group holding anything
untagged. Its outputs must be named exactly as the driver reads them. And it must
never be wired into the serving template or carry a deletion lock, because it
exists only for the duration of a drill.

These checks read tracked Bicep text; the compile test is the authoritative
syntax check. Detectors live in pure helpers so the control tests can prove each
one actually fires.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MAIN_BICEP: Final[Path] = REPO_ROOT / "infra" / "main.bicep"
MODULE: Final[Path] = REPO_ROOT / "infra" / "modules" / "postgres-geo-drill-footprint.bicep"
DRIVER: Final[Path] = REPO_ROOT / "deploy" / "postgres-restore-verify.py"

_VNET_TYPE: Final[str] = "Microsoft.Network/virtualNetworks"
_ZONE_TYPE: Final[str] = "Microsoft.Network/privateDnsZones"
_LINK_TYPE: Final[str] = "Microsoft.Network/privateDnsZones/virtualNetworkLinks"
_ENV_TYPE: Final[str] = "Microsoft.App/managedEnvironments"
_LOCK_TYPE: Final[str] = "Microsoft.Authorization/locks"


def _strip_line_comments(text: str) -> str:
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    return re.search(rf"^resource\s+\w+\s+'{re.escape(resource_type)}@", text, re.M) is not None


def _resource_blocks(text: str) -> dict[str, str]:
    """Each top-level resource's symbolic name mapped to its declaration text."""
    starts = [
        (match.start(), match.group(1))
        for match in re.finditer(r"^resource\s+(\w+)\s+'[^']+'", text, re.M)
    ]
    ends = [match.start() for match in re.finditer(r"^(?:resource|output)\s", text, re.M)]
    blocks: dict[str, str] = {}
    for start, name in starts:
        end = min((position for position in ends if position > start), default=len(text))
        blocks[name] = text[start:end]
    return blocks


def _untagged_resources(text: str) -> list[str]:
    return [name for name, block in _resource_blocks(text).items() if "tags: tags" not in block]


def _references_existing(text: str) -> bool:
    return re.search(r"'\s+existing\s*=", text) is not None or "/subscriptions/" in text


def _delegations(text: str) -> list[str]:
    return re.findall(r"serviceName:\s*'([^']+)'", text)


def _outputs(text: str) -> set[str]:
    return set(re.findall(r"^output\s+(\w+)\s", text, re.M))


@pytest.fixture(scope="module")
def live() -> str:
    return _strip_line_comments(MODULE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("restore_verify_driver", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_driver_deploys_this_module(driver: ModuleType) -> None:
    assert Path(driver.FOOTPRINT_MODULE) == MODULE


def test_the_network_carries_exactly_the_two_delegations_a_drill_needs(live: str) -> None:
    assert _declares_resource_type(live, _VNET_TYPE)
    assert sorted(_delegations(live)) == [
        "Microsoft.App/environments",
        "Microsoft.DBforPostgreSQL/flexibleServers",
    ]


def test_the_private_dns_zone_is_a_postgres_zone_linked_to_its_own_network(live: str) -> None:
    assert _declares_resource_type(live, _ZONE_TYPE)
    blocks = _resource_blocks(live)
    zone = next(block for block in blocks.values() if f"'{_ZONE_TYPE}@" in block)
    assert re.search(r"name:\s*'[^']*\.postgres\.database\.azure\.com'", zone)
    link = next(block for block in blocks.values() if f"'{_LINK_TYPE}@" in block)
    assert re.search(r"virtualNetwork:\s*\{\s*id:\s*vnet\.id\s*\}", link)
    assert "registrationEnabled: false" in link


def test_the_environment_is_internal_and_sits_in_the_modules_own_subnet(live: str) -> None:
    blocks = _resource_blocks(live)
    environment = next(block for block in blocks.values() if f"'{_ENV_TYPE}@" in block)
    assert re.search(r"infrastructureSubnetId:\s*vnet\.properties\.subnets\[0\]\.id", environment)
    assert "internal: true" in environment


def test_the_module_references_no_pre_existing_resource(live: str) -> None:
    assert not _references_existing(live)


def test_every_resource_carries_the_drivers_ownership_tag(live: str, driver: ModuleType) -> None:
    assert _resource_blocks(live), "the detector must find resources to constrain anything"
    assert _untagged_resources(live) == []
    assert re.search(rf"var\s+tags\s*=\s*\{{\s*{re.escape(driver.DRILL_TAG)}:\s*runId\s*\}}", live)


def test_outputs_are_named_as_the_driver_reads_them(live: str, driver: ModuleType) -> None:
    assert _outputs(live) == set(driver.FOOTPRINT_OUTPUTS)


def test_module_declares_no_deletion_lock(live: str) -> None:
    assert not _declares_resource_type(live, _LOCK_TYPE)


def test_module_is_not_wired_into_the_serving_template() -> None:
    main = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert "postgres-geo-drill-footprint.bicep" not in main


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="requires the Bicep CLI or the Azure CLI's bicep extension",
)
def test_module_compiles(tmp_path: Path) -> None:
    outfile = tmp_path / "postgres-geo-drill-footprint.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(MODULE), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(MODULE), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
# ---------------------------------------------------------------------------


def test_untagged_detector_catches_a_resource_without_tags(live: str) -> None:
    mutated = live.replace("tags: tags", "", 1)
    assert len(_untagged_resources(mutated)) == 1


def test_existing_detector_catches_a_borrowed_network() -> None:
    borrowed = "resource other 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {\n}\n"
    assert _references_existing(borrowed)
    assert _references_existing("param subnet string = '/subscriptions/s/resourceGroups/x'")


def test_delegation_detector_catches_a_dropped_delegation(live: str) -> None:
    mutated = live.replace("'Microsoft.App/environments'", "'Microsoft.Web/serverFarms'")
    assert "Microsoft.App/environments" not in _delegations(mutated)


def test_resource_type_detector_controls() -> None:
    declared = "resource lock 'Microsoft.Authorization/locks@2020-05-01' = {\n}\n"
    commented = "// resource lock 'Microsoft.Authorization/locks@2020-05-01' = {\n"
    assert _declares_resource_type(declared, _LOCK_TYPE)
    assert not _declares_resource_type(_strip_line_comments(commented), _LOCK_TYPE)

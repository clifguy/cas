"""Schema validation for the committed cloud validation-vault seed config.

The cloud profile bootstraps its first vault by seeding a ``vault_config.yaml``
directly into the SharePoint document library (CAS-ADR-043); a deployed SAGE
discovers it at startup and loads it with ``VaultConfig.model_validate``. This
suite guards the committed seed so a config SAGE would reject can never be
uploaded -- a defect that would otherwise surface only at runtime, in the
cloud, as a discovery failure.

The seed's ``vault.id`` is also replicated as a bare literal in three other
artifacts that no schema check reaches: the document-library folder the
bootstrap script uploads into, the validation driver's fallback ``--vault-id``,
and the deploy gate's documented ``PREFLIGHT_EXPECTED_VAULTS`` list. Discovery
matches the folder name against the config it contains, so a divergence between
them yields a vault the deployed SAGE never registers -- with no error at seed
time, and with the deploy still green unless the gate was told to expect it. The
coupling tests below hold all four literals to the config.
"""

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sage.config import VaultConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]

SEED_CONFIG_PATH = _REPO_ROOT / "deploy" / "test-vault" / "vault_config.yaml"
SEED_SCRIPT_PATH = _REPO_ROOT / "deploy" / "bootstrap" / "seed-vault-source.sh"
VALIDATE_DRIVER_PATH = _REPO_ROOT / "deploy" / "sharepoint_validate.py"
DEPLOYMENT_DOC_PATH = _REPO_ROOT / "docs" / "process" / "azure-deployment.md"

# The folder segment the bootstrap script PUTs the seed config into, anchored on
# the surrounding URI so the capture cannot drift onto an unrelated path.
_SEED_UPLOAD_FOLDER_RE = re.compile(
    r"root:/\$\{VAULT_SOURCE_ROOT_PATH\}/([^/\"]+)/vault_config\.yaml:/content"
)

# The driver's fallback --vault-id, anchored on the environment variable name.
_DRIVER_DEFAULT_VAULT_RE = re.compile(
    r"os\.environ\.get\(\s*\"SP_VALIDATE_VAULT_ID\"\s*,\s*\"([^\"]+)\"\s*\)"
)

# The id quoted inside the --vault-id help string. Anchored on the help text
# itself rather than searched for anywhere in the file: a bare substring check
# would be satisfied by the same id appearing in an unrelated line, which is the
# one thing a help-text-staleness check must not accept.
_DRIVER_HELP_VAULT_RE = re.compile(r"help=\"[^\"]*\$SP_VALIDATE_VAULT_ID or '([^']+)'[^\"]*\"")

# Every documented PREFLIGHT_EXPECTED_VAULTS value, however it is written -- as a
# `gh variable set --body` argument or as an inline environment assignment.
_PREFLIGHT_EXPECTED_RE = re.compile(
    r"PREFLIGHT_EXPECTED_VAULTS(?:\s+--env\s+\"\$ENVIRONMENT\"\s+--body\s+\"([^\"]*)\"|=(\S+))"
)

# Top-level sections VaultConfig declares without a default (sage/config.py).
# Each must be present for a config to validate; the optional sections
# (adapter_defaults, abstraction, access_control_defaults, retrieval_health,
# timing) carry defaults and are intentionally excluded.
REQUIRED_SECTIONS = (
    "vault",
    "document_types",
    "lifecycle",
    "metadata_extraction",
    "edge_inference",
)


def _load_seed_dict() -> dict:
    return yaml.safe_load(SEED_CONFIG_PATH.read_text())


def _seeded_vault_id() -> str:
    return VaultConfig.model_validate(_load_seed_dict()).vault.id


def test_seed_config_is_schema_valid() -> None:
    """The committed seed validates through the exact call SAGE discovery uses."""
    cfg = VaultConfig.model_validate(_load_seed_dict())
    assert cfg.vault.id == "cloud_validation"


def test_seed_script_uploads_to_the_folder_the_seed_config_declares() -> None:
    """The bootstrap upload folder and the seed's ``vault.id`` are the same name.

    The two are independent literals in different files and different languages,
    coupled only by the convention that discovery finds a vault by walking the
    library for folders whose ``vault_config.yaml`` declares them. A divergence
    seeds a config the deployed SAGE silently never registers, so nothing before
    this check reports it.
    """
    folders = _SEED_UPLOAD_FOLDER_RE.findall(SEED_SCRIPT_PATH.read_text())
    assert folders, (
        f"{SEED_SCRIPT_PATH.name} must PUT the seed config to "
        "root:/${VAULT_SOURCE_ROOT_PATH}/<vault_id>/vault_config.yaml:/content"
    )
    seeded = _seeded_vault_id()
    # Every upload, not just the first: a second config PUT into a different
    # folder is the same divergence, and checking only one would not see it.
    for folder in folders:
        assert folder == seeded, (
            f"{SEED_SCRIPT_PATH.name} seeds into folder {folder!r} but "
            f"{SEED_CONFIG_PATH.name} declares vault.id {seeded!r}; "
            "discovery would not register the vault"
        )


def test_validate_driver_defaults_to_the_seeded_vault() -> None:
    """The validation driver's fallback --vault-id names the seeded vault.

    The driver mutates whatever vault it is pointed at, so a stale default aims
    a destructive probe at a vault id that no longer exists -- or, worse, at one
    that does and was never meant to receive it.
    """
    driver_text = VALIDATE_DRIVER_PATH.read_text()
    match = _DRIVER_DEFAULT_VAULT_RE.search(driver_text)
    assert match is not None, (
        f"{VALIDATE_DRIVER_PATH.name} must default --vault-id to "
        "$SP_VALIDATE_VAULT_ID with a literal fallback"
    )
    seeded = _seeded_vault_id()
    assert match.group(1) == seeded, (
        f"{VALIDATE_DRIVER_PATH.name} defaults to {match.group(1)!r} but the "
        f"committed seed declares {seeded!r}"
    )
    help_match = _DRIVER_HELP_VAULT_RE.search(driver_text)
    assert help_match is not None, (
        f"{VALIDATE_DRIVER_PATH.name} must document the --vault-id fallback in its help text"
    )
    assert help_match.group(1) == seeded, (
        f"the --vault-id help text names {help_match.group(1)!r} but the default "
        f"applied is {seeded!r}"
    )


def test_documented_preflight_expectation_covers_the_seeded_vault() -> None:
    """Every documented PREFLIGHT_EXPECTED_VAULTS value lists the seeded vault.

    The preflight gate asserts that each id in this comma-list came back from
    ``/sage_vaults``. Omitting the validation vault leaves the deploy green when
    the vault silently failed to load -- the exact outcome a divergence between
    the seed config and the upload folder produces, and the one condition the
    other coupling tests here cannot observe because it is a property of the
    running tenant rather than of the repository.
    """
    doc_text = DEPLOYMENT_DOC_PATH.read_text()
    matches = _PREFLIGHT_EXPECTED_RE.findall(doc_text)
    assert matches, f"{DEPLOYMENT_DOC_PATH.name} must document a PREFLIGHT_EXPECTED_VAULTS value"
    # Completeness, not just presence. The loop below can only judge the sites the
    # pattern matched, so a site written in a spelling the pattern does not cover
    # would be skipped silently rather than checked -- an unexamined site and a
    # clean one are indistinguishable in the result otherwise.
    assert len(matches) == doc_text.count("PREFLIGHT_EXPECTED_VAULTS"), (
        f"{DEPLOYMENT_DOC_PATH.name} mentions PREFLIGHT_EXPECTED_VAULTS "
        f"{doc_text.count('PREFLIGHT_EXPECTED_VAULTS')} times but this check parsed "
        f"{len(matches)}; an unparsed site is an unchecked site"
    )
    seeded = _seeded_vault_id()
    for body, inline in matches:
        value = body or inline
        ids = [v.strip() for v in value.split(",") if v.strip()]
        assert seeded in ids, (
            f"{DEPLOYMENT_DOC_PATH.name} documents PREFLIGHT_EXPECTED_VAULTS={value!r}, "
            f"which omits the seeded vault {seeded!r}; a deploy would pass without it"
        )


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_seed_config_rejects_missing_required_section(section: str) -> None:
    """Anti-coincidental guard: dropping any required section must fail validation.

    Proves the schema-valid assertion above is genuine -- a config missing a
    required section (which SAGE would refuse to load) does not slip through as
    a coincidental YAML-parses pass.
    """
    data = _load_seed_dict()
    del data[section]
    with pytest.raises(ValidationError):
        VaultConfig.model_validate(data)

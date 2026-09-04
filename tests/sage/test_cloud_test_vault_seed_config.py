"""Schema validation for the committed cloud validation-vault seed config.

The cloud profile bootstraps its first vault by seeding a ``vault_config.yaml``
directly into the SharePoint document library (CAS-ADR-043); a deployed SAGE
discovers it at startup and loads it with ``VaultConfig.model_validate``. This
suite guards the committed seed so a config SAGE would reject can never be
uploaded -- a defect that would otherwise surface only at runtime, in the
cloud, as a discovery failure.

The seed's ``vault.id`` is also replicated as a bare literal in four other
artifacts that no schema check reaches: the document-library folder the
bootstrap script uploads into, the validation driver's fallback ``--vault-id``,
the CI harness's ``SP_VALIDATE_VAULT_ID``, and the deploy gate's documented
``PREFLIGHT_EXPECTED_VAULTS`` list. Nothing reconciles them at runtime: discovery
enumerates the library's folder names but registers each vault under the
``vault.id`` its own config declares (``sage/app.py``), and a vault's sources are
then addressed at ``<root>/<registered id>/``. A divergence therefore does not
fail -- it splits. The vault registers and serves normally while its declaration
sits in one folder and its sources are addressed to another, so the split is
invisible to discovery, to the preflight vault check, and to any read that only
asks whether the vault is present. The coupling tests below hold all five
literals to the config, before that state can be created.

Each replica has its own structural gate elsewhere asserting a different property
of it -- that the harness targets a disposable vault rather than ``cas``, that
the seed config is schema-valid. Those answer "is this value acceptable?"; these
answer "is it the same value?", and a literal-against-literal check cannot.
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
VALIDATE_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "sharepoint-validate.yml"

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
    held together only by convention. Discovery reads the config out of whatever
    folder it finds and registers the vault under the id that config declares, so
    a divergence produces a vault that loads and serves while its declaration and
    its source tree live at different paths -- not a visible failure, which is why
    this has to be checked here rather than observed later.
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
            f"{SEED_CONFIG_PATH.name} declares vault.id {seeded!r}; the vault "
            "would register under the declared id with its declaration left in "
            "the other folder"
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


def test_documented_preflight_expectation_names_exactly_the_seeded_vault() -> None:
    """Every documented PREFLIGHT_EXPECTED_VAULTS value is exactly the seeded vault.

    The preflight gate asserts that each id in this comma-list came back from
    ``/sage_vaults``, and fails closed on any that did not. Two failures are
    possible and this checks both, which is why it asserts set equality rather
    than membership:

    - *Omitting* the seeded vault leaves the deploy green when that vault failed
      to load for any reason at all -- a seed that never ran, a config the schema
      rejected, a binding misconfigured for the tenant.
    - *Naming a vault the bootstrap does not create* is worse, because it fails
      closed forever rather than passing wrongly once. A local-only vault id
      documented here is unsatisfiable on every real tenant, and an operator
      following the runbook literally cannot get a green deploy.

    The runbook is bring-up documentation, and at bring-up the tenant has exactly
    the one vault ``seed-vault-source.sh`` seeds. A tenant that later adds vaults
    through the maintenance surface can extend its own live variable; the
    documented example must stay the reproducible minimum.

    That argument is strongest for the ``gh variable set`` line, which an operator
    copies verbatim during bring-up. It is applied to the hand-run preflight
    example too, where the value is illustrative rather than prescriptive: pinning
    both keeps one answer to "what does this repository claim a tenant has", and an
    example naming a vault the reader's tenant lacks teaches the wrong shape even
    where nothing enforces it.
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
        # Split exactly as the consumer does. deploy/cloud-preflight.sh sets
        # `local IFS=','` and iterates unquoted, which skips empty elements but
        # does NOT trim: with IFS holding only a comma, whitespace is not a
        # delimiter, so " cloud_validation" stays a distinct id and the gate's
        # grep for it fails. Trimming here would make this check more permissive
        # than the thing it checks -- it would pass a documented value the deploy
        # then rejects, which is the one outcome a documentation gate must not have.
        ids = {v for v in value.split(",") if v}
        assert ids == {seeded}, (
            f"{DEPLOYMENT_DOC_PATH.name} documents PREFLIGHT_EXPECTED_VAULTS={value!r}; "
            f"the bring-up example must name exactly the seeded vault {seeded!r}, "
            "comma-separated with no surrounding whitespace. "
            f"Missing: {sorted({seeded} - ids)}. "
            f"Not created by the bootstrap, or mis-spaced: {sorted(ids - {seeded})}"
        )


def test_validation_workflow_targets_the_seeded_vault() -> None:
    """The CI harness's ``SP_VALIDATE_VAULT_ID`` names the seeded vault.

    The workflow's own structural gate asserts this value against a literal, which
    answers a different question -- *which* vault the harness may mutate, never
    ``cas``. That check stays green when the seed config's id changes underneath
    it, so on its own the workflow is a fourth uncoupled replica of the id. This
    holds it to the config; the two together mean the harness targets a vault that
    is both disposable and the one actually seeded.
    """
    workflow = yaml.safe_load(VALIDATE_WORKFLOW_PATH.read_text())
    targets = [
        job["env"]["SP_VALIDATE_VAULT_ID"]
        for job in (workflow.get("jobs") or {}).values()
        if isinstance(job, dict) and "SP_VALIDATE_VAULT_ID" in (job.get("env") or {})
    ]
    assert targets, f"{VALIDATE_WORKFLOW_PATH.name} must set SP_VALIDATE_VAULT_ID in a job env"
    seeded = _seeded_vault_id()
    for target in targets:
        assert target == seeded, (
            f"{VALIDATE_WORKFLOW_PATH.name} points the harness at {target!r} but the "
            f"committed seed declares {seeded!r}; the harness would mutate a vault "
            "that is not the one this repository seeds"
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

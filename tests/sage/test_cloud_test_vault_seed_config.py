"""Schema validation for the committed cloud validation-vault seed config.

The cloud profile bootstraps its first vault by seeding a ``vault_config.yaml``
directly into the SharePoint document library (CAS-ADR-043); a deployed SAGE
discovers it at startup and loads it with ``VaultConfig.model_validate``. This
suite guards the committed seed so a config SAGE would reject can never be
uploaded -- a defect that would otherwise surface only at runtime, in the
cloud, as a discovery failure.

The seed's ``vault.id`` is also replicated as a bare literal in artifacts that
no schema check reaches: the document-library folder the bootstrap script
uploads into, the validation driver's fallback ``--vault-id``, the CI harness's
``SP_VALIDATE_VAULT_ID``, the deploy gate's documented
``PREFLIGHT_EXPECTED_VAULTS`` list, the operator runbooks' worked commands and
prose, and the seed config's own header comment and storage roots. Nothing
reconciles them at runtime: discovery enumerates the library's folder names but
registers each vault under the ``vault.id`` its own config declares
(``sage/app.py``), and a vault's sources are then addressed at
``<root>/<registered id>/``. A divergence therefore does not fail -- it splits.
The vault registers and serves normally while its declaration sits in one folder
and its sources are addressed to another, so the split is invisible to
discovery, to the preflight vault check, and to any read that only asks whether
the vault is present. The coupling tests below hold every replica to the config,
before that state can be created.

The replicas are held by an *inventory* rather than one test per site: every
tracked file that names the id is listed with the forms it names it in, each
form is anchored on structure where structure exists (a Graph path, an export
line, a YAML key, the startup banner's own log line) and on a narrowly pinned
phrase where only prose does, and two completeness controls close the sweep --
every occurrence of the id in a listed file must fall inside an anchored form,
and every tracked file naming the id must be listed. A replica added anywhere
in the tree therefore fails here rather than surfacing later as a stale
document.

The *root* segment of the same path is replicated the same way: the bootstrap
script's ``VAULT_SOURCE_ROOT_PATH`` default, the IaC parameter defaults, the
engine's ``root_path`` model default and its JSON-schema mirror, the maintenance
job's environment fallback, and the "defaults to" statements in the operator
docs. That divergence has the opposite shape: discovery lists vault folders
within the root, so a config seeded under one root and served under another is
never enumerated at all -- an absent vault rather than a split one.

Each replica has its own structural gate elsewhere asserting a different property
of it -- that the harness targets a disposable vault rather than ``cas``, that
the seed config is schema-valid. Those answer "is this value acceptable?"; these
answer "is it the same value?", and a literal-against-literal check cannot.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sage.config import StackDocumentStoreConfig, VaultConfig
from sage.maintenance._cloud_env import config_from_env

_REPO_ROOT = Path(__file__).resolve().parents[2]

SEED_CONFIG_PATH = _REPO_ROOT / "deploy" / "test-vault" / "vault_config.yaml"
SEED_SCRIPT_PATH = _REPO_ROOT / "deploy" / "bootstrap" / "seed-vault-source.sh"
VALIDATE_DRIVER_PATH = _REPO_ROOT / "deploy" / "sharepoint_validate.py"
DEPLOYMENT_DOC_PATH = _REPO_ROOT / "docs" / "process" / "azure-deployment.md"
VALIDATE_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "sharepoint-validate.yml"
RUNBOOK_PATH = _REPO_ROOT / "docs" / "process" / "sharepoint-vault-source.md"
POSTGRES_BOOTSTRAP_DOC_PATH = _REPO_ROOT / "docs" / "process" / "postgres-entra-bootstrap.md"
DELETE_VAULT_CLOUD_PATH = _REPO_ROOT / "sage" / "maintenance" / "delete_vault_cloud.py"
CORE_CONFIG_SCHEMA_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_config.schema.json"
INFRA_DIR = _REPO_ROOT / "infra"
MAIN_BICEP_PATH = INFRA_DIR / "main.bicep"
BICEPPARAM_EXAMPLE_PATH = INFRA_DIR / "main.bicepparam.example"
BICEPPARAM_PATH = INFRA_DIR / "main.bicepparam"

# The bootstrap script's root default, anchored on the whole parameter-expansion
# line: the operator may override the root through the environment, and the
# default is the value that applies when they do not.
_SEED_SCRIPT_ROOT_DEFAULT_RE = re.compile(
    r"^VAULT_SOURCE_ROOT_PATH=\"\$\{VAULT_SOURCE_ROOT_PATH:-([^}\"]*)\}\"\s*$", re.MULTILINE
)

# A Bicep declaration of vaultSourceRootPath *with* a default. A declaration
# without one (a module that is always handed the value) is not a replica.
_BICEP_ROOT_DEFAULT_RE = re.compile(
    r"^param\s+vaultSourceRootPath\s+string\s*=\s*'([^']*)'\s*$", re.MULTILINE
)

# The runbook's worked Graph paths -- the `az rest` commands an operator copies
# verbatim -- capturing the root and vault-id segments together.
_RUNBOOK_GRAPH_PATH_RE = re.compile(r"root:/([^/\"`<>\s]+)/([^/\"`<>\s]+)/vault_config\.yaml")


# The runbook's backticked `<root>/<vault_id>/` folder mentions, anchored on the
# resolved root default so a backticked repository path (`deploy/test-vault/`)
# is a non-match rather than a misdiagnosed replica. A folder mention under a
# *stale* root is then not captured either, and surfaces as an unanchored
# occurrence of the id in the sweep below -- still a failure, at the right line.
def _runbook_folder_re(root: str) -> re.Pattern[str]:
    return re.compile(rf"`{re.escape(root)}/([^/`<>\s]+)/`")


# The runbook's exported SP_VALIDATE_VAULT_ID, anchored on the export line.
_RUNBOOK_EXPORT_VAULT_RE = re.compile(r"^\s*export\s+SP_VALIDATE_VAULT_ID=(\S+)", re.MULTILINE)

# A prose statement of what the vault-source root defaults to, in the forms the
# docs use: the parameter name (or "root path") followed by "default(s) to/is"
# and a quoted value (Markdown backticks or Bicep-comment single quotes), or
# "the default `<root>` root". Anchored on the parameter's name or the word
# "root" so a neighbouring parameter's default is not captured.
_ROOT_DEFAULT_STATEMENT_RES = (
    re.compile(r"`?vaultSourceRootPath`?\s+defaults?\s+(?:to|is)\s+[`']([^`'\s]+)[`']"),
    re.compile(r"\bdefault\s+`([^`\s]+)`\s+root\b"),
    re.compile(r"\broot path defaults?\s+to\s+[`']([^`'\s]+)[`']"),
)
_ROOT_DEFAULT_DOCS = (RUNBOOK_PATH, BICEPPARAM_EXAMPLE_PATH, BICEPPARAM_PATH)
# The parameter's name outside a `<placeholder>`, or the prose "root path".
_ROOT_PARAM_NAME_RE = re.compile(r"(?<!<)vaultSourceRootPath|\broot path\b")
_DEFAULT_WORD_RE = re.compile(r"\bdefaults?\b")
# How far, in whitespace-flattened characters, the word "default" may sit from
# the parameter's name and still be the same statement.
_ROOT_STATEMENT_REACH = 60

# Bare prose mentions of the vault id, in the phrase forms the docs use:
# "`<id>` vault", "includes `<id>`" (what list_vaults returns), "confirm `<id>`",
# "exactly `<id>`", and the reStructuredText "vault is ``<id>``". Wording is
# pinned here, narrowly and deliberately: a bare mention has no structural
# anchor, and the alternative is not checking it. The completeness controls
# below keep a mention in an unlisted form from being skipped silently.
_MENTION_VAULT_RE = re.compile(r"`([^`\s]+)`\s+vault\b")
_MENTION_INCLUDES_RE = re.compile(r"\bincludes\s+`([^`\s]+)`")
_MENTION_CONFIRM_RE = re.compile(r"\bconfirm\s+`([^`\s]+)`")
_MENTION_EXACTLY_RE = re.compile(r"\bexactly\s+`([^`\s]+)`")
_MENTION_VAULT_IS_RE = re.compile(r"\bvault is ``([^`\s]+)``")

# The seed config's own replicas of its vault.id: the header comment that spells
# the library path the file is seeded to, and the storage roots, which sit at
# <...>/<vault_id>/<leaf>. The id line itself anchors the declaration.
_SEED_ID_LINE_RE = re.compile(r"^\s*id:\s*(\S+)\s*$", re.MULTILINE)
_SEED_HEADER_PATH_RE = re.compile(
    r"^#.*<vaultSourceRootPath>/([^/\s]+)/vault_config\.yaml", re.MULTILINE
)
_SEED_ROOTS_RE = re.compile(
    r"^\s*(?:storage_root|brain_root):\s*\S*/([^/\s]+)/[^/\s]+\s*$", re.MULTILINE
)

# The single id a documented PREFLIGHT_EXPECTED_VAULTS value carries, in either
# spelling. Used to anchor the occurrence; the value's completeness is asserted
# separately with the wider _PREFLIGHT_EXPECTED_RE below.
_PREFLIGHT_VALUE_RE = re.compile(
    r"PREFLIGHT_EXPECTED_VAULTS(?:\s+--env\s+\"\$ENVIRONMENT\"\s+--body\s+\"|=)([^\"\s\\,]+)"
)

# The startup banner's own "vaults loaded (N): <ids>" line (sage/startup_banner.py),
# quoted by the bootstrap doc as what the operator should see in the logs.
_VAULTS_LOADED_RE = re.compile(r"vaults loaded \(\d+\):\s*([^\s,]+)")

# The CI harness's job-level SP_VALIDATE_VAULT_ID, as a YAML key.
_WORKFLOW_ENV_VAULT_RE = re.compile(r"^\s*SP_VALIDATE_VAULT_ID:\s*(\S+)\s*$", re.MULTILINE)

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

# The inventory: every tracked file outside tests/ that names the seeded vault.
# The anchored-occurrence test walks each entry; the inventory test holds this
# tuple to what the tree actually contains. The forms each file names the id in
# are built per run by ``_vault_id_anchors``, because one of them (the runbook's
# folder mention) is anchored on the resolved root default.
_VAULT_ID_FILES: tuple[Path, ...] = (
    SEED_CONFIG_PATH,
    SEED_SCRIPT_PATH,
    VALIDATE_DRIVER_PATH,
    VALIDATE_WORKFLOW_PATH,
    DEPLOYMENT_DOC_PATH,
    RUNBOOK_PATH,
    POSTGRES_BOOTSTRAP_DOC_PATH,
    DELETE_VAULT_CLOUD_PATH,
)


def _vault_id_anchors(root_default: str) -> dict[Path, tuple[tuple[re.Pattern[str], int], ...]]:
    """The forms each inventoried file names the vault id in, as (pattern,
    capture group), for the given resolved root default."""
    return {
        SEED_CONFIG_PATH: ((_SEED_ID_LINE_RE, 1), (_SEED_HEADER_PATH_RE, 1), (_SEED_ROOTS_RE, 1)),
        SEED_SCRIPT_PATH: ((_SEED_UPLOAD_FOLDER_RE, 1),),
        VALIDATE_DRIVER_PATH: ((_DRIVER_DEFAULT_VAULT_RE, 1), (_DRIVER_HELP_VAULT_RE, 1)),
        VALIDATE_WORKFLOW_PATH: ((_WORKFLOW_ENV_VAULT_RE, 1), (_MENTION_VAULT_RE, 1)),
        DEPLOYMENT_DOC_PATH: ((_PREFLIGHT_VALUE_RE, 1), (_MENTION_EXACTLY_RE, 1)),
        RUNBOOK_PATH: (
            (_RUNBOOK_GRAPH_PATH_RE, 2),
            (_runbook_folder_re(root_default), 1),
            (_RUNBOOK_EXPORT_VAULT_RE, 1),
            (_MENTION_VAULT_RE, 1),
            (_MENTION_INCLUDES_RE, 1),
            (_MENTION_CONFIRM_RE, 1),
        ),
        POSTGRES_BOOTSTRAP_DOC_PATH: ((_VAULTS_LOADED_RE, 1), (_MENTION_INCLUDES_RE, 1)),
        DELETE_VAULT_CLOUD_PATH: ((_MENTION_VAULT_IS_RE, 1),),
    }


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


def _seed_script_root_default() -> str:
    matches = _SEED_SCRIPT_ROOT_DEFAULT_RE.findall(SEED_SCRIPT_PATH.read_text())
    assert len(matches) == 1, (
        f"{SEED_SCRIPT_PATH.name} must default the root exactly once, as "
        'VAULT_SOURCE_ROOT_PATH="${VAULT_SOURCE_ROOT_PATH:-<root>}"; '
        f"found {len(matches)} such line(s)"
    )
    return matches[0]


def _bicep_root_defaults() -> dict[Path, str]:
    """Every Bicep file under ``infra/`` that declares a ``vaultSourceRootPath``
    default, mapped to that default. Walks the whole tree so a module default
    is held to the same value as the entry point's -- a module that diverges is
    inert only for as long as the entry point keeps threading the value in.
    """
    return {
        path: match.group(1)
        for path in sorted(INFRA_DIR.rglob("*.bicep"))
        if (match := _BICEP_ROOT_DEFAULT_RE.search(path.read_text())) is not None
    }


def _root_default_replicas() -> dict[str, str]:
    """Every declared default for the vault-source root, keyed by where it lives:
    the bootstrap script, each Bicep file declaring one, the engine's config
    model and its JSON-schema mirror, and the maintenance job's environment
    fallback (exercised by building the config with the root unset, so the
    fallback is read as behaviour rather than as source text).
    """

    def rel(path: Path) -> str:
        return path.relative_to(_REPO_ROOT).as_posix()

    replicas = {rel(SEED_SCRIPT_PATH): _seed_script_root_default()}
    for path, default in _bicep_root_defaults().items():
        replicas[rel(path)] = default
    replicas["sage.config.StackDocumentStoreConfig.root_path"] = (
        StackDocumentStoreConfig.model_fields["root_path"].default
    )
    schema = json.loads(CORE_CONFIG_SCHEMA_PATH.read_text())
    replicas[f"{rel(CORE_CONFIG_SCHEMA_PATH)}#document_store.root_path"] = schema["properties"][
        "document_store"
    ]["properties"]["root_path"]["default"]
    job_env = {
        "PG_FQDN": "pg.example.invalid",
        "PG_DATABASE": "sage",
        "PG_USER": "sage",
        "SHAREPOINT_SITE_ID": "site",
        "SHAREPOINT_DRIVE_ID": "drive",
    }
    replicas["sage.maintenance._cloud_env.config_from_env (SHAREPOINT_ROOT_PATH unset)"] = (
        config_from_env(job_env).document_store.root_path
    )
    return replicas


def _main_bicep_root_default() -> str:
    """The IaC parameter's default -- the referent every other replica is held to."""
    default = _bicep_root_defaults().get(MAIN_BICEP_PATH)
    assert default is not None, (
        f"{MAIN_BICEP_PATH.relative_to(_REPO_ROOT)} must declare a default for "
        "vaultSourceRootPath (param vaultSourceRootPath string = '<root>')"
    )
    return default


def _root_default_statement_sites(text: str) -> int:
    """How many times ``text`` names the root parameter within reach of the word
    "default". Measured over whitespace-flattened text so a statement reflowed
    across a line break counts once, the same as the statement patterns match it.
    """
    flat = re.sub(r"\s+", " ", text)
    return sum(
        1
        for match in _ROOT_PARAM_NAME_RE.finditer(flat)
        if _DEFAULT_WORD_RE.search(
            flat, max(0, match.start() - _ROOT_STATEMENT_REACH), match.end() + _ROOT_STATEMENT_REACH
        )
    )


def _text_or_none(path: Path) -> str | None:
    """The file's text, or ``None`` for a file that is not UTF-8 text."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _line_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _unanchored_occurrences(text: str, token: str, anchored_starts: set[int]) -> list[int]:
    """Line numbers of every whole-word occurrence of ``token`` in ``text`` that
    no anchored capture starts at."""
    pattern = re.compile(rf"\b{re.escape(token)}\b")
    return [
        _line_at(text, m.start())
        for m in pattern.finditer(text)
        if m.start() not in anchored_starts
    ]


def _tracked_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [_REPO_ROOT / entry.decode() for entry in listing.split(b"\0") if entry]


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


def test_every_root_default_is_the_bicep_default() -> None:
    """Every declared default for the vault-source root is the IaC parameter's.

    The seed upload roots the vault tree at the bootstrap script's default; a
    deployed SAGE roots discovery at whatever it is handed, which under the IaC
    is the Bicep default, and outside it -- a hand-built job environment, a
    config that omits ``root_path`` -- is the engine's own default or the
    maintenance job's fallback. Discovery lists vault folders *within* the root,
    so a config seeded under one root and served under another is never
    enumerated: the vault is simply absent, with nothing to say that two halves
    of the bootstrap disagreed about where to look. Each default is a bare
    literal in its own language. The Bicep parameter is the one a tenant
    overrides, so it is the referent; this holds the *defaults* together and
    says nothing about a tenant that overrides one of them.
    """
    replicas = _root_default_replicas()
    referent_key = MAIN_BICEP_PATH.relative_to(_REPO_ROOT).as_posix()
    assert referent_key in replicas, (
        f"{referent_key} must declare a default for vaultSourceRootPath "
        "(param vaultSourceRootPath string = '<root>')"
    )
    referent = replicas[referent_key]
    for where, default in replicas.items():
        assert default == referent, (
            f"{where} defaults the vault-source root to {default!r} but {referent_key} "
            f"defaults vaultSourceRootPath to {referent!r}; a vault seeded under one "
            "root and served under the other is absent rather than split"
        )


def test_runbook_commands_address_the_seeded_vault_under_the_default_root() -> None:
    """Every worked path in the operator runbook names the seeded vault under
    the default root.

    The runbook is the hand-run bring-up procedure -- the one path with no gate
    behind it -- and its ``az rest`` commands are copied verbatim, so a stale
    path there seeds the wrong folder as surely as a stale script would. Both
    segments are checked: the vault id against the committed seed, and the
    root against the Bicep default the runbook says its paths assume.

    Anchored on the path forms (the Graph ``root:/…/vault_config.yaml`` command
    and the backticked ``<root>/<id>/`` folder mention), not on the prose around
    them, so an unrelated edit does not trip it.
    """
    text = RUNBOOK_PATH.read_text()
    root_default = _main_bicep_root_default()
    graph_paths = list(_RUNBOOK_GRAPH_PATH_RE.finditer(text))
    folder_mentions = list(_runbook_folder_re(root_default).finditer(text))
    assert graph_paths, f"{RUNBOOK_PATH.name} must carry a worked root:/…/vault_config.yaml command"
    assert folder_mentions, f"{RUNBOOK_PATH.name} must name the `{root_default}/<vault_id>/` folder"
    # Completeness: every Graph path in the runbook must have parsed, or a site
    # written in a form the pattern does not cover is skipped rather than checked.
    assert len(graph_paths) == text.count("root:/"), (
        f"{RUNBOOK_PATH.name} carries {text.count('root:/')} root:/ paths but this "
        f"check parsed {len(graph_paths)}; an unparsed site is an unchecked site"
    )
    seeded = _seeded_vault_id()
    for match in graph_paths:
        root, vault_id = match.group(1), match.group(2)
        line = _line_at(text, match.start())
        assert vault_id == seeded, (
            f"{RUNBOOK_PATH.name}:{line} addresses vault {vault_id!r} but "
            f"{SEED_CONFIG_PATH.name} declares vault.id {seeded!r}; an operator "
            "following the runbook seeds a folder the config does not name"
        )
        assert root == root_default, (
            f"{RUNBOOK_PATH.name}:{line} assumes root {root!r} but "
            f"{MAIN_BICEP_PATH.name} defaults vaultSourceRootPath to {root_default!r}; "
            "the runbook's paths claim to assume the default root"
        )
    for match in folder_mentions:
        assert match.group(1) == seeded, (
            f"{RUNBOOK_PATH.name}:{_line_at(text, match.start())} names folder "
            f"{root_default}/{match.group(1)}/ but {SEED_CONFIG_PATH.name} declares "
            f"vault.id {seeded!r}"
        )


def test_documented_root_default_is_the_bicep_default() -> None:
    """Every prose statement of what the vault-source root defaults to -- in the
    runbook, the parameter-set example, and the tenant parameter set -- states
    the Bicep default.

    These are wording, not configuration, and nothing consumes them; but the
    runbook is what an operator reads while deciding whether to override the
    root, and the parameter sets are the files they write the override into. A
    stale "defaults to" teaches the wrong shape at exactly the moment it matters.

    Completeness control: every site where the parameter's name (or "root path")
    sits within reach of the word "default" -- measured over whitespace-flattened
    text, so a reflowed statement counts the same as an unwrapped one -- must
    have yielded a capture, so a statement in a spelling the patterns do not
    cover fails here rather than passing unread.
    """
    root_default = _main_bicep_root_default()
    for path in _ROOT_DEFAULT_DOCS:
        text = path.read_text()
        statements = [m for pattern in _ROOT_DEFAULT_STATEMENT_RES for m in pattern.finditer(text)]
        assert statements, f"{path.name} must state what the vault-source root defaults to"
        sites = _root_default_statement_sites(text)
        assert len(statements) == sites, (
            f"{path.name} names the root parameter within reach of a default at {sites} "
            f"site(s) but this check read {len(statements)} statement(s); an unread "
            "statement is an unchecked one"
        )
        for match in sorted(statements, key=lambda m: m.start()):
            assert match.group(1) == root_default, (
                f"{path.name}:{_line_at(text, match.start())} says the vault-source root "
                f"defaults to {match.group(1)!r} but {MAIN_BICEP_PATH.name} defaults "
                f"vaultSourceRootPath to {root_default!r}"
            )


def test_root_default_statement_inventory_is_complete() -> None:
    """The documents held by the statement check are exactly the tracked files
    that state the root default.

    The per-document check can only read the documents it is told about, so a
    "defaults to" statement written into a document it does not list would pass
    unread. This holds the list to the tree with the same site predicate the
    per-document check counts with: any tracked text file outside ``tests/``
    that names the root parameter within reach of the word "default" must be
    listed, and a listed document that stops stating the default must be
    dropped.
    """
    stating = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _tracked_files()
        if path.relative_to(_REPO_ROOT).parts[0] != "tests"
        and path.is_file()
        and (text := _text_or_none(path)) is not None
        and _root_default_statement_sites(text)
    }
    listed = {path.relative_to(_REPO_ROOT).as_posix() for path in _ROOT_DEFAULT_DOCS}
    assert stating == listed, (
        f"Tracked files stating the root default but not held by the check: "
        f"{sorted(stating - listed)}. Held but no longer stating it: {sorted(listed - stating)}"
    )


@pytest.mark.parametrize(
    "path", sorted(_VAULT_ID_FILES), ids=lambda p: p.relative_to(_REPO_ROOT).as_posix()
)
def test_every_occurrence_of_the_vault_id_is_anchored_to_the_seed(path: Path) -> None:
    """Every occurrence of the vault id in an inventoried file sits in a form the
    inventory reads for that file, and every such form names the seeded vault.

    The per-site tests above assert each replica's own stronger property (the
    upload folder, the driver default and its help text, the preflight list's
    exact membership, the harness env). This one is the sweep behind them: it
    reads the same value at every site the inventory lists, so a stale replica
    is reported by file and line, and it refuses an occurrence that no listed
    form covers -- the only way a mention can be written that the sweep does not
    reach is to add a form for it here.

    Anti-coincidental-pass: with the seed's id changed and nothing else, every
    inventoried file fails on its first anchored capture; with one site changed
    alone, only that file fails, naming the line. A mention appended in a form
    the inventory does not list fails the unanchored-occurrence assertion, not
    silently passes it -- which is what makes the induction hold: every
    occurrence was anchored when it was written, so a stale one is always at an
    anchored site.
    """
    text = path.read_text()
    rel = path.relative_to(_REPO_ROOT).as_posix()
    seeded = _seeded_vault_id()
    captures = [
        (match.start(group), match.group(group))
        for pattern, group in _vault_id_anchors(_main_bicep_root_default())[path]
        for match in pattern.finditer(text)
    ]
    assert captures, f"{rel} carries none of the forms the inventory reads for it"
    for start, value in sorted(captures):
        # The phrase forms capture whatever id sits in them, so a deliberate
        # reference to a different vault reads as a stale replica here. That is
        # the accepted cost of not skipping stale mentions; the remedy is named.
        assert value == seeded, (
            f"{rel}:{_line_at(text, start)} names vault {value!r} but "
            f"{SEED_CONFIG_PATH.name} declares vault.id {seeded!r}. If this is a "
            "deliberate reference to another vault, phrase it outside the listed "
            "forms (for example 'the vault `<id>`' rather than '`<id>` vault')"
        )
    stray = _unanchored_occurrences(text, seeded, {start for start, _ in captures})
    assert not stray, (
        f"{rel} names the seeded vault at line(s) {stray} in a form the inventory does "
        "not read for this file; phrase it in a listed form or add an anchor for it"
    )


def test_vault_id_anchor_inventory_is_complete() -> None:
    """The inventory lists exactly the tracked files that name the seeded vault.

    The anchored-occurrence test can only sweep the files it is told about, so a
    replica written into a file the inventory does not list would be unreachable
    -- which is how replicas accumulated unnoticed before the inventory existed.
    This holds the key set to the tree: a new file naming the
    vault must be listed with the forms it uses, and a file that stops naming it
    must be dropped, so the inventory can neither lag the tree nor pad it.

    ``tests/`` is excluded because it is the checking side, not a replica.
    """
    seeded = _seeded_vault_id()
    needle = re.compile(rb"\b" + re.escape(seeded.encode()) + rb"\b")
    naming = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _tracked_files()
        if path.relative_to(_REPO_ROOT).parts[0] != "tests"
        and path.is_file()
        and needle.search(path.read_bytes())
    }
    inventory = {path.relative_to(_REPO_ROOT).as_posix() for path in _VAULT_ID_FILES}
    assert naming == inventory, (
        f"Tracked files naming {seeded!r} but missing from the inventory: "
        f"{sorted(naming - inventory)}. Inventoried but no longer naming it: "
        f"{sorted(inventory - naming)}"
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

"""Shared-component version parity gate.

Three versions have to agree across surfaces that are built and run by
different toolchains: the Postgres majors (the deployed Flexible Server's, and
the one the workstation and the CI service containers run), the Python version,
and the Node major. ``versions.json`` at the repository root is their single
declaration.

Consumers that can read a file when they need the value do so -- the
infrastructure template through ``loadJsonContent`` at compile time, the
workflows through the ``versions`` prelude job's outputs, this suite through
``tests.helpers.versions``. Consumers that structurally cannot restate the
value, and this gate holds them to the declaration: a Dockerfile ``FROM`` line
(which must also keep its digest pin where Dependabot can read it), a
``pyproject.toml`` key, a required check's context name, and prose in the
runbooks.

``docs/process/shared-version-parity.md`` states which components are held in
parity and which differences are structural and therefore out of scope. The
checks below follow the structural-gate style of
``tests/infra/test_frontend_node_version.py``: every check proves it located
its sites before comparing them, so a parser that silently matches nothing
cannot pass vacuously, and the controls at the end prove each detector fires on
the regression it targets.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.helpers.versions import (
    REPO_ROOT,
    VERSIONS_MANIFEST,
    declared_versions,
    major_of,
    node_major,
    parse_ruff_target,
    postgres_deploy_major,
    postgres_dev_major,
    python_version,
    ruff_target_version,
)

WORKFLOWS_DIR: Final[Path] = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW: Final[Path] = WORKFLOWS_DIR / "ci.yml"
BUILD_IMAGES_WORKFLOW: Final[Path] = WORKFLOWS_DIR / "build-images.yml"
POSTGRES_BICEP: Final[Path] = REPO_ROOT / "infra" / "modules" / "postgres.bicep"
MAIN_BICEP: Final[Path] = REPO_ROOT / "infra" / "main.bicep"
DOCKERFILE: Final[Path] = REPO_ROOT / "Dockerfile"
DOCKERFILE_BFF: Final[Path] = REPO_ROOT / "Dockerfile.bff"
PYPROJECT: Final[Path] = REPO_ROOT / "pyproject.toml"
BRANCH_PROTECTION: Final[Path] = REPO_ROOT / "docs" / "process" / "branch_protection.md"
PARITY_DOC: Final[Path] = REPO_ROOT / "docs" / "process" / "shared-version-parity.md"
POSTGRES_RUNBOOKS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "docs" / "process" / "postgres-local-runtime.md",
    REPO_ROOT / "docs" / "process" / "postgres-backup-restore.md",
)
PYTHON_PROSE: Final[tuple[Path, ...]] = (REPO_ROOT / "README.md", REPO_ROOT / "CLAUDE.md")

# The CI job whose service container runs the deploy floor rather than the
# development major, and whose display name is the branch ruleset's required
# check context.
DEPLOY_FLOOR_JOB: Final[str] = "storage-deploy-floor"

# Every Postgres service container this repository declares: three on the
# development major, one on the deploy floor. A hard count rather than a
# lower bound, so a site that is added without being bound to the manifest --
# or one that silently disappears from the walk -- fails here.
EXPECTED_PG_SERVICE_COUNT: Final[int] = 4

# Files permitted to lint and format below the declared Python, and the target
# each uses. The deploy probes run on the runner's system interpreter rather
# than a provisioned one; see the reason recorded in `pyproject.toml`. Pinned
# rather than open so a carve-out cannot quietly exempt anything else.
EXPECTED_RUFF_CARVE_OUTS: Final[dict[str, str]] = {"deploy/*.py": "py312"}

_PGVECTOR_TAG = re.compile(r"pgvector/pgvector:pg(?P<tag>[^\s\"']+)")

# The exact image reference each Postgres service must carry. Compared by
# equality rather than containment: containment constrains only that the
# expression appears somewhere in the string, which an image like
# ``pgvector/pgvector:pg16-${{ ... }}`` satisfies while pinning the wrong tag.
_FLOOR_IMAGE: Final[str] = "pgvector/pgvector:pg${{ needs.versions.outputs.postgres_deploy_major }}"
_DEV_IMAGE: Final[str] = "pgvector/pgvector:pg${{ needs.versions.outputs.postgres_dev_major }}"
_BICEP_LINE_COMMENT = re.compile(r"//.*$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _strip_bicep_comments(text: str) -> str:
    """Drop ``//`` line comments so a match cannot come from prose.

    The same shape ``tests/infra/test_postgres_module.py`` uses. Bicep string
    literals in this repository carry no ``//``, so a naive strip is safe here.
    """
    return _BICEP_LINE_COMMENT.sub("", text)


class _ModuleParameters:
    """Which parameters a nested-template deployment is and is not passed."""

    def __init__(self, passed: set[str]) -> None:
        self.passed = passed

    @property
    def absent(self) -> frozenset[str]:
        return frozenset({"postgresVersion"} - self.passed)


def _emitted_module_parameter_names(template: dict[str, Any]) -> _ModuleParameters:
    """Names the parent supplies to the deployment carrying ``postgresVersion``.

    Reads the ``Microsoft.Resources/deployments`` resource whose nested template
    declares the parameter, and returns the parameter names its
    ``properties.parameters`` supplies -- the caller-side overrides.
    """
    for resource in template.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        nested = ((resource.get("properties") or {}).get("template") or {}).get("parameters")
        if isinstance(nested, dict) and "postgresVersion" in nested:
            supplied = (resource.get("properties") or {}).get("parameters") or {}
            return _ModuleParameters(set(supplied.keys()))
    return _ModuleParameters(set())


def _emitted_postgres_version_default(node: Any) -> tuple[str | None, dict[str, Any]]:
    """Find the compiled ``postgresVersion`` default and its template's variables.

    The Postgres module is emitted as a nested template inside ``main.json``, so
    the parameter and the generated variable holding the loaded manifest live in
    the same nested scope; both are returned together because the assertion
    needs to resolve one against the other.
    """
    if isinstance(node, dict):
        parameters = node.get("parameters")
        if isinstance(parameters, dict) and "postgresVersion" in parameters:
            default = (parameters["postgresVersion"] or {}).get("defaultValue")
            return (default if isinstance(default, str) else None, node.get("variables") or {})
        for value in node.values():
            found = _emitted_postgres_version_default(value)
            if found[0] is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _emitted_postgres_version_default(value)
            if found[0] is not None:
                return found
    return None, {}


def _workflow_jobs(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs") or {}


def _postgres_service_images() -> dict[tuple[str, str], str]:
    """Every ``pgvector/pgvector`` service image, keyed by (workflow, job)."""
    found: dict[tuple[str, str], str] = {}
    for path in (CI_WORKFLOW, BUILD_IMAGES_WORKFLOW):
        for job_id, job in _workflow_jobs(path).items():
            for service in ((job or {}).get("services") or {}).values():
                image = str((service or {}).get("image", ""))
                if "pgvector/pgvector" in image:
                    found[(path.name, job_id)] = image
    return found


def _version_pins(action_prefix: str, key: str) -> list[tuple[str, str, str]]:
    """Every ``key`` input given to a ``action_prefix`` step, across all workflows.

    Returns (workflow, job, pin) triples. Both Python and Node pins ride
    actions in this repository -- ``astral-sh/setup-uv``, ``astral-sh/ruff-action``
    and ``actions/setup-node`` -- rather than a version file, so the walk is by
    action name.
    """
    pins: list[tuple[str, str, str]] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for job_id, job in _workflow_jobs(path).items():
            for step in (job or {}).get("steps") or []:
                if not str((step or {}).get("uses", "")).startswith(action_prefix):
                    continue
                pin = ((step or {}).get("with") or {}).get(key)
                if pin is not None:
                    pins.append((path.name, job_id, str(pin)))
    return pins


def _python_version_pins() -> list[tuple[str, str, str]]:
    return (
        _version_pins("astral-sh/setup-uv", "python-version")
        + _version_pins("astral-sh/ruff-action", "python-version")
        + _version_pins("actions/setup-python", "python-version")
    )


def _runtime_stage_text() -> str:
    """The SAGE image's runtime stage.

    The runtime stage copies only the venv, model cache and source from the
    builder, never the builder's apt binaries, so a client installed upstream
    would not be present at run time. Matching the same marker
    ``tests/deploy/test_sage_container_image.py`` uses.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    marker = "AS runtime"
    index = text.find(marker)
    assert index != -1, f"no `AS runtime` stage found in {DOCKERFILE}"
    return text[index:]


# --------------------------------------------------------------------------- #
# The manifest itself                                                          #
# --------------------------------------------------------------------------- #


def test_manifest_declares_every_shared_component() -> None:
    """Each held component is declared, as a non-empty string.

    The accessors in ``tests.helpers.versions`` assert the shape, so calling
    them all is the check. Strings rather than numbers because Bicep's
    ``version`` property takes a string and a JSON ``3.14`` would be a float.
    """
    for accessor in (postgres_deploy_major, postgres_dev_major, python_version, node_major):
        accessor()
    assert major_of(postgres_dev_major()) >= major_of(postgres_deploy_major()), (
        "the development Postgres major must not sit below the deploy floor; the floor "
        "exists to catch statements valid only on the higher major."
    )


def test_manifest_points_at_the_parity_document() -> None:
    """The manifest names the document stating the rule's scope, and it exists.

    JSON carries no comments, so this pointer is the only thing a reader who
    opens ``versions.json`` first has to go on.
    """
    documentation = declared_versions().get("documentation")
    assert documentation, f"{VERSIONS_MANIFEST} must carry a `documentation` pointer"
    assert (REPO_ROOT / documentation).is_file(), (
        f"{VERSIONS_MANIFEST} points at {documentation}, which does not exist"
    )
    assert (REPO_ROOT / documentation) == PARITY_DOC


# --------------------------------------------------------------------------- #
# Postgres -- the deploy major                                                 #
# --------------------------------------------------------------------------- #


def test_bicep_reads_the_manifest_for_the_deploy_major() -> None:
    """The infra module loads the declared major instead of restating it.

    This is the check that makes the Bicep a *reader*. Comments are stripped
    first, so a ``loadJsonContent`` mentioned only in prose cannot satisfy it;
    and the parameter must carry no bare quoted literal, so a hand-written
    default cannot sit alongside the load.
    """
    source = _strip_bicep_comments(POSTGRES_BICEP.read_text(encoding="utf-8"))
    param_lines = [ln for ln in source.splitlines() if "param postgresVersion" in ln]
    assert len(param_lines) == 1, (
        f"expected exactly one `param postgresVersion` declaration in {POSTGRES_BICEP}; "
        f"found {len(param_lines)}"
    )
    declaration = param_lines[0]
    assert re.search(r"loadJsonContent\(\s*'[^']*versions\.json'\s*\)", declaration), (
        f"postgresVersion must default to a loadJsonContent read of versions.json; "
        f"got: {declaration.strip()}"
    )
    assert "postgres.deploy_major" in declaration, (
        f"postgresVersion must resolve `postgres.deploy_major`; got: {declaration.strip()}"
    )
    assert not re.search(r"=\s*'\d+'", declaration), (
        f"postgresVersion still carries a literal default: {declaration.strip()}"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_bicep_compiles_with_the_loaded_default(tmp_path: Path) -> None:
    """The compiled ARM resolves the server version from the loaded manifest.

    A compile-time file load is transformed on the way to ARM, and that
    transform is not reproduced by reading the Bicep. Asserting the source form
    alone is a drift guard, not a correctness proof, so this reads the emitted
    artifact.

    It asserts the *parameter* resolves the load, not merely that the manifest
    bytes were embedded. Embedding happens for any ``loadJsonContent`` anywhere
    in the module, including one assigned to a variable nothing consumes -- so
    a template that embedded the manifest and still defaulted ``postgresVersion``
    to a literal would satisfy a contents-only check while deploying the wrong
    major. Both halves are asserted here: the default is an expression over the
    loaded variable, and that variable carries the declared major.
    """
    outfile = tmp_path / "main.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(MAIN_BICEP), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(MAIN_BICEP), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"

    template = json.loads(outfile.read_text(encoding="utf-8"))
    default, variables = _emitted_postgres_version_default(template)
    assert default is not None, (
        "no postgresVersion parameter found in the compiled template; the "
        "Postgres module did not reach the emitted ARM"
    )
    # Bicep lowers a compile-time file load to a generated variable ($fxv#N)
    # holding the file's parsed contents, and the parameter default becomes an
    # expression over it.
    match = re.fullmatch(r"\[variables\('([^']+)'\)\.postgres\.deploy_major\]", default)
    assert match is not None, (
        f"postgresVersion's emitted default must be an expression resolving "
        f"postgres.deploy_major from a loaded variable; got {default!r}"
    )
    loaded = variables.get(match.group(1))
    assert isinstance(loaded, dict), (
        f"the emitted default reads variable {match.group(1)!r}, which the template "
        f"does not define as an object; got {loaded!r}"
    )
    assert loaded.get("postgres", {}).get("deploy_major") == postgres_deploy_major(), (
        f"the variable the emitted default reads carries "
        f"{loaded.get('postgres', {}).get('deploy_major')!r}, not the declared "
        f"{postgres_deploy_major()!r}"
    )

    # A default is only the effective value while no caller overrides it. A
    # parent-side `postgresVersion:` on the module call is emitted as a
    # `{value: ...}` entry with no defaultValue, which the walker above skips --
    # so every assertion so far would stay green while the server deployed a
    # literal. Assert the override is absent rather than inferring it.
    overrides = _emitted_module_parameter_names(template)
    assert "postgresVersion" in overrides.absent, (
        "a caller passes postgresVersion to the Postgres module, so the manifest-derived "
        "default is not the deployed value; remove the override or make it read the manifest"
    )


def test_container_pg_client_matches_the_deploy_major() -> None:
    """The shipped image's Postgres client major matches the deployed server.

    A client older than the server cannot read its dumps; a client newer than
    the server is the case ``pg_dump`` refuses outright.
    """
    major = postgres_deploy_major()
    assert f"postgresql-client-{major}" in _runtime_stage_text(), (
        f"the runtime stage must install postgresql-client-{major} to match the "
        f"declared Flexible Server major (versions.json postgres.deploy_major)"
    )


def test_deploy_floor_service_image_is_the_deploy_major() -> None:
    """The deploy-floor CI job's service container runs the declared floor."""
    images = _postgres_service_images()
    floor = [img for (_, job), img in images.items() if job == DEPLOY_FLOOR_JOB]
    assert len(floor) == 1, (
        f"expected exactly one Postgres service on job {DEPLOY_FLOOR_JOB}; found {floor}"
    )
    assert floor[0] == _FLOOR_IMAGE, (
        f"the deploy-floor service image must be exactly {_FLOOR_IMAGE!r}, reading the "
        f"declared floor from the versions prelude job; got {floor[0]!r}"
    )


def test_deploy_floor_check_context_names_the_deploy_major() -> None:
    """The required check's context name carries the floor major, in all three copies.

    The name is a literal on purpose: it is the branch ruleset's required
    status-check context, and a context templated from a job output would
    change whenever the declared major did, blocking every merge while GitHub
    waited on a context that never arrives. That makes the name a restating
    site, held here -- together with its two copies in ``branch_protection.md``,
    the human-readable table and the captured ruleset JSON.

    A change to the declared floor therefore also needs the *live* ruleset
    edited, which no test can reach; ``branch_protection.md`` says so.
    """
    major = postgres_deploy_major()
    expected = f"storage tests on the deploy floor (pg{major})"

    job = _workflow_jobs(CI_WORKFLOW).get(DEPLOY_FLOOR_JOB)
    assert job is not None, f"no `{DEPLOY_FLOOR_JOB}` job in {CI_WORKFLOW}"
    assert job.get("name") == expected, (
        f"the {DEPLOY_FLOOR_JOB} job's name is the required check context and must be "
        f"{expected!r}; got {job.get('name')!r}"
    )

    protection = BRANCH_PROTECTION.read_text(encoding="utf-8")
    table_rows = [ln for ln in protection.splitlines() if ln.startswith("| `storage tests")]
    assert len(table_rows) == 1, (
        f"expected exactly one required-check table row for the deploy floor in "
        f"{BRANCH_PROTECTION}; found {len(table_rows)}"
    )
    assert f"`{expected}`" in table_rows[0], (
        f"the required-check table row must name {expected!r}; got {table_rows[0]!r}"
    )

    ruleset_lines = [ln for ln in protection.splitlines() if '"context": "storage tests' in ln]
    assert len(ruleset_lines) == 1, (
        f"expected exactly one captured-ruleset context entry for the deploy floor in "
        f"{BRANCH_PROTECTION}; found {len(ruleset_lines)}"
    )
    assert f'"{expected}"' in ruleset_lines[0], (
        f"the captured ruleset must name {expected!r}; got {ruleset_lines[0]!r}"
    )


# --------------------------------------------------------------------------- #
# Postgres -- the development major                                            #
# --------------------------------------------------------------------------- #


def test_every_postgres_service_image_reads_the_manifest() -> None:
    """No CI service container names a Postgres major of its own.

    The count is exact rather than a floor: a service added without a binding,
    or a walk that stopped finding them, fails here rather than passing over a
    shorter list.
    """
    images = _postgres_service_images()
    assert len(images) == EXPECTED_PG_SERVICE_COUNT, (
        f"expected {EXPECTED_PG_SERVICE_COUNT} pgvector service containers across "
        f"{CI_WORKFLOW.name} and {BUILD_IMAGES_WORKFLOW.name}; found {sorted(images)}"
    )
    literal = {site: img for site, img in images.items() if not _PGVECTOR_TAG.search(img)}
    assert not literal, f"unparseable pgvector image references: {literal}"
    unbound = {site: img for site, img in images.items() if "needs.versions.outputs." not in img}
    assert not unbound, (
        "every Postgres service image must read its major from the versions prelude "
        f"job rather than restate it; these do not: {unbound}"
    )


def test_non_floor_service_images_are_the_development_major() -> None:
    """Every service container but the deploy floor runs the development major."""
    images = _postgres_service_images()
    others = {site: img for site, img in images.items() if site[1] != DEPLOY_FLOOR_JOB}
    assert len(others) == EXPECTED_PG_SERVICE_COUNT - 1, (
        f"expected {EXPECTED_PG_SERVICE_COUNT - 1} non-floor Postgres services; "
        f"found {sorted(others)}"
    )
    wrong = {site: img for site, img in others.items() if img != _DEV_IMAGE}
    assert not wrong, (
        f"every non-floor service image must be exactly {_DEV_IMAGE!r}; a job that "
        f"should run the development major is bound to something else: {wrong}"
    )


def _prelude_jq_writes(prelude: dict[str, Any]) -> dict[str, str]:
    """Map each output key the prelude writes to the manifest path it reads.

    Parses the prelude's own ``run:`` block, which is the only place the binding
    between a declared output and a manifest key actually exists. Reading the
    ``outputs:`` block alone sees names on both sides of a binding that may not
    hold.
    """
    writes: dict[str, str] = {}
    for step in prelude.get("steps") or []:
        for key, path in re.findall(
            r"(\w+)=\$\(jq -er (\.[\w.]+) versions\.json\)", str((step or {}).get("run", ""))
        ):
            writes[key] = path
    return writes


def _resolve_manifest_path(path: str) -> Any:
    node: Any = declared_versions()
    for segment in path.lstrip(".").split("."):
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node


def test_versions_prelude_reads_manifest_keys_that_exist() -> None:
    """Every ``jq`` path a prelude reads resolves to a value in the manifest.

    This is the drift the prelude mechanism is actually exposed to: rename a
    manifest key, update ``tests/helpers/versions.py`` in the same change, and
    the four preludes keep reading the old path. ``jq -er`` then exits non-zero,
    ``set -e`` fails the prelude, and every job that needs it is *skipped* --
    which the forge counts as a satisfied required check. Nothing else in this
    suite reads those paths, so nothing else can catch it.
    """
    checked = 0
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        prelude = _workflow_jobs(path).get("versions")
        if prelude is None:
            continue
        writes = _prelude_jq_writes(prelude)
        assert writes, (
            f"{path.name}: the versions prelude declares outputs but no "
            f"`<key>=$(jq -er <path> versions.json)` write was found in its run block"
        )
        for key, manifest_path in writes.items():
            value = _resolve_manifest_path(manifest_path)
            assert isinstance(value, str) and value.strip(), (
                f"{path.name}: the versions prelude reads {manifest_path} for output "
                f"{key!r}, which does not resolve to a non-empty string in "
                f"{VERSIONS_MANIFEST.name}"
            )
            checked += 1
    assert checked, f"no versions prelude jq reads found under {WORKFLOWS_DIR}"


def test_versions_prelude_writes_exactly_the_outputs_it_declares() -> None:
    """Each declared output is written, and each written key is declared.

    A declared-but-unwritten output resolves to the empty string at run time --
    an image tag of ``pgvector/pgvector:pg`` -- rather than failing the workflow
    parse, so the consuming job runs against nothing. A written-but-undeclared
    key is dead work that reads as a binding.
    """
    checked = 0
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        prelude = _workflow_jobs(path).get("versions")
        if prelude is None:
            continue
        declared = set((prelude.get("outputs") or {}).keys())
        written = set(_prelude_jq_writes(prelude))
        assert declared == written, (
            f"{path.name}: the versions prelude declares outputs {sorted(declared)} but "
            f"writes {sorted(written)}; an output declared and not written resolves to "
            f"the empty string in every job that reads it"
        )
        checked += 1
    assert checked, f"no versions prelude found under {WORKFLOWS_DIR}"


def test_versions_prelude_outputs_every_consumed_key() -> None:
    """Each workflow's prelude job declares the outputs its jobs consume.

    A ``needs.versions.outputs.<key>`` reference to an output the prelude does
    not declare resolves to the empty string at run time -- an image tag of
    ``pgvector/pgvector:pg`` -- rather than failing the workflow parse. Nothing
    else in this suite would notice.
    """
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        jobs = _workflow_jobs(path)
        text = path.read_text(encoding="utf-8")
        consumed = set(re.findall(r"needs\.versions\.outputs\.([A-Za-z0-9_]+)", text))
        if not consumed:
            continue
        prelude = jobs.get("versions")
        assert prelude is not None, f"{path.name} consumes versions outputs but has no prelude job"
        declared = set((prelude.get("outputs") or {}).keys())
        assert consumed <= declared, (
            f"{path.name}: jobs consume {sorted(consumed - declared)} but the versions "
            f"prelude declares only {sorted(declared)}"
        )
        for job_id, job in jobs.items():
            if job_id == "versions":
                continue
            job_text = yaml.dump(job)
            if "needs.versions.outputs." not in job_text:
                continue
            needs = job.get("needs")
            needs = [needs] if isinstance(needs, str) else (needs or [])
            assert "versions" in needs, (
                f"{path.name}: job {job_id} reads versions outputs without `needs: versions`"
            )


def test_runbooks_name_the_development_major() -> None:
    """The workstation runbooks install the declared development major.

    These are the sites a person follows by hand, so a stale major here puts
    the workstation on a version nothing else runs.
    """
    expected = postgres_dev_major()
    for runbook in POSTGRES_RUNBOOKS:
        majors = set(re.findall(r"postgresql@(\d+)", runbook.read_text(encoding="utf-8")))
        assert majors, f"no `postgresql@<major>` reference found in {runbook}"
        assert majors == {expected}, (
            f"{runbook.name} names Postgres major(s) {sorted(majors)}; the declared "
            f"development major is {expected}"
        )


# --------------------------------------------------------------------------- #
# Python                                                                       #
# --------------------------------------------------------------------------- #


def test_workflow_python_pins_read_the_manifest() -> None:
    """No workflow names a Python version of its own."""
    pins = _python_version_pins()
    assert pins, f"no python-version pins found under {WORKFLOWS_DIR}"
    unbound = [p for p in pins if "needs.versions.outputs.python" not in p[2]]
    assert not unbound, (
        "every workflow python-version pin must read the versions prelude job's "
        f"output rather than restate it; these do not: {unbound}"
    )


def test_pyproject_python_floor_matches_the_manifest() -> None:
    """``requires-python`` names the declared version as its floor."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    requires = config["project"]["requires-python"]
    assert requires.startswith(">="), f"expected a `>=` floor for requires-python; got {requires!r}"
    assert requires.removeprefix(">=").strip() == python_version(), (
        f"requires-python is {requires!r}; versions.json declares {python_version()}"
    )


def test_ruff_target_version_matches_the_manifest() -> None:
    """Ruff lints against the language level the project actually requires.

    A target below the required version is not a cosmetic mismatch: it silences
    the rules that would flag code the older level cannot run, and permits
    rewrites down to it.
    """
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    target = config["tool"]["ruff"]["target-version"]
    assert target == ruff_target_version(python_version()), (
        f"[tool.ruff] target-version is {target!r}; versions.json declares "
        f"Python {python_version()}, which is {ruff_target_version(python_version())!r}"
    )


def test_ruff_per_file_target_carve_outs_are_declared() -> None:
    """Only the deploy probes may lint below the declared Python, and they must.

    A per-file target is how a file that runs on an interpreter this manifest
    does not govern avoids being rewritten into syntax that interpreter cannot
    parse. It is also, unchecked, the way to silently exempt anything at all
    from the Python parity claim -- so the carve-out set is pinned rather than
    merely permitted, and a new entry has to be added here deliberately.

    The deploy probes qualify because ``cloud-preflight.sh`` invokes them as a
    bare ``python3`` and the SharePoint validation workflow runs them in a job
    that provisions no interpreter.
    """
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    carve_outs = config["tool"]["ruff"].get("per-file-target-version") or {}
    assert set(carve_outs) == set(EXPECTED_RUFF_CARVE_OUTS), (
        f"per-file Ruff targets are pinned to {sorted(EXPECTED_RUFF_CARVE_OUTS)}; found "
        f"{sorted(carve_outs)}. A carve-out exempts its files from the declared Python, so "
        f"each one is a deliberate entry here with its reason in pyproject.toml."
    )
    for pattern, target in carve_outs.items():
        assert target == EXPECTED_RUFF_CARVE_OUTS[pattern], (
            f"carve-out {pattern!r} targets {target!r}; expected "
            f"{EXPECTED_RUFF_CARVE_OUTS[pattern]!r}"
        )
        assert parse_ruff_target(target) <= parse_ruff_target(
            ruff_target_version(python_version())
        ), (
            f"carve-out {pattern!r} targets {target!r}, which is above the declared "
            f"Python {python_version()}; a carve-out is a floor, never a raise"
        )


def test_python_base_images_match_the_manifest() -> None:
    """Both container images build on the declared Python.

    A ``FROM`` line cannot read a file, and the readable tag has to stay on it
    ahead of the digest for Dependabot's docker ecosystem to parse it, so these
    two sites restate the version and are held here.
    """
    expected = python_version()
    for dockerfile in (DOCKERFILE, DOCKERFILE_BFF):
        text = dockerfile.read_text(encoding="utf-8")
        found = set(re.findall(r"^FROM python:([\d.]+)", text, re.MULTILINE))
        assert found, f"no `FROM python:<version>` stage found in {dockerfile}"
        assert found == {expected}, (
            f"{dockerfile.name} builds on Python {sorted(found)}; versions.json declares {expected}"
        )


def test_prose_names_the_declared_python_version() -> None:
    """The setup prose a contributor reads names the declared version."""
    expected = python_version()
    for doc in PYTHON_PROSE:
        found = set(re.findall(r"Python (\d+\.\d+)", doc.read_text(encoding="utf-8")))
        assert found, f"no `Python <major>.<minor>` reference found in {doc}"
        assert found == {expected}, (
            f"{doc.name} names Python {sorted(found)}; versions.json declares {expected}"
        )


# --------------------------------------------------------------------------- #
# Node                                                                         #
# --------------------------------------------------------------------------- #


def test_workflow_node_pins_read_the_manifest() -> None:
    """No workflow names a Node major of its own.

    The remaining Node sites -- the ``Dockerfile.bff`` base, the ``@types/node``
    floor, the Dependabot note -- are held to the same declaration by
    ``tests/infra/test_frontend_node_version.py``.
    """
    pins = _version_pins("actions/setup-node", "node-version")
    assert pins, f"no actions/setup-node node-version pins found under {WORKFLOWS_DIR}"
    unbound = [p for p in pins if "needs.versions.outputs.node" not in p[2]]
    assert not unbound, (
        "every workflow node-version pin must read the versions prelude job's output "
        f"rather than restate it; these do not: {unbound}"
    )


# --------------------------------------------------------------------------- #
# Anti-coincidental controls                                                   #
#                                                                             #
# Each proves one detector above fires on the regression it targets, so a      #
# green check means the site was found and compared rather than missed.        #
# --------------------------------------------------------------------------- #


def test_control_prelude_jq_parser_detects_a_bad_path_and_a_missing_write() -> None:
    """Both prelude regressions are visible to the parser that must catch them.

    Arm A is a jq path the manifest does not carry; arm B is a declared output
    with no write. Each passed the previous gate, which compared output names on
    both sides of a binding it never read.
    """
    bad_path = yaml.safe_load(
        """
        outputs:
          python: ${{ steps.declared.outputs.python }}
        steps:
          - id: declared
            run: |
              echo "python=$(jq -er .python.versio versions.json)" >> "$GITHUB_OUTPUT"
        """
    )
    writes = _prelude_jq_writes(bad_path)
    assert writes == {"python": ".python.versio"}, writes
    assert _resolve_manifest_path(".python.versio") is None, (
        "the resolver must report a manifest path that does not exist"
    )
    assert _resolve_manifest_path(".python.version") == python_version()

    missing_write = yaml.safe_load(
        """
        outputs:
          python: ${{ steps.declared.outputs.python }}
          node: ${{ steps.declared.outputs.node }}
        steps:
          - id: declared
            run: |
              echo "python=$(jq -er .python.version versions.json)" >> "$GITHUB_OUTPUT"
        """
    )
    declared = set((missing_write.get("outputs") or {}).keys())
    written = set(_prelude_jq_writes(missing_write))
    assert declared - written == {"node"}, (
        "the declared-vs-written comparison must surface the unwritten output"
    )


def test_control_comment_stripping_hides_a_commented_out_load() -> None:
    """A ``loadJsonContent`` in a comment does not satisfy the Bicep check.

    Without the strip, the module's own explanatory comment above the parameter
    would satisfy the search and the check would pass whatever the parameter
    actually said.
    """
    commented = (
        "// param postgresVersion string = loadJsonContent('../../versions.json')\n"
        "param postgresVersion string = '16'\n"
    )
    stripped = _strip_bicep_comments(commented)
    assert "loadJsonContent" not in stripped
    assert re.search(r"=\s*'\d+'", stripped), "the literal default must survive the strip"


def test_control_service_image_walk_detects_a_literal_tag() -> None:
    """The service-image walk sees a hand-written tag, not just a bound one."""
    workflow = yaml.safe_load(
        """
        jobs:
          probe:
            services:
              postgres:
                image: pgvector/pgvector:pg99
        """
    )
    job = workflow["jobs"]["probe"]
    image = job["services"]["postgres"]["image"]
    assert "pgvector/pgvector" in image
    assert "needs.versions.outputs." not in image, (
        "the unbound-image predicate must reject a literal tag"
    )
    assert _PGVECTOR_TAG.search(image).group("tag") == "99"


def test_control_version_pin_walk_finds_pins_by_action() -> None:
    """The pin walk keys on the action, not on the input name alone.

    Python pins in this repository ride ``setup-uv`` and ``ruff-action`` rather
    than ``setup-python``; a walk that looked only for the latter would find
    nothing here and pass over an empty list.
    """
    by_action: dict[str, int] = {}
    for prefix in ("astral-sh/setup-uv", "astral-sh/ruff-action", "actions/setup-python"):
        by_action[prefix] = len(_version_pins(prefix, "python-version"))
    assert sum(by_action.values()) == len(_python_version_pins())
    assert by_action["astral-sh/setup-uv"] > 0, (
        f"expected setup-uv to carry Python pins; walk found {by_action}"
    )


def test_control_ruff_target_deriver_rejects_a_malformed_version() -> None:
    """A manifest value that is not ``major.minor`` fails loudly.

    Without this, ``'3'`` or ``'3.14.1'`` would produce a plausible-looking
    token and the Ruff check would compare two wrong things.
    """
    assert ruff_target_version("3.14") == "py314"
    for bad in ("3", "3.14.1", "py314", "", "3.x"):
        with pytest.raises(ValueError):
            ruff_target_version(bad)


def test_control_ruff_target_token_parses_major_and_minor_separately() -> None:
    """A Ruff target token is not a dotted version and must not be read as one.

    ``py312`` carries no separator, so parsing the digits as a single integer
    yields 312 and compares it against a major of 3 -- which is what the first
    draft of the carve-out check did, and what made it red against a correct
    configuration. Ordering is the property the check needs, so ordering is what
    is asserted.
    """
    assert parse_ruff_target("py312") == (3, 12)
    assert parse_ruff_target("py314") == (3, 14)
    assert parse_ruff_target("py312") < parse_ruff_target("py314")
    assert parse_ruff_target(ruff_target_version("3.14")) == (3, 14)
    for bad in ("312", "py", "py3", "python314", "py3.14", ""):
        with pytest.raises(ValueError):
            parse_ruff_target(bad)


def test_control_major_parser_rejects_an_unresolvable_spec() -> None:
    """A spec with no leading integer raises rather than defaulting."""
    assert major_of("17") == 17
    assert major_of(">=3.14") == 3
    assert major_of("^24.13.2") == 24
    with pytest.raises(ValueError):
        major_of("latest")


def test_control_required_context_matchers_select_only_their_own_site() -> None:
    """Each branch-protection matcher picks its own line and ignores the others.

    The two structured sites and any prose mentioning the context all carry the
    same string. A matcher keyed on that string alone would match all three,
    report the wrong count, and then compare a declared major against a
    sentence. Driven against a synthetic document so the control tests the
    matchers rather than today's contents of the real one.
    """
    document = "\n".join(
        (
            "The deploy-floor job exists because storage tests on the deploy floor (pg16)",
            "runs against the deployed major rather than the development one.",
            "| `storage tests on the deploy floor (pg16)` | scoped storage tests. |",
            '          { "context": "storage tests on the deploy floor (pg16)", "x": 1 }',
        )
    )
    lines = document.splitlines()
    rows = [ln for ln in lines if ln.startswith("| `storage tests")]
    contexts = [ln for ln in lines if '"context": "storage tests' in ln]
    assert len(rows) == 1, f"the table matcher must select one line; got {rows}"
    assert len(contexts) == 1, f"the ruleset matcher must select one line; got {contexts}"
    assert rows[0] != contexts[0], "the two matchers must select different lines"
    assert "The deploy-floor job exists" not in rows[0] + contexts[0], (
        "neither matcher may select the prose mention"
    )

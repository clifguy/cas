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
    postgres_client_major,
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

# Full suite, browser integration, and image verification each use Postgres.
EXPECTED_PG_SERVICE_COUNT: Final[int] = 3

# Files permitted to lint and format below the declared Python, and the target
# each uses. The deploy probes run on the runner's system interpreter rather
# than a provisioned one; see the reason recorded in `pyproject.toml`. Pinned
# rather than open so a carve-out cannot quietly exempt anything else.
EXPECTED_RUFF_CARVE_OUTS: Final[dict[str, str]] = {"deploy/*.py": "py312"}

# The scripts a workflow or shell script runs as a bare `python3`, in a job that
# provisions no interpreter. These are the reason a carve-out exists, so the
# carve-out is asserted to cover them by path rather than only to be declared:
# moving one out from under the glob would otherwise restore the hazard behind a
# gate still reporting green.
SYSTEM_INTERPRETER_SCRIPTS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "deploy" / "mcp_preflight_probe.py",
    REPO_ROOT / "deploy" / "sharepoint_validate.py",
)

_PGVECTOR_TAG = re.compile(r"pgvector/pgvector:pg(?P<tag>[^\s\"']+)")

# The exact image reference each Postgres service must carry. Compared by
# equality rather than containment: containment constrains only that the
# expression appears somewhere in the string, which an image like
# ``pgvector/pgvector:pg16-${{ ... }}`` satisfies while pinning the wrong tag.
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


def _emitted_module_overrides(template: dict[str, Any]) -> set[str] | None:
    """Parameter names the parent supplies to the module declaring ``postgresVersion``.

    Returns ``None`` when no nested template declares the parameter -- the module
    was not located, which is a different fact from "located and overriding
    nothing". Conflating the two lets an empty template, or a ``resources``
    block emitted as a dict rather than a list, report every override as absent
    while one is live.

    ``resources`` is a list in the emitted ARM today and a dict under symbolic-name
    codegen, so both are walked.
    """
    resources = template.get("resources")
    if isinstance(resources, dict):
        candidates = list(resources.values())
    elif isinstance(resources, list):
        candidates = resources
    else:
        return None
    for resource in candidates:
        if not isinstance(resource, dict):
            continue
        properties = resource.get("properties") or {}
        nested = (properties.get("template") or {}).get("parameters")
        if isinstance(nested, dict) and "postgresVersion" in nested:
            return set((properties.get("parameters") or {}).keys())
    return None


def _emitted_postgres_version_default(node: Any) -> tuple[str | None, dict[str, Any]]:
    """Find the compiled ``postgresVersion`` default and its template's variables.

    The Postgres module is emitted as a nested template inside ``main.json``, so
    the parameter and the generated variable holding the loaded manifest live in
    the same nested scope; both are returned together because the assertion
    needs to resolve one against the other.

    A parent-side override is emitted as a sibling ``parameters`` block whose
    entry carries ``value`` rather than ``defaultValue``. Descending into that
    block and stopping there would report the default as missing and blame the
    module for not reaching the ARM, so a block with no ``defaultValue`` is
    walked past rather than treated as the answer.
    """
    if isinstance(node, dict):
        parameters = node.get("parameters")
        if isinstance(parameters, dict) and "postgresVersion" in parameters:
            default = (parameters["postgresVersion"] or {}).get("defaultValue")
            if isinstance(default, str):
                return default, node.get("variables") or {}
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

    # Checked first: an override makes every statement below about the default
    # irrelevant, and diagnosing it as "the module is missing" sends a reader
    # somewhere else entirely.
    overrides = _emitted_module_overrides(template)
    assert overrides is not None, (
        "no nested template declaring postgresVersion found in the compiled ARM; the "
        "Postgres module did not reach it"
    )
    assert "postgresVersion" in overrides
    parent = next(
        resource
        for resource in template["resources"]
        if "postgresVersion" in resource.get("properties", {}).get("parameters", {})
    )
    assert parent["properties"]["parameters"]["postgresVersion"] == {
        "value": "[variables('postgresMajor')]"
    }
    expression = template["variables"]["postgresMajor"]
    selection = re.fullmatch(
        r"\[if\(empty\(parameters\('postgresGeneration'\)\), "
        r"variables\('([^']+)'\)\.postgres\.deploy_major, "
        r"variables\('([^']+)'\)\.postgres\.migration\.target_major\)\]",
        expression,
    )
    assert selection, "serving major must select the fixed migration target by generation"
    assert (
        template["variables"][selection[1]]["postgres"]["deploy_major"] == postgres_deploy_major()
    )
    assert template["variables"][selection[2]]["postgres"]["migration"]["target_major"] == "17"

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


def test_container_pg_client_covers_both_majors() -> None:
    """The shipped client covers the source and replacement during migration.

    A client older than either server cannot dump it; newer clients support
    logical upgrade from the older source.
    """
    major = postgres_client_major()
    assert f"postgresql-client-{major}" in _runtime_stage_text(), (
        f"the runtime stage must install postgresql-client-{major} to match the "
        f"declared Flexible Server major (versions.json postgres.deploy_major)"
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


def test_service_images_are_the_development_major() -> None:
    """Every service container runs the development major."""
    images = _postgres_service_images()
    others = images
    assert len(others) == EXPECTED_PG_SERVICE_COUNT, (
        f"expected {EXPECTED_PG_SERVICE_COUNT} Postgres services; found {sorted(others)}"
    )
    wrong = {site: img for site, img in others.items() if img != _DEV_IMAGE}
    assert not wrong, (
        f"every service image must be exactly {_DEV_IMAGE!r}; a job that "
        f"should run the development major is bound to something else: {wrong}"
    )


# A prelude reads each value into a shell variable and echoes the variables into
# `$GITHUB_OUTPUT`. The two halves are matched separately: the read is what binds
# a name to a manifest path, the echo is what turns that name into a job output,
# and a prelude can get either half wrong on its own.
# Which manifest path each prelude output must carry. The join below proves an
# output is backed by *a* manifest read; without this it does not prove which,
# so a prelude reading `.node.major` into an output named `python` resolves to a
# non-empty string and passes every other check here.
EXPECTED_PRELUDE_BINDINGS: Final[dict[str, str]] = {
    "postgres_deploy_major": ".postgres.deploy_major",
    "postgres_dev_major": ".postgres.dev_major",
    "python": ".python.version",
    "node": ".node.major",
}

_PRELUDE_READ = re.compile(r'(\w+)="\$\(jq -er (\.[\w.]+) versions\.json\)"')
_PRELUDE_ECHO = re.compile(r'echo "(\w+)=\$(\w+)"')


def _prelude_jq_reads(prelude: dict[str, Any]) -> dict[str, str]:
    """Map each shell variable the prelude assigns to the manifest path it reads.

    Parses the prelude's own ``run:`` block, which is the only place the binding
    between a name and a manifest key actually exists. Reading the ``outputs:``
    block alone sees names on both sides of a binding that may not hold.
    """
    reads: dict[str, str] = {}
    for step in prelude.get("steps") or []:
        for key, path in _PRELUDE_READ.findall(str((step or {}).get("run", ""))):
            reads[key] = path
    return reads


def _prelude_output_writes(prelude: dict[str, Any]) -> dict[str, str]:
    """Map each key echoed into ``$GITHUB_OUTPUT`` to the variable it echoes.

    Only steps that actually redirect to ``$GITHUB_OUTPUT`` count: an echo that
    goes nowhere produces no output, and reads identically in the source.
    """
    writes: dict[str, str] = {}
    for step in prelude.get("steps") or []:
        run = str((step or {}).get("run", ""))
        if "GITHUB_OUTPUT" not in run:
            continue
        for key, variable in _PRELUDE_ECHO.findall(run):
            writes[key] = variable
    return writes


def _prelude_jq_writes(prelude: dict[str, Any]) -> dict[str, str]:
    """Map each emitted output key to the manifest path standing behind it.

    Joins the two halves: a key is bound to a path only when it is echoed into
    ``$GITHUB_OUTPUT`` *and* the variable it echoes was assigned from that path.
    A key whose variable was never read resolves to the empty string at run time
    and is deliberately absent here rather than reported with a path it does not
    have.
    """
    reads = _prelude_jq_reads(prelude)
    return {
        key: reads[variable]
        for key, variable in _prelude_output_writes(prelude).items()
        if variable in reads
    }


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
    the four preludes keep reading the old path.

    The prelude reads each value into a shell variable, so ``set -e`` fires on a
    path that does not resolve and the ``versions`` job fails. Because
    ``versions`` is itself a required status check, that failure blocks the
    merge rather than being laundered: a job skipped for a failed dependency
    counts as a satisfied required check, which is what the ruleset entry
    prevents.

    Worth knowing why the form matters, since it changed once already. Written
    as ``echo "k=$(jq -er <path> versions.json)"``, ``set -e`` reads the exit
    status of ``echo`` rather than of the substitution inside its argument, so
    ``jq`` fails and the step writes ``k=null`` and succeeds. That was the safer
    shape while ``versions`` was not required -- a garbage tag and a loud red
    beat a silent skip -- and it is the wrong shape now. See
    ``docs/process/branch_protection.md``.

    Either way the value never reaches a job, and nothing else in this suite
    reads those paths -- so this check is what catches the typo before any run.
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
            assert key in EXPECTED_PRELUDE_BINDINGS, (
                f"{path.name}: the versions prelude emits {key!r}, which is not a declared "
                f"binding ({sorted(EXPECTED_PRELUDE_BINDINGS)})"
            )
            assert manifest_path == EXPECTED_PRELUDE_BINDINGS[key], (
                f"{path.name}: output {key!r} is read from {manifest_path}, not from "
                f"{EXPECTED_PRELUDE_BINDINGS[key]}; the value would resolve and every other "
                f"check here would pass while the output carried the wrong version"
            )
            value = _resolve_manifest_path(manifest_path)
            assert isinstance(value, str) and value.strip(), (
                f"{path.name}: the versions prelude reads {manifest_path} for output "
                f"{key!r}, which does not resolve to a non-empty string in "
                f"{VERSIONS_MANIFEST.name}"
            )
            checked += 1
    assert checked, f"no versions prelude jq reads found under {WORKFLOWS_DIR}"


def test_versions_prelude_outputs_resolve_to_the_step_that_writes_them() -> None:
    """Each output names a step that exists and actually writes its key.

    An output is ``${{ steps.<id>.outputs.<key> }}``, and the write is a line in
    some step's ``run:``. Checking that the key sets match leaves three ways for
    the two halves to name different things: a write in a step the output does
    not name, a writing step with no ``id:`` at all, and a write never redirected
    to ``$GITHUB_OUTPUT``. Each resolves the output to the empty string at run
    time -- through the one link the key-set comparison does not cross.
    """
    checked = 0
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        prelude = _workflow_jobs(path).get("versions")
        if prelude is None:
            continue
        steps = {
            str(step.get("id")): str(step.get("run", ""))
            for step in (prelude.get("steps") or [])
            if isinstance(step, dict) and step.get("id")
        }
        for key, expression in (prelude.get("outputs") or {}).items():
            match = re.search(r"steps\.(\w+)\.outputs\.(\w+)", str(expression))
            assert match is not None, (
                f"{path.name}: output {key!r} is {expression!r}, which names no step output"
            )
            step_id, written_key = match.group(1), match.group(2)
            assert step_id in steps, (
                f"{path.name}: output {key!r} reads step {step_id!r}, which has no `id:` "
                f"among the prelude's steps ({sorted(steps)})"
            )
            assert written_key == key, (
                f"{path.name}: output {key!r} reads a differently named step output {written_key!r}"
            )
            run = steps[step_id]
            assert re.search(rf'echo "{re.escape(written_key)}=\$\w+"', run), (
                f"{path.name}: step {step_id!r} does not echo {written_key!r}"
            )
            assert "GITHUB_OUTPUT" in run, (
                f"{path.name}: step {step_id!r} never redirects to $GITHUB_OUTPUT, so nothing "
                f"it echoes becomes an output"
            )
            checked += 1
    assert checked, f"no versions prelude outputs found under {WORKFLOWS_DIR}"


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
        echoed = _prelude_output_writes(prelude)
        reads = _prelude_jq_reads(prelude)
        unbacked = {k: v for k, v in echoed.items() if v not in reads}
        assert not unbacked, (
            f"{path.name}: the prelude echoes {sorted(unbacked)} from variables it never "
            f"assigns from the manifest ({sorted(unbacked.values())}); each resolves to the "
            f"empty string at run time"
        )
        written = set(echoed)
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
    covered = {path for pattern in carve_outs for path in REPO_ROOT.glob(pattern)}
    for probe in SYSTEM_INTERPRETER_SCRIPTS:
        assert probe in covered, (
            f"{probe.relative_to(REPO_ROOT)} is invoked as a bare `python3` in a job that "
            f"provisions no interpreter, so it must fall under a per-file carve-out; the "
            f"declared globs cover {sorted(p.name for p in covered)}"
        )
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
        matched = sorted(REPO_ROOT.glob(pattern))
        assert matched, (
            f"carve-out {pattern!r} matches no file. Ruff does not validate a per-file glob, "
            f"so a carve-out whose files moved away reports as held while the files it was "
            f"written for are formatted at the declared target again."
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


def test_control_prelude_parser_detects_each_way_the_binding_can_break() -> None:
    """Every way the read/echo pair can come apart is visible to the parser.

    The prelude binds a manifest path to a job output in two steps -- assign a
    shell variable from ``jq``, then echo it into ``$GITHUB_OUTPUT`` -- and each
    half can be wrong without the other. Arm A is a path the manifest does not
    carry; arm B is a declared output that is never echoed; arm C is an echo
    whose variable was never assigned, which the single-map parser could not
    express at all because it read the two halves as one.
    """
    bad_path = yaml.safe_load(
        """
        outputs:
          python: ${{ steps.declared.outputs.python }}
        steps:
          - id: declared
            run: |
              python="$(jq -er .python.versio versions.json)"
              echo "python=$python" >> "$GITHUB_OUTPUT"
        """
    )
    assert _prelude_jq_writes(bad_path) == {"python": ".python.versio"}
    assert _resolve_manifest_path(".python.versio") is None, (
        "the resolver must report a manifest path that does not exist"
    )
    assert _resolve_manifest_path(".python.version") == python_version()

    missing_echo = yaml.safe_load(
        """
        outputs:
          python: ${{ steps.declared.outputs.python }}
          node: ${{ steps.declared.outputs.node }}
        steps:
          - id: declared
            run: |
              python="$(jq -er .python.version versions.json)"
              echo "python=$python" >> "$GITHUB_OUTPUT"
        """
    )
    declared = set((missing_echo.get("outputs") or {}).keys())
    assert declared - set(_prelude_output_writes(missing_echo)) == {"node"}, (
        "the declared-vs-echoed comparison must surface the output that is never written"
    )

    unread_variable = yaml.safe_load(
        """
        outputs:
          python: ${{ steps.declared.outputs.python }}
        steps:
          - id: declared
            run: |
              echo "python=$python" >> "$GITHUB_OUTPUT"
        """
    )
    echoed = _prelude_output_writes(unread_variable)
    reads = _prelude_jq_reads(unread_variable)
    assert echoed == {"python": "python"}, echoed
    assert not reads, "nothing is assigned from the manifest in this arm"
    assert {k: v for k, v in echoed.items() if v not in reads} == {"python": "python"}, (
        "an echo of a variable that was never assigned must be reported, not silently "
        "resolved to a path it does not have"
    )

    swapped = yaml.safe_load(
        """
        outputs:
          python: ${{ steps.declared.outputs.python }}
        steps:
          - id: declared
            run: |
              python="$(jq -er .node.major versions.json)"
              echo "python=$python" >> "$GITHUB_OUTPUT"
        """
    )
    swapped_writes = _prelude_jq_writes(swapped)
    assert swapped_writes == {"python": ".node.major"}, swapped_writes
    assert _resolve_manifest_path(".node.major"), (
        "the swapped path resolves, which is why resolvability alone cannot catch this"
    )
    assert swapped_writes["python"] != EXPECTED_PRELUDE_BINDINGS["python"], (
        "the declared-binding comparison is what separates a readable path from the right one"
    )

    unredirected = yaml.safe_load(
        """
        steps:
          - id: declared
            run: |
              python="$(jq -er .python.version versions.json)"
              echo "python=$python"
        """
    )
    assert _prelude_jq_reads(unredirected) == {"python": ".python.version"}
    assert not _prelude_output_writes(unredirected), (
        "an echo that never reaches $GITHUB_OUTPUT produces no output and must not count"
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

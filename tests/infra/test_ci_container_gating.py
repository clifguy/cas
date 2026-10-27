"""Structural gate for when CI builds the container images.

The CI workflow's ``container`` job builds both images, scans them and runs the
container smoke tests. It is the slowest job a pull request can run, and most
pull requests change nothing that could break an image the ordinary test job
would not also catch. So on a pull request it runs only when the image recipe
or the embedding trial's surface changes; on every other event -- above all the
default branch's push -- it runs unconditionally. A code change that breaks the
image is therefore still caught before any deploy, which builds only from a
commit whose default-branch run is green.

The recipe filter is checked against the Dockerfiles themselves: every single
file they copy must trip it, so adding a ``COPY`` of a new file cannot silently
take that file out of pull-request coverage. The directories copied whole
(application source) are the deliberate exception and are named below.

The layer cache is written by one scheduled caller rather than by every push,
and this module also holds that caller to the default branch, since a cache
written from another ref warms a scope no other run reads.

These checks read the tracked workflow YAML and Dockerfiles only, so they run in
the ordinary Python test job.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR: Final[Path] = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW: Final[Path] = WORKFLOWS_DIR / "ci.yml"
CACHE_WORKFLOW: Final[Path] = WORKFLOWS_DIR / "image-cache.yml"
DOCKERFILES: Final[tuple[Path, ...]] = (REPO_ROOT / "Dockerfile", REPO_ROOT / "Dockerfile.bff")
BUILD_CALLEE: Final[str] = "./.github/workflows/build-images.yml"

# Directories the Dockerfiles copy whole. Application source changes on almost
# every pull request; building for each of them is the cost this gating removes,
# and the default-branch push covers them before any deploy.
_CODE_DIRECTORIES: Final[frozenset[str]] = frozenset({"sage/", "app/", "app/backend/"})

# Recipe files that are not COPY sources but decide what the image contains.
_RECIPE_FILES: Final[tuple[str, ...]] = (
    "Dockerfile",
    "Dockerfile.bff",
    ".dockerignore",
    "Dockerfile.bff.dockerignore",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/build-images.yml",
)

_TRIAL_SURFACE: Final[tuple[str, ...]] = (
    "scripts/embedding_contention_trial.py",
    "sage/adapters/embedding_nomic.py",
    "Dockerfile",
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _on_block(workflow: dict) -> dict:
    """The trigger mapping; PyYAML reads a bare ``on:`` key as ``True``."""
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def _normalise(raw: object) -> str:
    return " ".join(str(raw).replace("${{", " ").replace("}}", " ").split())


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a paths-filter glob: ``**`` spans directories, ``*`` does not."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out))


def _filters() -> dict[str, list[str]]:
    jobs = _load(CI_WORKFLOW).get("jobs") or {}
    steps = (jobs.get("paths-filter") or {}).get("steps") or []
    for step in steps:
        if "dorny/paths-filter" in str(step.get("uses") or ""):
            return yaml.safe_load(step["with"]["filters"]) or {}
    raise AssertionError("the paths-filter job has no dorny/paths-filter step")


def _covered(path: str, globs: list[str]) -> bool:
    return any(_glob_to_regex(glob).fullmatch(path) for glob in globs)


def _copy_sources(dockerfile: Path) -> list[str]:
    """The build-context sources of every ``COPY`` or ``ADD`` that is not ``--from``.

    Continuation lines are joined first, so a multi-line instruction is read
    whole. The JSON (exec) form is refused rather than half-parsed: a source the
    parser cannot read is a source the filter coverage check cannot see.
    """
    text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    sources: list[str] = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or tokens[0].upper() not in ("COPY", "ADD"):
            continue
        flags = [token for token in tokens[1:] if token.startswith("--")]
        args = [token for token in tokens[1:] if not token.startswith("--")]
        if any(flag.startswith("--from") for flag in flags):
            continue
        assert args and not args[0].startswith("["), (
            f"{dockerfile.name}: cannot read the sources of {line.strip()!r}"
        )
        sources.extend(args[:-1])
    return sources


def _container_job() -> dict:
    return (_load(CI_WORKFLOW).get("jobs") or {}).get("container") or {}


# ---------------------------------------------------------------------------
# The pull-request gate on the container job
# ---------------------------------------------------------------------------


def test_container_job_reads_the_paths_filter() -> None:
    needs = _container_job().get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    assert "paths-filter" in needs, (
        "the container job's condition cannot read a filter it does not need"
    )


def test_container_job_runs_on_every_event_but_a_pull_request() -> None:
    """Only a pull request may skip the job, and only through the two filters."""
    condition = _normalise(_container_job().get("if") or "")
    assert condition, "the container job is unconditional; pull requests pay for every build"
    assert condition == (
        "github.event_name != 'pull_request'"
        " || needs.paths-filter.outputs.image == 'true'"
        " || needs.paths-filter.outputs.embedding == 'true'"
    ), (
        f"unexpected container job condition {condition!r}: it must admit every "
        "non-pull-request event, so the default-branch push always builds, and "
        "admit a pull request only when the image or embedding filter fires"
    )


def test_container_job_requests_the_trial_only_where_it_can_change() -> None:
    passed = _normalise((_container_job().get("with") or {}).get("embedding_trial"))
    assert passed == (
        "github.event_name != 'pull_request' || needs.paths-filter.outputs.embedding == 'true'"
    ), (
        f"the container job requests the embedding trial as {passed!r}; it must run "
        "on every non-pull-request event and on a pull request touching its surface"
    )


def test_ci_never_writes_the_layer_cache() -> None:
    assert not (_container_job().get("with") or {}).get("write_cache"), (
        "the CI caller writes the layer cache; every push would export fresh blobs "
        "into a fixed repository budget"
    )


# ---------------------------------------------------------------------------
# The filters
# ---------------------------------------------------------------------------


def test_filter_job_exports_the_image_and_embedding_outputs() -> None:
    outputs = (_load(CI_WORKFLOW).get("jobs") or {}).get("paths-filter", {}).get("outputs") or {}
    for name in ("image", "embedding"):
        assert _normalise(outputs.get(name)) == f"steps.f.outputs.{name}", (
            f"the paths-filter job does not export its `{name}` result"
        )


def test_every_copied_file_and_recipe_input_trips_the_image_filter() -> None:
    """A file the image contains, or is built from, reaches pull-request coverage."""
    globs = _filters().get("image") or []
    assert globs, "the paths-filter step declares no `image` filter"

    uncovered: list[str] = []
    seen_directories: set[str] = set()
    for dockerfile in DOCKERFILES:
        for source in _copy_sources(dockerfile):
            if source.endswith("/"):
                assert source in _CODE_DIRECTORIES, (
                    f"{dockerfile.name} copies the directory {source}, which is neither "
                    "covered by the image filter nor named as application source"
                )
                seen_directories.add(source)
                continue
            if not _covered(source, globs):
                uncovered.append(f"{dockerfile.name}: {source}")
    for recipe in _RECIPE_FILES:
        if not _covered(recipe, globs):
            uncovered.append(recipe)

    assert not uncovered, f"these image inputs never trip the image filter: {uncovered}"
    assert seen_directories == _CODE_DIRECTORIES, (
        f"the directory exemption names {sorted(_CODE_DIRECTORIES - seen_directories)}, "
        "which no Dockerfile copies any longer; drop it rather than leave a stale exemption"
    )


def test_application_source_trips_neither_filter() -> None:
    """The control: a code change outside the trial's surface leaves the job off."""
    filters = _filters()
    for path in ("sage/services/lifecycle.py", "app/src/main.tsx", "tests/sage/test_x.py"):
        for name in ("image", "embedding"):
            assert not _covered(path, filters.get(name) or []), f"{path} trips the {name} filter"


def test_embedding_filter_covers_the_trial_surface() -> None:
    globs = _filters().get("embedding") or []
    assert globs, "the paths-filter step declares no `embedding` filter"
    missing = [path for path in _TRIAL_SURFACE if not _covered(path, globs)]
    assert not missing, f"changes to {missing} would not run the embedding trial on a pull request"


# ---------------------------------------------------------------------------
# The scheduled cache writer
# ---------------------------------------------------------------------------


def _callers_of_build() -> dict[str, dict]:
    callers: dict[str, dict] = {}
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        for job_name, job in (_load(path).get("jobs") or {}).items():
            if isinstance(job, dict) and job.get("uses") == BUILD_CALLEE:
                callers[f"{path.name}:{job_name}"] = job
    return callers


def test_cache_writer_is_scheduled_and_bound_to_the_default_branch() -> None:
    workflow = _load(CACHE_WORKFLOW)
    on = _on_block(workflow)
    assert set(on) == {"schedule", "workflow_dispatch"}, (
        f"the cache writer triggers on {sorted(on)}; it must run only on its schedule or by hand"
    )

    # The budget argument is one write a day: a single cron entry firing at a
    # fixed minute and hour on every day, never overlapping itself.
    schedule = on.get("schedule") or []
    assert len(schedule) == 1, f"the cache writer must have one schedule entry, got {schedule}"
    fields = str(schedule[0].get("cron") or "").split()
    assert len(fields) == 5 and all(f.isdigit() for f in fields[:2]), (
        f"the cache writer's cron {fields} must fire at a literal minute and hour"
    )
    assert fields[2:] == ["*", "*", "*"], (
        f"the cache writer's cron {fields} must fire once on every day"
    )
    concurrency = workflow.get("concurrency") or {}
    assert concurrency.get("group") and concurrency.get("cancel-in-progress") is False, (
        "the cache writer must serialise its runs without cancelling one mid-write"
    )

    jobs = workflow.get("jobs") or {}
    assert len(jobs) == 1, "the cache writer must hold exactly one job"
    job = next(iter(jobs.values()))
    assert job.get("uses") == BUILD_CALLEE, "the cache writer must call the shared build"
    assert _normalise(job.get("if") or "") == "github.ref == 'refs/heads/main'", (
        "the cache writer is not bound to the default branch; a dispatch from another "
        "ref writes a cache scope no other run reads"
    )
    args = job.get("with") or {}
    assert args.get("write_cache") is True, "the cache writer does not ask to write the cache"
    assert args.get("push") is False, "the cache writer must not push images"

    # The trial runs only on a dispatch that asks for it: a scheduled run
    # carries no inputs, so the comparison is false there.
    dispatch_inputs = (on.get("workflow_dispatch") or {}).get("inputs") or {}
    trial_input = dispatch_inputs.get("embedding_trial") or {}
    assert trial_input.get("type") == "boolean" and trial_input.get("default") is False, (
        "the cache writer's dispatch must offer an `embedding_trial` boolean defaulting to false"
    )
    assert _normalise(args.get("embedding_trial")) == "inputs.embedding_trial == true", (
        "the cache writer must request the trial exactly when a dispatch asks for it"
    )


def test_only_the_scheduled_caller_requests_a_cache_write() -> None:
    writers = sorted(
        name
        for name, job in _callers_of_build().items()
        if (job.get("with") or {}).get("write_cache")
    )
    assert writers == [f"{CACHE_WORKFLOW.name}:{next(iter(_load(CACHE_WORKFLOW)['jobs']))}"], (
        f"the callers requesting a cache write are {writers}; only the scheduled "
        "writer may, beside the deploy arm's push"
    )


# ---------------------------------------------------------------------------
# Controls for the helpers above
# ---------------------------------------------------------------------------


def test_glob_translation_control() -> None:
    assert _covered("docs/fs/sage/a.json", ["docs/fs/**"])
    assert _covered("Dockerfile.bff", ["Dockerfile*"])
    assert not _covered("deploy/x/Dockerfile", ["Dockerfile*"])
    assert not _covered("sage/x.py", ["sage/*.json"])


def test_copy_source_parser_control(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM x AS a\n"
        "COPY --from=uv /uv /bin/uv\n"
        "COPY --chown=a:a pyproject.toml uv.lock ./\n"
        "COPY sage/ ./sage/\n"
        "COPY deploy/a.yaml \\\n"
        "     deploy/b.yaml /opt/\n"
        "ADD docs/x.json /opt/x.json\n",
        encoding="utf-8",
    )
    assert _copy_sources(dockerfile) == [
        "pyproject.toml",
        "uv.lock",
        "sage/",
        "deploy/a.yaml",
        "deploy/b.yaml",
        "docs/x.json",
    ]


def test_copy_source_parser_refuses_the_exec_form(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text('COPY ["a.json", "/opt/a.json"]\n', encoding="utf-8")
    try:
        _copy_sources(dockerfile)
    except AssertionError:
        return
    raise AssertionError("the parser half-read an exec-form COPY instead of refusing it")

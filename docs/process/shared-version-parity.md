# Shared-component version parity — the mechanics

The rule this implements, the classification that decides what it applies to, and the
upgrade choreography are **not** stated here. They are the *CAS Version Parity Discipline*
steering document in the cas vault (`doc_type=steering_document`), which is authoritative:
where a component exists in more than one profile and its version is a free choice, hold it
identical across every environment and gate the identity; where it cannot be identical, make
the difference explicit and tested. That document also classifies which differences are
parity candidates and which are the profile switch working as designed, and it explains why
those differences are out of scope.

This file carries only what is specific to this repository: where the declaration lives, how
each consumer reaches it, and the one hazard that constrains how a value can be moved.

## The declaration

[`versions.json`](../../versions.json) at the repository root, holding the Postgres majors,
the Python version and the Node major.

JSON rather than the YAML this repository prefers for human-edited configuration: Bicep can
read JSON and text at compile time and has no YAML loader, and the manifest is read by four
toolchains, so the one inflexible consumer picks the format. Repo root rather than `infra/`
because the workstation must not read cloud IaC to learn what to install.

## Readers and gated restatements

The steering document's enforcement layer 1 holds that an *ungated* restatement is the
defect: a consumer that cannot read a file is not forbidden, it is required to be gated, and
a site in neither class is the drift the discipline exists to prevent. The tables below are
this repository's classification of every site into one class or the other.

| Reads the declaration | Mechanism |
|---|---|
| `infra/modules/postgres.bicep`, replacement templates and migration driver | `loadJsonContent` at compile time |
| `ci.yml`, `build-images.yml`, `dependabot-triage.yml`, `ruleset-drift.yml` | a `versions` prelude job's outputs |
| the test suite | `tests/helpers/versions.py` |

A prelude job rather than a step because a service container's `image` resolves before any
step in its own job runs, so it cannot read a value a sibling step computed — but it can read
`needs.<job>.outputs`. The `python-version` and `node-version` pins go through the same
outputs, so there is one mechanism rather than two.

| Restates it, and is gated | Why it cannot read |
|---|---|
| `Dockerfile`, `Dockerfile.bff` `FROM` lines | A `FROM` line takes no file input, and the readable tag must stay on it ahead of the digest for Dependabot's `docker` ecosystem to parse it. |
| `pyproject.toml` — `requires-python`, `[tool.ruff] target-version` | Static TOML; no interpolation. |
| the deploy-floor job's `name:` | It is a required check context — see below. |
| the Postgres runbooks, `README.md`, `CLAUDE.md`, the Dependabot `@types/node` note | Prose. |

`tests/infra/test_shared_version_parity.py` and `tests/infra/test_frontend_node_version.py`
are enforcement layer 2, holding every restating site to the declaration. Each check proves
it located its site before comparing, so a parser that silently matched nothing cannot pass.

Layer 3 is the deploy preflight's `postgres_major` check, which reads the live server's major
from the Azure control plane through `deploy/postgres-version-probe.sh` — a control-plane
read, because the server integrates privately into a delegated subnet and is not reachable
from a runner. Layer 4 is `tests/infra/test_workstation_postgres_major.py`, which fails under
CI, where the service container's tag is derived from the declaration so a mismatch means the
derivation did not take effect, and warns on a workstation, where the server is the
developer's own and the steering document permits the workstation to lag.

## The two Postgres majors are a transition, not a design

The manifest declares `postgres.deploy_major` and `postgres.dev_major` separately, and today
they differ. Read that as the steering document does: **parity is the resting state, and this
skew is a bounded transition that has not yet completed** — not two values with standing
jobs. The `storage-deploy-floor` CI job that tests the delta is the cost of the skew, not a
feature of it; the discipline's own conclusion is that testing a version delta is strictly
worse than not having one, because converging removes the class by construction.

Two names exist so that every consumer of each value is bound to a declaration while the
transition is open, rather than being bound to nothing. When the convergence lands, the two
collapse to one: set both to the same major, delete the deploy-floor job with its required
check context, and the second name goes away. `dev_major` may not fall below `deploy_major`,
and that is asserted.

## The required check context constrains how the floor moves

The deploy-floor job is named `storage tests on the deploy floor (pg16)`, and that name is a
required status check in the branch ruleset. It is **not** templated from the prelude job's
output: a context that changed whenever the declared major did would block every merge while
GitHub waited on a context that never arrives.

So the floor major appears in four places that must move together, and only three are in this
repository — the job's `name:` in `ci.yml`, the required-check table in
[`branch_protection.md`](branch_protection.md), the captured ruleset JSON in that same
document, and **the live GitHub ruleset**, which no test can reach. Changing
`postgres.deploy_major` is therefore not a repository-only change. See `branch_protection.md`,
which also records why the `versions` prelude job should itself become a required context.

During the offline cloud replacement, the container client reads the greater of
`deploy_major` and `dev_major` through its gate. This lets it dump the older
incumbent and the replacement. A nonempty, verified serving generation selects
the development major in the main template; the empty generation still selects
the deploy floor. Neither selection removes the floor job. See
[cloud recovery](postgres-cloud-recovery.md) for the staged cutover boundary.

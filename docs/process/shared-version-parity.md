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

## PostgreSQL serving baseline and retained migration

`postgres.deploy_major` and `postgres.dev_major` are both 17. The full PostgreSQL
17 test job supplies the storage coverage; there is no separate deploy-floor job.
Its former required context must be removed from the live ruleset in the same
landing window as the workflow and captured ruleset change. Preserve every other
required context, especially `versions`, and read back the live ruleset afterward.

The two baseline keys describe deployment and development intent; equality is gated.
Changing either for another upgrade requires a separately reviewed migration design.
Changing `dev_major` alone must never change the major of a serving generation.

`postgres.migration.source_major` and `postgres.migration.target_major` retain the
fixed 16-to-17 migration contract. The accepted nonempty serving generation uses
that target major, while new empty-generation deployments use `deploy_major`.
Persistent generation selection and the `serving:<generation>` fence prevent an
ordinary deployment from returning to the retained incumbent. Neither baseline
convergence nor a development-major bump permits clearing that fence.

The replacement template, migration job and driver read the fixed migration
contract, rather than interpreting today's serving major as yesterday's source.
The runtime client must cover the baseline and both retained migration endpoints.
See [cloud recovery](postgres-cloud-recovery.md) for acceptance and recovery limits.

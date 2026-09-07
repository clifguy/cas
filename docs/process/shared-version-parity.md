# Shared-component version parity

Some versions have to be the same across surfaces that are built and run by
different toolchains. When each surface names its own, nothing relates them, and
they drift apart quietly — the failure is not a red check but a statement that
works everywhere it is tested and breaks where it is deployed.

[`versions.json`](../../versions.json) at the repository root is the single
declaration of every version held in parity. This document states which those
are, how each consumer gets the value, and which differences between surfaces
are structural and therefore deliberately *not* held.

## What is held

| Component | Key | What it governs |
|---|---|---|
| PostgreSQL, deploy floor | `postgres.deploy_major` | The Flexible Server the cloud profile provisions, the Postgres client shipped in the SAGE image, and the CI job that runs the storage tests against that major. |
| PostgreSQL, development | `postgres.dev_major` | The workstation server and the CI service containers. |
| Python | `python.version` | The interpreter every workflow sets up, the container base images, the project's `requires-python` floor, and Ruff's language target. |
| Node | `node.major` | The frontend CI jobs, the SPA-builder container base, and the `@types/node` floor. |

**The two Postgres majors are not drift.** They are two named values with
different jobs. The deployed server sits on the floor; the workstation and CI
run ahead of it. The gap is what makes a statement valid only on the newer major
— `ALTER COLUMN ... SET EXPRESSION AS` is the worked example — detectable before
it reaches a deploy, and the `storage-deploy-floor` CI job exists to detect it.
The parity rule holds each value to its own consumers; it does not collapse the
two. `dev_major` may not fall below `deploy_major`, and that is asserted.

## How each consumer gets the value

Consumers that can read a file at the moment they need the value **read** it.
Consumers that structurally cannot **restate** it, and a gate holds the
restatement to the declaration. The distinction matters: a reader cannot drift,
while a restating site can and is only as safe as the gate over it.

### Readers

- **The infrastructure template.** `infra/modules/postgres.bicep` defaults its
  `postgresVersion` parameter from `loadJsonContent('../../versions.json')`. The
  manifest's contents are embedded in the compiled ARM template at build time.
- **The workflows.** Each workflow that needs a version carries a small
  `versions` prelude job that emits the declared values as job outputs, and its
  other jobs read `needs.versions.outputs.*`. A prelude job rather than a step
  because a service container's `image` resolves before any step in its own job
  runs, so it cannot read a value a sibling step computed — but it can read
  `needs.<job>.outputs`. The `python-version` and `node-version` pins go through
  the same outputs so there is one mechanism rather than two.
- **The test suite.** `tests/helpers/versions.py` reads the manifest directly.

### Restating sites, and why each cannot read

| Site | Why it cannot read |
|---|---|
| `Dockerfile`, `Dockerfile.bff` `FROM` lines | A `FROM` line takes no file input, and the readable tag has to stay on that line ahead of the digest for Dependabot's `docker` ecosystem to parse it. |
| `pyproject.toml` — `requires-python`, `[tool.ruff] target-version` | Static TOML; no interpolation. |
| The `storage-deploy-floor` job's `name:` | See below — it must stay a literal. |
| `docs/process/postgres-local-runtime.md`, `postgres-backup-restore.md` | Prose a person follows by hand. |
| `README.md`, `CLAUDE.md` | Prose. |
| `.github/dependabot.yml`'s `@types/node` note | Prose inside a config comment. |

`tests/infra/test_shared_version_parity.py` and
`tests/infra/test_frontend_node_version.py` hold every one of those to the
manifest, and each check proves it located its site before comparing, so a
parser that silently matched nothing cannot pass.

## The required check context is a literal, on purpose

The CI job that runs the storage tests on the deploy floor is named
`storage tests on the deploy floor (pg16)`, and that name is a **required status
check context** in the branch ruleset. It is not templated from the prelude job's
output, because a context that changes whenever the declared major does would
block every merge while GitHub waited on a context that never arrives.

The consequence is that the floor major appears in four places that must move
together, and only three of them are in this repository:

1. the job's `name:` in `.github/workflows/ci.yml`,
2. the required-check table in [`branch_protection.md`](branch_protection.md),
3. the captured ruleset JSON in the same document,
4. **the live GitHub ruleset**, which no test can reach.

Changing `postgres.deploy_major` therefore means editing the live ruleset in the
same change. See `branch_protection.md` for how.

## Beyond the repository: the live server

Every check above compares tracked files with each other, which establishes that
the repository is self-consistent — a weaker claim than it looks, because the
repository can agree with itself about a major the deployed server does not have.
That is not hypothetical on this surface: configuration applied outside the
template has drifted here before.

So the deploy preflight (`deploy/cloud-preflight.sh`, check `postgres_major`)
reads the **live** server's major from the Azure control plane through
`deploy/postgres-version-probe.sh` and compares it with the declaration. A
control-plane read rather than a database connection, because the server
integrates privately into a delegated subnet and is not reachable from a runner.
The probe is seamed like the other authenticated probes: unset, the check skips,
so an operator shell without an `az` session does not fail the gate on a
question it cannot ask.

The test suite makes the same check for the server it is itself running against
(`tests/infra/test_workstation_postgres_major.py`). It **fails under CI**, where
the service container's tag is derived from the manifest and a mismatch means
the derivation did not take effect, and **warns on a workstation**, where the
server is the developer's own and failing the suite over its major would make it
unusable during an upgrade.

## What is deliberately not held

These differ between the local and cloud profiles by design (CAS-ADR-042). They
are not parity failures and no gate should be written for them:

- **Managed identity.** The cloud profile authenticates through Entra; the local
  profile has no directory and no managed identity to hold in common.
- **The vault-source binding.** Local and cloud profiles bind different source
  stores. The binding is a profile property, not a version.
- **The host platform.** The cloud runs Linux containers; the workstation is
  macOS on Apple Silicon. Package managers, paths and service supervision differ
  throughout, and the runbooks are written to each.
- **The abstraction provider.** The MLX-accelerated model is Apple-Silicon-only.
  CI and the deployed containers stub it. There is no shared version to declare.

The test for whether something belongs here: would two surfaces disagreeing
about it produce a statement that passes every gate and fails at deploy? If not,
it is a difference, not drift.

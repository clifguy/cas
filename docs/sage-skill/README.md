# SAGE skill maintenance and package preparation

`skills/sage/` is the canonical operational skill, owned by CAS. It is independent
of the development-workflow distribution and named `sage` on both supported hosts.
The retained source provenance is in `source-provenance.json`; `coverage.md` maps
all gap and portability obligations. Hosted vault documents are historical inputs
only. No future vault storage or publication is selected. Distribution design is
an owner decision in a separate plan.

Edit this one operational core, retain progressive relative references and validate
against current served contracts, target-vault configuration and observed state.
Do not synchronize hand-maintained platform variants. Run the skill frontmatter
validator and `tests/skills/`; observable host decision trials are a separate gate
from static content and reference verification.

## Reproducible package

The supported build command requires a clean exact source revision:

```sh
.venv/bin/python scripts/sage_package.py build --source "$PWD" --output /absolute/new-package
.venv/bin/python scripts/sage_package.py verify /absolute/new-package
```

The package manifest records canonical CAS commit, CAS Git release identity, every
file hash and mode. The manifest's SHA-256 is the package identity; retain it
outside the package as the trusted expected digest. `manifest.json` cannot hash
itself. Building twice from the same source produces identical bytes. This does
not create a new release/version stream or publish an artifact.

The package includes `sage/`, a small installer adapter and the exact pinned shared
transaction engine. The generated engine export in `scripts/sage_installer/` is
owned upstream by `resurrectionit/development-skills`; never hand-edit it. Update
it only from a reviewed exact export, record the upstream revision and hashes in
`scripts/sage_installer/runtime-manifest.json`, run upstream regression/fault tests and CAS integration tests.
No workflow roster, reviewer asset, source checkout, server package or third-party
Python module is needed at installation time. POSIX Python 3.10+ is required for
the shared transaction engine; other operating systems are unverified.

## Explicit targets and transactions

Use an explicit existing personal configuration root. Codex's selected personal
root can be `$CODEX_HOME` (default `~/.codex`); Claude's can be
`$CLAUDE_CONFIG_DIR` (default `~/.claude`). Verify current native support and selected
path on the actual host before choosing a cutover target. Package labels do not
prove host discovery or precedence. These examples are for an authorized target:

```sh
python /absolute/package/installer.py plan --root /absolute/personal-root --target-kind codex-personal --bundle /absolute/package > /absolute/plan.json
python /absolute/package/installer.py apply --plan /absolute/plan.json
python /absolute/package/installer.py verify --root /absolute/personal-root --target-kind codex-personal
python /absolute/package/installer.py rollback --root /absolute/personal-root --target-kind codex-personal --transaction EXACT_TRANSACTION
```

Use `claude-personal` for Claude. Only `skills/sage` is installed. Manifest, lock,
receipts, transaction journal and exact before/after backups live under
`.skill-install`, never `skills/manifest.json` or another host-reserved location.
An existing unowned `skills/sage` requires explicit `--adopt skills/sage` on the
plan; the plan binds its observed bytes, not just its name. Read the plan before
apply. Drift, stale plans and concurrent locks refuse before mutation. An
interrupted installation blocks use until `recover` restores the recorded state.
Rollback requires the current exact transaction and refuses newer owned edits.
Retain package, trusted manifest digest, plan and transaction backup for recovery.

## Later cutover gate

This source change prepares packages and trials only. Active installation, pointer
migration, compatibility retirement, memory edits and live write smoke remain
separate authorizations. See `migration.md` for the concrete preparation boundary.
A source PR can be ready while later acceptance remains pending. Report source
checks, deterministic trials, native host behavior, authenticated reads, live
writes/transfers and actual active installation independently.

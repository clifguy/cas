# Change records

Every change to the published contract carries a **change record**: one YAML
file under `unreleased/` stating whether the change is a patch or requires a new
minor release (CAS-ADR-008). A record carries no version number. The release
step assigns every unreleased record to the release that publishes it, folds the
records into `revision_history` in `../manifest.json`, and deletes the files.
Concurrent changes therefore add independent files instead of contending for the
next number, which is what made them conflict at merge under the counters this
replaces.

## When a record is required

A change needs a record when it touches any of:

- a file listed in `../manifest.json`;
- either OpenAPI specification (`../sage/sage_core_api.openapi.yaml`,
  `../cas_app_api.openapi.yaml`);
- `../manifest.json` itself, outside a release (an artifact delisted, an entry
  reworded);
- the MCP tool catalog, `../sage/sage_mcp_tools.catalog.json`. The catalog is
  generated from the servers, and a test gate fails when it is stale, so a change
  to an MCP tool's signature or docstring reaches this rule through the catalog:
  regenerate it with `python -m scripts.dump_mcp_catalog --write`.

A description-only change needs a `patch` record. There is no separate escape
hatch.

## Format

The file name is a short kebab-case slug describing the change, ending in
`.yaml` (`scored-response-excerpts.yaml`). It is not parsed, but it orders the
release: records fold into the release entry, and their caller notes into the
tag message, in file-name order. Two concurrent changes choosing the same name
would conflict, so make it specific.

The fields are defined by `change_record.schema.json`:

```yaml
classification: minor            # patch | minor
category: [capability]           # minor only: capability, caller-adaptation, operator-adaptation
summary: >-                      # for maintainers: what changed, which artifacts
  Scored search responses over the inline budget are excerpted...
for_callers: >-                  # minor: release-note text; empty for a pure patch
  An oversize semantic or keyword response now arrives inline with long passages cut.
```

A record is published text: the substrate's public-posture scan reads it, so it
names no tickets, pull requests, or people.

*CAS Release Classification* (a steering document in the CAS vault) enumerates
which changes fall in each category and what stays a patch. When unsure,
classify minor; the owner can downgrade.

## The contract comparison

`python -m scripts.substrate_changes check` runs the gate locally. It compares
the working tree with its merge base on `origin/main`; pass `--base` and
`--head` for a commit range. CI runs it on every pull request.

The check compares both OpenAPI specifications and the MCP catalog. If it finds
an added operation, tool, parameter, accepted value, or response field, or a
removal, rename, new requirement, or narrowed schema, a `patch` record fails. When
the owner judges such a finding a patch anyway, for example because it declares
behaviour the server already had, the record carries the owner's override:

```yaml
classification: patch
summary: Declare the three error codes the batch ingest already returned.
detector_override:
  reason: The server already returned these codes; the contract now lists them.
```

The override covers the change's findings as a whole, not one finding, so its
reason should account for all of them; the gate warns when a record carries an
override the comparison finds nothing for. The override is for the owner to
write, not the author. The comparison sees
shape, not meaning: a changed default behind an unchanged schema, and every
operator-facing change, still depend on the author's classification.

## What a change may not do

Only a release moves a version. A change that is not a release fails the gate if
it edits an artifact's `version` in the manifest, a specification's
`info.version`, or `revision_history`, or if it deletes another change's record.

The release procedure is in `docs/process/substrate-releases.md`.

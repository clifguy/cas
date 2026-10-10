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
removal, rename, new requirement, or narrowed schema, a `patch` record fails. An
added response status is classified by what it answers: a success (2xx) is a
capability, and any other status is a refusal a caller must now handle, so a
caller adaptation. The same holds for an error code a tool's published error
schema gains or loses, and for an arm added to a union only non-2xx responses
reach. When
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

## Deprecation before adaptation

CAS-ADR-008 clause 8 has a caller adaptation that withdraws or changes something
callers use follow a deprecation of it. The gate holds a removal (a rename is a
removal beside an addition), a narrowed input, and a changed default to that rule.
It applies to adaptations released after 3.0, and reads deprecations only from
releases after 3.0.

**Deprecating.** Mark the element on the contract in the same change:
`deprecated: true` on the OpenAPI operation, parameter, response header, or
schema, or an MCP tool or parameter description that opens with `Deprecated:`.
On MCP, JSON Schema's own `deprecated: true` in a parameter's input schema also
counts; with the description prefix it is still one deprecation. The record names what it
deprecates, the replacement, and the earliest date the adaptation may ship:

```yaml
classification: minor
category: [caller-adaptation]
summary: Deprecates the limit parameter of the things listing.
for_callers: >-
  limit is deprecated; use page_size. It may be removed from 2027-01-15.
deprecates:
  - surface: sage_core_api          # sage_core_api | cas_app_api | mcp
    pointer: paths//things/get/parameters/query:limit
    replacement: page_size
    earliest_adaptation: "2027-01-15"   # quoted, so YAML keeps it a string
```

The pointer is the one the contract comparison prints in its findings. The gate
fails a change that marks a deprecation no record declares, and a record that
declares one the contract does not mark. The exception is a deprecated value,
value format or default: its parameter's description states it, so no mark is
looked for. A deprecated default's pointer ends in `/default`; a deprecated
value's is `.../enum/<value>`, the pointer its removal is reported at, so the
deprecation covers that value and no other. A deprecated value format, a class
of values such as timestamps, ends in `.../format/<name>`. A `/` in the value is written `~1` and a `~`
is written `~0`, as in a JSON Pointer. For an operation listed in
`WARNING_CARRIERS` in `sage/services/deprecations.py`, declare the form there
too, so a caller using it is warned in the response; another operation first
needs a warnings field and its service wired to that module.

**Adapting.** The adaptation ships in a later release than the deprecation, and
no sooner than 30 days after that release's date in the manifest's revision
history, or the recorded `earliest_adaptation` if later. Its record names the
deprecation it follows. A deprecated element covers the elements inside it, so a
deprecated operation covers the removal of its parameters:

```yaml
classification: minor
category: [caller-adaptation]
summary: Removes the limit parameter deprecated in release 3.1.
for_callers: limit is gone; use page_size.
follows_deprecation:
  - release: "3.1"
    surface: sage_core_api
    pointer: paths//things/get/parameters/query:limit
```

**Exempt.** An adaptation ships without a deprecation, or before its window
closes, only for a `security-exposure`, a `data-integrity` fault, or an element
`never-served` to a caller. The record names the exemption and the reason, and
its `summary` and `for_callers` say so too. The exemption covers the change's
withdrawing findings as a whole:

```yaml
exemption:
  kind: never-served
  reason: The operation was specified but no server implemented it.
```

## What a change may not do

Only a release moves a version. A change that is not a release fails the gate if
it edits an artifact's `version` in the manifest, a specification's
`info.version`, or `revision_history`, or if it deletes another change's record.

The release procedure is in `docs/process/substrate-releases.md`.

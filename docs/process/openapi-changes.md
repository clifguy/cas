# Changing a SAGE Core API response field

Renaming or reshaping a response field on the SAGE Core API touches more
surfaces than the schema block alone. A schema-only edit ships a one-sided
rename that the conformance suite or the frontend build will reject — or,
worse, stale prose that no gate catches. Work through the full blast radius.

## Blast radius — touch every one

1. **Pydantic model** — `sage/models/schemas.py`: the response-model field, and
   its membership in any `required` set.
2. **OpenAPI schema** — `docs/fs/sage/sage_core_api.openapi.yaml`: the
   `properties` entry, the `required` list, and **every description**. A schema
   property description must be **byte-for-byte identical** between the Pydantic
   `Field(description=...)` and the YAML, or the verbatim-description conformance
   test (`test_pydantic_descriptions_match_yaml_verbatim`) fails.
3. **OpenAPI operation prose** — the endpoint `summary:`/`description:` text
   that names the field. The schema-description gate does not cover it, so a
   stale mention still ships silently unless you grep for it. What *is* covered:
   this prose is served verbatim at `/openapi.json`, and
   `test_published_operation_prose_matches_committed_specs` pins the served
   document to the YAML — so an edit here reaches external callers, and an
   attempt to fix the served text anywhere else fails that test.
4. **Producer code** — wherever the value is assembled (e.g. the service that
   builds the response kwargs).
5. **Frontend TypeScript interface** — `app/src/api/types.ts` is hand-mirrored
   (no transform layer); `tsc -b` is the boundary gate. Update the interface and
   every consumer.
6. **Tests** — hard-coded spot-guards in `tests/sage/test_openapi_conformance.py`
   and body/attribute assertions in the app and service test suites.
7. **Test-plan docs** — `tests/app/*.md` list response fields by name.

## Sweep

```sh
grep -rn '<old_field_name>' docs/ tests/ app/ sage/
```

A schema-block-focused search misses the operation prose (item 3) and the
test-plan markdown (item 7); the repo-wide grep catches them.

## Leave alone

- **`docs/fs/manifest.json` changelog summaries** — dated historical records,
  accurate as of their date; don't rewrite them.
- **Genuine internal identifiers** — adapter names, internal module paths, and
  similar that happen to share the old token but are not the renamed field.

## Note

A **symmetric** rename (Pydantic and YAML changed together, identically)
satisfies the description-parity and coverage conformance tests directly — no
allowlist edit is needed. An allowlist entry is a smell that the rename is
one-sided.


## Type names in component descriptions

A schema, property, or event description may name a type a reader can find
in either published API contract. An implementation class with no published
contract definition gives that reader nothing to resolve; describe its
behavior instead. A type alias that usefully names a published shape may
have an explicit, location-specific justification: the existing `Sha256Str`
reference on `verify_hashes` names the documented digest format. That
exception does not exempt aliases everywhere.

The component scan applies this criterion to the formerly unresolved sites:

| Description | Disposition |
| --- | --- |
| Core `ParseFilenameResponse` | Replace `FilenameParser` with filename extraction behavior. |
| Core `BatchIngestFileMetadata.parsed_metadata` | Replace `FilenameParser` with configured filename parser; retain metadata precedence. |
| Core `SummaryEvent` | Describe the summary returned to non-streaming callers without the internal `IngestSummary` dataclass name. |
| App `SummaryEvent` | Use the same summary description as Core. |

Keep Python class docstrings and field descriptions synchronized with these
YAML descriptions. The field-description parity gate enforces exact field
text; class docstrings also need review because that gate does not compare
them.

Run the repeatable scan and its regression checks with:

```sh
.venv/bin/python -m pytest tests/sage/test_narrative_schema_grounding.py -k component -n 0
```

The component check recursively reads description strings under both specs'
`components.schemas`, including nested schemas and events. It extracts
interior-capital names and subtracts the schema names declared in either
spec. It reports the spec surface, JSON pointer and name. Ordinary plural
acronyms (`UIs`, `IDs`, `PDFs`) carry reasoned pins at their five individual
locations; a pin that disappears or becomes resolvable fails the ratchet.
No description contributes vocabulary to its own validation.

This is a separate pool from the MCP/OpenAPI operation disclosure-parity
check. Type-name grounding asks whether a reference exists, while disclosure
parity compares what two operation narratives say. Widening their shared
reader would change parity scores and exception semantics, so it stays
unchanged. The component check does not detect lowercase internal names,
validate behavioral claims, or prove a declared type is appropriate in the
sentence where it appears; those remain review questions.

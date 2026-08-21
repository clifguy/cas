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

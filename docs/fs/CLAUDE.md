# Formal Substrate

Executable specifications for CAS: API contracts, configuration schemas, data model schemas.

## Conventions

- All schemas use JSON Schema draft 2020-12 (`$schema: "https://json-schema.org/draft/2020-12/schema"`).
- API specifications use OpenAPI 3.1.0.
- Schema files use `.schema.json` extension; API specs use `.openapi.yaml`.
- The root configuration schema (`sage/vault_config.schema.json`) uses `$ref` to compose the SAGE vault sub-schemas (enumerated in `manifest.json`).
- `manifest.json` is the inventory. Update it whenever schemas are added, removed, or promoted. A newly listed artifact carries `version: null` until a release publishes it.
- Versions follow CAS-ADR-008. An artifact's `version` is the release `MAJOR.MINOR` in which it last changed, and only the release step moves it. The same holds for both specifications' `info.version` and for `revision_history`. A change to the published contract instead adds a change record under `changes/unreleased/`; see `changes/README.md`. The release procedure is `docs/process/substrate-releases.md`.
- `sage/sage_mcp_tools.catalog.json` is generated. Regenerate it with `python -m scripts.dump_mcp_catalog --write` whenever an MCP tool's signature or docstring changes; a test gate fails while it is stale.

## Null fields on the wire

**The published schema decides whether a key appears in a response.** A
property the schema lists under `required` carries its key on every response,
null or not. A property it leaves optional may be omitted when its value is
null, and is.

This is the authoritative statement, and it holds for both surfaces. The rule
is applied per field, not per response: a serializer that drops every null
deletes keys the contract promises are always present, and one that keeps every
null inflates every response with keys carrying nothing. Which of those is right
is a question each field has already answered by being declared required or not.

Three consequences, each stated because it was got wrong before it was written
down:

- **Null is often a value, not an absence.** A null `duplicate_of` is the
  reported verdict *no document holds these bytes*. A caller cannot read that
  verdict off a key that is not there, which is why the field is declared
  required and its key always ships.
- **A model has one wire shape, on either surface.** The same model reached
  directly, nested in a batch result, carried on an event stream, or returned
  by the MCP surface or the Core API renders identically. The MCP surface
  renders through `sage.models.wire`; the Core API routes render through the
  route class in `sage.api.wire_route`, which applies the same rule to the
  declared response model. Code that assembles a response body by hand renders
  its nested models through `sage.models.wire` for that reason rather than
  dumping them itself.
- **Omitting a null does not excuse declaring it.** No body carries an
  optional null, so no body can show that its declaration refuses one. A field
  whose model admits null is declared nullable in the spec all the same,
  required or not, because the declaration is what a generated client is built
  from.

**An empty error `detail` is an absent one.** Its keys vary by error code, so an
empty object tells a caller nothing the code has not, and the Core API contract
already says `detail` is omitted when empty. The error normalizes an empty
`detail` to null where it is raised, so every envelope — MCP, Core API, bulk
per-item, batch-ingest per-file — omits it under the ordinary null rule rather
than by a test of its own.

One thing the rule does not reach, by decision rather than by oversight. The
stack-configuration report is dumped whole on both surfaces: its published
response declares only the sections callers most often read and admits the
rest, so the schema cannot say which absent keys a caller may rely on, and a
field the stack leaves unset is reported null rather than dropped.

Express nullability the OpenAPI 3.1 way — `type: [string, "null"]`, or an
`anyOf` branch beside a `$ref`. The 3.0 `nullable: true` keyword is not part of
3.1 and a 3.1 client ignores it, so a property declared that way reads as
non-nullable and the generated client rejects the null the server correctly
sends.

## Null fields in requests

**A request property admits null in the published schema exactly when its
model does.** For an optional request property, a null means the same as
leaving the property out: the surface accepts either and treats them alike. So
the schema declares the null arm wherever the model accepts one, and a client
validating against the schema can send what the surface serves. The rule runs
in both directions: a schema must not offer a null the model refuses.

This is the same declaration the response rule above relies on, which is why
one comparison holds both: every model field is compared with its spec
property, request and response components alike.

`tests/sage/test_wire_shape_conformance.py` enforces all of this. It serializes
a response both ways the surfaces render it and validates the body against the
component the spec declares for it, compares every model field's nullability
with its property's, and checks that every route the applications serve renders
through the wire route class.

**The MCP tool input schemas are the one declared exception.** The rule governs
the specifications' components. An MCP tool's published input schema carries an
optional parameter as its non-null form alone: `type: string` rather than
`anyOf: [{type: string}, {type: null}]`, with the null default dropped and a
`$ref` arm inlined. Some MCP clients discard `anyOf` when they load a tool, and
the parameter then reaches the model with no type at all. Leaving the parameter
out is the advertised way to send no value; a null is still accepted and still
means the same as omission, so only the declaration narrows, never what the
surface serves. Item schemas nested inside a parameter keep their null arms.
`tests/sage/test_mcp_input_schema_publication.py` enforces this form.

## Validation

```bash
# Validate a schema file against JSON Schema meta-schema
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('FILE')), json.load(open('/path/to/meta-schema')))"

# Validate a domain YAML config against its schema
python3 -c "import yaml, jsonschema, json; jsonschema.validate(yaml.safe_load(open('CONFIG.yaml')), json.load(open('SCHEMA.json')))"
```

## Source Authority

- SAGE schemas: developed against the SAGE Architecture Reference, maintained in the CAS SAGE vault. See ../ref/README.md for vault access.
- ROOT Harness schemas: developed against the ROOT Harness Architecture Reference, maintained in the CAS SAGE vault.
- Changes to architecture documents or schemas should trigger a conformance check in the other direction.

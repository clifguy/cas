# Formal Substrate

Executable specifications for CAS: API contracts, configuration schemas, data model schemas.

## Conventions

- All schemas use JSON Schema draft 2020-12 (`$schema: "https://json-schema.org/draft/2020-12/schema"`).
- API specifications use OpenAPI 3.1.0.
- Schema files use `.schema.json` extension; API specs use `.openapi.yaml`.
- The root configuration schema (`sage/vault_config.schema.json`) uses `$ref` to compose the SAGE vault sub-schemas (enumerated in `manifest.json`).
- `manifest.json` is the inventory. Update it whenever schemas are added, removed, or promoted.

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
- **A model has one wire shape.** The same model reached directly, nested in a
  batch result, or carried on an event stream renders identically. Code that
  assembles a response body by hand renders its nested models through
  `sage.models.wire` for that reason rather than dumping them itself.
- **The REST transport sends every key**, which satisfies a required and an
  optional declaration alike, so it conforms without doing anything special.
  The transports differ only within what the contract permits, and a client
  generated from either spec parses both.

Two things the rule does not reach, by decision rather than by oversight. The
MCP error envelope is assembled by hand and omits an empty `detail` on
falsiness rather than on null. And a response with no published schema at all,
such as the stack-configuration report, is dumped whole: the rule's authority is
the schema, and there is none.

Express nullability the OpenAPI 3.1 way — `type: [string, "null"]`, or an
`anyOf` branch beside a `$ref`. The 3.0 `nullable: true` keyword is not part of
3.1 and a 3.1 client ignores it, so a property declared that way reads as
non-nullable and the generated client rejects the null the server correctly
sends.

`tests/sage/test_wire_shape_conformance.py` enforces all of this by serializing
a response and validating it against the component the spec declares for it.

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

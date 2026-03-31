# Formal Substrate

Executable specifications for CAS: API contracts, configuration schemas, data model schemas.

## Conventions

- All schemas use JSON Schema draft 2020-12 (`$schema: "https://json-schema.org/draft/2020-12/schema"`).
- API specifications use OpenAPI 3.1.0.
- Schema files use `.schema.json` extension; API specs use `.openapi.yaml`.
- The root configuration schema (`sage/vault_config.schema.json`) uses `$ref` to compose the five SAGE sub-schemas.
- `manifest.json` is the inventory. Update it whenever schemas are added, removed, or promoted.

## Validation

```bash
# Validate a schema file against JSON Schema meta-schema
python3 -c "import jsonschema, json; jsonschema.validate(json.load(open('FILE')), json.load(open('/path/to/meta-schema')))"

# Validate a domain YAML config against its schema
python3 -c "import yaml, jsonschema, json; jsonschema.validate(yaml.safe_load(open('CONFIG.yaml')), json.load(open('SCHEMA.json')))"
```

## Source Authority

- SAGE schemas: developed against SAGE Architecture Reference (docs/ref/).
- ROOT Harness schemas: developed against ROOT Harness Architecture Reference (docs/ref/).
- Changes to architecture documents or schemas should trigger a conformance check in the other direction.

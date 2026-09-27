# Authorized vault configuration changes

Document-write permission does not authorize schema or configuration changes.
Read the exact target vault's current configuration and the served update contract
before proposing or making a change. Observed facets cannot replace declared rules.

Section updates replace that whole section. Preserve every existing key that is
meant to remain; `document_types` is also a whole section, not a one-type patch.
Build the replacement from a fresh read, make the intended edit and inspect a dry
run where supported. Readback includes defaults: resending that exact section can
report no change, while omitting default-filled keys can report a change even when
it looks equivalent. `changed_sections` reports the submitted replacement, not
proof of a semantic change or a successful write.

After an authorized write reread the section and compare intended values and
preserved keys; where relevant use a supported dry-run lifecycle action to inspect
valid actions. Reconcile failures before retry. Do not add `force` just to make a
refusal pass; destructive warnings can require a new scope decision.

For a schema migration, inventory stored values before narrowing. Reads can accept
a declared filter key without validating historical values against the proposed
write schema; a readable row is not proof its next write will validate. Stage the
migration as widen, authorized row backfill, verify all affected rows, then narrow.
A dry-run write with a retired value can expose `tier3_schema_violation`. Handle
concurrent writers and interrupted phases explicitly; do not claim migration
complete while incompatible values remain.

`source_types` on a document type constrains filename-based type resolution. An
explicit, schema-valid `doc_type` can bypass that filename inference constraint;
a new source format alone is not a reason to edit vault configuration. Confirm the
served adapter/source-type support separately.

# Diagnose before repair

A successful ingest receipt can precede projection, indexing and abstraction.
`no_projection` immediately afterwards may be a race. Read the stored document by
its returned ID and inspect pipeline status/error, then make a bounded read-only
wait appropriate to the served contract. One final status read is better than a
poll after every unit of work. Search absence is not document absence.

Pipeline recomputation is a mutation, not a harmless diagnostic probe. Only after
read diagnosis establishes a recoverable condition and repair is authorized use
the supported repair operation. Its `already_in_flight` refusal means processing
is underway: return to bounded reads, do not force or duplicate work. A persistent
missing projection, failed pipeline or interruption remains incomplete; terminal
failure is not successful completion. Preserve the exact typed error and completed
steps. Report uncertainty when supported reads cannot establish the outcome.

For apparently lost references inspect complete lineage and literal edge rows as
[retrieval guidance](retrieval.md) describes. Do not copy valid predecessor edges
to the successor. For a fresh write missing from catalog filters, read the stored
row by the returned ID before any retry. Compare its actual metadata to the
intended arguments; client-side parameter filtering and default values can explain
the mismatch. Correct metadata through an authorized patch, not duplicate ingest.

An uncertain write or transfer requires identity/hash/readback reconciliation
before replay; follow [transfer recovery](transfer.md). A validation refusal needs
its declared correction, not a broader call or a guessed schema. Permission
refusals retain the host's approval boundary.

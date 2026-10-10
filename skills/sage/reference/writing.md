# Writing to a SAGE vault

Read this before any call that creates or changes a document. The mechanics
belong to the tool descriptions: which parameters exist, which errors are
raised, what to wait for. What follows is the ordering and the judgment, which
no single description can carry.

## Before any write

Establish the exact deployment, vault, action and scope. An explicit request
or approved plan already covering those supplies authorization; reuse it for
the named operation, its transfer/completion legs, and bounded in-scope
recovery. State the resolved target without asking for the same approval again.

If the target/action is inferred, ambiguous, or outside that approval, ask
and wait before the dependent write. Permission to read or to write one
vault does not authorize a different vault, unrelated lifecycle change,
schema change or broader mutation. Preserve any separately required owner
decision such as ticket closure. Revalidate scope if the target or intended
effect changes.

When a call offers a dry run, use it to check an uncertain transition before
making it. A refused dry run costs nothing and reports exactly what the real
call would have said, including the actions the document's current state
actually permits.

## Adding a document

**Look for a predecessor first, every time.** This single step prevents the
most common and least visible corruption in the whole system.

1. Search the vault for an existing document on the same subject, with the
   same title, or carrying the same identifier.
2. If one exists and this is a newer version of it, ingest with the
   predecessor named. That archives the old one and links the two in a single
   step.
3. If one exists and this is genuinely a different document, say so out loud
   and proceed without the link.
4. If none exists, ingest normally.

Why it matters: an edited file has a different content hash from the original,
so the duplicate guard does not fire. The file lands as a second active
document. Nothing links it to the first, and no error is raised. Whoever reads
the vault later finds two active documents on one subject and has no way to
tell which is current. Both look equally real.

Re-ingesting genuinely unchanged content is safe and is refused by hash, with
the refusal naming the document that already holds those bytes. Treat that
refusal as a skip signal, not as a failure.

Ingest data files as they are. JSON, JSON Lines, YAML, TOML, XML, CSV and TSV
are a source format of their own, so do not convert one to Markdown first; the ingest
tool's source-type description lists every format with the extensions it is
inferred from.

## Metadata

Supply the values you know rather than leaving them to be inferred from the
filename. Ask the person for anything you cannot determine. A document that
arrives with wrong metadata is a document nobody finds again.

Check the vocabulary from the facets call before choosing a document type or
a lifecycle value. Both are declared per vault, and a value borrowed from a
different vault is rejected.

A filename can supply metadata at ingest, but it never makes a document
findable by search. Naming conventions are an ingest convenience, not a
retrieval strategy.

Read the current ingest schema before forming arguments: nested document metadata
and top-level typed metadata/provenance have distinct locations. Client catalogs
can silently discard misplaced arguments; do not freeze an old parameter list in
this skill. Read back the stored row by returned ID and compare intended type,
metadata and provenance even if a catalog query returns nothing. Preserve original
submitter fields where applicable; prefer the host's automatic agent identity
unless intentionally asserting a role. Filename inference does not replace this
readback. See [diagnosis](diagnosis.md).

## After an ingest

Ingestion continues in the background after the call returns, but the document
is usable at once: it can be read, patched, linked and superseded. Only passage
search waits on indexing, and the semantic abstract on abstraction. Report the
ingest on the verified document record and its readback, not on the pipeline.

Wait earlier only when the next step needs passage search or the abstract.
Otherwise settle every document a batch or session ingested with one bounded
wait at its end, rather than checking after each unit of work. The tool
description names the terminal states and explains why the bound matters.
Report failed terminal states, interruptions and unresolved waits separately;
terminal alone does not mean successful.

## Revising a document

Use the supersede action. It archives the predecessor and creates the link
atomically. Do not hand-roll it as an edge creation followed by an archive:
creating an edge does not move the predecessor's lifecycle, so that path
leaves the vault in a state that looks right and is not.

Typed per-vault metadata does not carry across a supersession. Supply it again
on the new version, or it is lost.

Edges on a predecessor still resolve from its successor through the version
chain, so the new version showing no edge rows of its own is correct. Do not
re-create or re-anchor them; that duplicates what the chain already resolves.

If the document rests in a state that does not permit supersession, inspect
the live transition contract. Use a supported transition, supersession and
resting-state restoration only when the existing authorization explicitly
covers those lifecycle effects. Otherwise stop for the missing decision; a
refusal does not authorize changing lifecycle merely to make the call pass.
Track each completed step and reconcile interruptions before proceeding, so
a temporary state is never silently reported as the intended resting state.

A completed predecessor can produce an active successor. Read the vault's allowed
transitions before revising it; after successful terminal processing restore the
intended resting state with its supported completion action and read it back.
Preserving an already authorized resting state does not authorize a new closure
judgment or reopening. A failed or interrupted pipeline must be reported, not
silently completed merely to reproduce the predecessor's label.

## Editing a document that is already in the vault

Spill the **source** bytes to a file, edit that file, and ingest it as a
supersession. Do not spill the projection and edit that. The projection is
reconstructed text; ingesting it back produces a whole-file difference that
buries the actual change.

## Changing metadata without changing content

Patch it rather than re-ingesting. Scalar fields are set-or-omit, and the
list-valued and typed-dictionary fields take operation objects rather than
bare values. The description spells out the shapes. The shape is a
concurrency contract, not a style preference: it lets two callers add
different values without clobbering each other.

## Creating graph relationships

Read the served batch and edge schemas before writing: array names, required
anchors, document-ID versus version-label values, and rationale enums are contract
facts, not guesses. Use the declared hand-authored rationale when appropriate.
Only create supported relationships with sufficient evidence; verify literal
production rows separately from staged proposals. See [retrieval](retrieval.md).

## Dependency edges

A `depends_on` edge is satisfied by any state the vault's lifecycle marks
dependency-satisfying, which by default is `active` and `completed`, so a
target still open satisfies it; to model "blocked until finished", the vault
must declare `satisfies_dependency: false` on `active`.

## Bulk

Bulk ingest diverges from single-document ingest in ways that surprise people,
particularly around metadata review and version chains. Read its description
before using it, and prefer single ingests when there are only a few files.

## Never

- Never write without established authorization for the exact target and action.
- Never create a second active copy of a document the vault already holds.
- Never edit files inside the vault tree directly, by any means. Everything
  goes through the tools. Direct changes break provenance and desync the
  stores, and nothing surfaces the damage until much later.

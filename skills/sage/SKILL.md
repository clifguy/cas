---
name: sage
description: "Operate SAGE knowledge vaults: discover and read documents, ingest and revise retained sources, follow versioned graphs, and diagnose vault operations. Use when working with a SAGE deployment or its documents, including unexpected tool results."

---

# Working a SAGE vault

SAGE is a document graph. A vault holds documents; each one has extracted
text, typed metadata, a lifecycle state, and typed edges to other documents.
It is not a file store and not a vector database with a wrapper. Holding
either model wastes calls.

**The served tool descriptions are authoritative for every mechanic.**
Parameters, error codes, retrieval modes, what to wait for, how bytes are
delivered: all of it is documented on the tools themselves, generated from
the code the deployment is actually running. Read them rather than guessing,
and trust them over anything you remember. This skill deliberately does not
repeat them. It carries only the decisions that span tools, which no single
description can state.

## Five rules

1. **Orient before acting.** Never filter, ingest, or answer on a vocabulary
   you assumed.
2. **Resolve the vault and establish write authorization.** Reuse explicit
   authorization for the same target and action; ask when it is missing or
   ambiguous. See `reference/writing.md`.
3. **Look for a predecessor before every ingest.** Adding a revised document
   without linking it forks the record, silently and permanently.
4. **Read a tool's description before supplying a parameter you have not
   used.** They are long because they are complete.
5. **Read a document with the projection call.** Never reconstruct one by
   stitching search results together.

## Orienting

Do this once per vault per session, before anything else.

1. **`list_vaults`** gives the roster with names and descriptions. It is on
   the ordinary tool surface of an accessible registered deployment. If that
   surface is unavailable, follow `reference/runtime.md`; do not infer an
   empty inventory.
2. **`search` in catalog mode against the facets target** gives the
   vocabulary actually present in that vault: its document types, lifecycle
   states, tags, and more, each with counts. This is the call that tells you
   what is in here.

Every vault declares its own document types, lifecycle states, and edge
tiers. Nothing learned from one vault transfers to another. If a filter is
rejected or returns nothing, suspect that this step was skipped.

If the maintenance tool surface is also registered, `get_vault_config` gives
the declared rules rather than the observed vocabulary: exact lifecycle
actions, typed metadata fields, the filename pattern. It may not be
registered. Facets suffice for observed vocabulary, not declared schemas or
uniqueness guarantees. When the operation or caller requires those rules,
obtain them through an available live surface or stop the dependent write.
Use typed errors to diagnose a refused call, not speculative writes to
discover a schema.

## Choosing the vault

A request rarely names a vault. Resolve it by matching what the person asked
for against the descriptions from `list_vaults`.

- **Reading:** pick the best match, and say which one you chose.
- **Writing:** establish the exact deployment, vault and action. An explicit
  request or approved plan that identifies them already supplies authorization;
  state the resolved target and proceed within that scope. If the target or
  action was only inferred, ask and wait. Apply `reference/writing.md`.
- **Genuinely ambiguous:** ask. Two plausible vaults is a question, not a
  coin flip.

## Routing

| What the person wants | What to do |
|---|---|
| "What do we know about X" | Orient, then semantic search, then read the promising results in full |
| "Find the doc about X" or "the one called Y" | Catalog mode with filters when the metadata is known; semantic search when it is not |
| "List all X" or "every open Y" | Catalog mode with filters. Never use semantic search to enumerate; it will miss records and say nothing |
| "An exact phrase or an identifier" | Keyword mode. Read its description first; its matching rules repay the minute |
| "The latest version of X" | Find any member of the chain, then follow the supersedes edge to the head |
| "What replaced this" or "what did this replace" | The chain call, not traversal |
| "What relates to this" | Traversal |
| "Are this document's dependencies met" | The preconditions check, rather than walking edges yourself |
| "Who wrote X" or "what did Y change" | Catalog mode with a provenance filter; a display name resolves to the stable key. For an overview, add the provenance fields to the facets call |
| "Add this" or "put this in the vault" | Read `reference/writing.md` first |
| "Update X" or "revise the Y policy" | A supersession. Read `reference/writing.md` first |
| "Mark this done", "retire this", "drop this" | A lifecycle transition. Read `reference/writing.md` first |

## Reading

Read [retrieval guidance](reference/retrieval.md) for enumeration, version identity,
literal edge anchors, large results, or retained binary source retrieval.

- Whole document: the projection call.
- One section of a long document: list the headings first, then read the
  section by an exact heading path.
- Anything large: spill it to a file path rather than pulling it inline.
- Enumerating many: ask for the light response shape, then fetch detail only
  for the few you actually want.

## Writing

Read `reference/writing.md` before any call that creates or changes a
document. It carries the ordering the per-tool descriptions cannot.

## Host compatibility

Use this complete skill folder with its relative references on Claude or
Codex. Read [the runtime guidance](reference/runtime.md) before first use on
a host, or when a required capability is missing. A skill is instructions,
not a callable tool; resolve the host's actual tools and the selected
deployment's served contract.

## Moving bytes

If a call returns `upload_required` or `download_required`, it has not
failed. It handed back a recipe that the caller runs. Read
`reference/transfer.md`.

## When something looks wrong

Read [diagnosis](reference/diagnosis.md) before repair. For authorized configuration
changes, read [configuration](reference/configuration.md).

Report what the tool actually returned, including the status and the detail.
SAGE errors are typed and usually carry the remedy inside them, and the
description of the tool you called enumerates them. Do not retry a refused
call with different phrasing, and do not work around a validation error;
both mean the call was wrong in a way the server has already explained.

Never edit files inside a vault directly, by any means. Every read and every
write goes through the tools. Direct changes break provenance and desync the
stores, and the damage is not visible until much later.

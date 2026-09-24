---
name: cas-code-review
description: This skill should be used when the user asks to review a CAS commit, branch, or diff for the documented failure modes (F1-F5), the gate-integrity check (G1), the claims, surface-parity and stored-data checks (C1, S1, R1), and the public-posture gate (P1) catalogued in the skill's own section list. Trigger phrases include "cas code review", "review for CAS failure modes", "audit cas changes", "check this commit for cas drift", "run cas-code-review", and similar. The skill is tuned to the CAS repository specifically; do not invoke it on unrelated codebases.
version: 0.10.0
---

# cas-code-review

A repo-resident review pass. It runs at commit time, before a pull request exists, and its sections are the failure modes the CAS failure log (vault: cas, `doc_type=failure_record`) shows a commit-time review has to catch. The first five derive from the AI-First SDLC Tooling Survey §3 (vault: cas, `doc_type=reference_document`); the rest were earned by later failure records.

## Portability notes (forward-looking, not architectural)

This skill is single-tier: every section lives in this file. G1, F4, C1 and R1 name properties of verifications, change sets, claims and stored data, and their abstract form should carry to any stack; F3, S1 and the collapsed F1/F5 lines embed FastAPI, Pydantic v2, MCP and the SAGE multi-vault architecture. Generalization is a hypothesis until a second repo needs its own skill; that is the trigger to extract a universal layer, not before.

## Section structure

Full sections carry four parts: **What this catches**, **Already gated deterministically** (what CI catches without the skill, which the skill must not duplicate), **Review prompts**, and **What "correct" looks like**. Sections whose structural case a deterministic gate now owns, and where no recent record has landed, are collapsed to one line and walked as a one-line check.

Section families:

- **F** — code-correctness failure modes from the Tooling Survey (F1–F5).
- **C, S, R** — code-correctness sections earned by later records: behavioural claims against code (C1), the same answer on every surface and against the published contract (S1), and every writer and reader of stored data (R1).
- **G** — gate integrity: whether a verification in the diff can go red, which is why a defect of a given shape would survive review rather than how it was written.
- **P** — public-repo posture: a release-discipline ratchet, not a correctness failure.

Section numbering is the skill's own and is not the failure-log numbering; where a section names a record it says "recorded as" to mark the reference. A section's anchors are worked examples of a shape, never a roster of a failure class: a record can teach a section's shape without belonging to the class that section's records mostly carry.

## How to invoke

**Trigger scope.** Every commit that reaches a branch passes through this review, including fix commits made while answering a PR review (recorded as F117: a disposition fix commit made outside the commit surface introduced a test that could not go red, and no commit-time review ever read it). A commit this review did not see is unreviewed change, whatever else reviewed its branch.

Diff sources, in order of preference: a specific commit (`git show <sha>`), a branch against main (`git diff main...HEAD`), staged changes (`git diff --staged`), unstaged changes (`git diff`). If no diff is in scope, return "no diff in scope" and stop.

**Walk every section before emitting anything.** Produce a census first — for each section, MET or NOT-MET with one clause of justification. Visit every section; do not stop once there are enough findings. An unvisited section is indistinguishable in the output from one visited and found clean. G1's census entry is not complete until every verification in the diff has its executed-audit line (see G1). The census is working material; do not emit it unless asked.

For each section that fired, emit: the section tag (F1–F5, G1, C1, S1, R1, P1); the files or hunks that triggered it; the defect mechanism, stated concretely; and any follow-up. Do not emit sections that did not fire. **Brevity in the output, completeness in the walk.**

---

## F1 — API convention drift

Routers stay one service call (no storage, adapter or model construction in the handler body); `tests/sage/test_router_conformance.py` and the import-linter contracts gate the structure, so flag only a new router that lands allowlisted in `KNOWN_VIOLATIONS`, or a drained router whose `KNOWN_VIOLATIONS` entry and `ignore_imports` line were not dropped with it.

---

## F2 — Tests-as-chronicle

When the diff tightens a boundary validator and edits tests in the same change, name each test edit as a correction of a wrong assertion or a relaxation masking a semantic break, flag every widened fixture or weakened assertion, and require a negative test that the new validator rejects the previously permitted input (the pattern of commit `98c03f9`).

---

## F3 — Boundary-validation gap

**What this catches.** Input that is type-correct at a boundary but whose shape is unconstrained, lost, or reinterpreted there: a shape-bearing field with no typed alias; a normalization that erases a distinction downstream code branches on; a raw-input validator that trips on an unexpected type; an operator- or environment-supplied value read as a pattern where a literal was meant.

**Already gated deterministically.** Pydantic enforces type. Nothing enforces shape, preserved distinctions, or literal comparison.

**Convention reference.** The *CAS Typed-Alias Boundary Conventions* steering document (vault: cas, `doc_type=steering_document`) says where typed aliases must apply (`BaseModel` fields, FastAPI route params, FastMCP entry points), where they need not, and the three boundary patterns (Pattern 1 alias on a route-param annotation; Pattern 2 module-scope `TypeAdapter` + in-body `validate_python` at an MCP entry point; Pattern 3 `BaseModel` field annotation). Cite it when surfacing a finding.

**Review prompts.**
- For every field or parameter added or modified at a boundary surface (`sage/models/schemas.py`, `root_harness/`, `*/models.py`, `*/schemas.py`, routes under `sage/api/routers/` and `app/backend/`, MCP entry points in `sage/sage_api_tools.py`/`sage/app_tools.py`): if it carries a known shape (date, id, hash, version label, path, URL, UUID, vault id, heading), does it use an existing typed alias (`DocumentIdStr`, `EdgeIdStr`, `Sha256Str`, `DocumentDateStr`, `VaultIdStr`, `UserIdStr`, …) rather than a bare type? A new shape adds its alias in the same diff; an inline `Annotated[str, AfterValidator(...)]` is a smell. Dict-shaped payloads walk their shape-bearing keys in a `model_validator`.
- **Collapsing normalization.** Does the boundary normalize `{}`, `""` or `[]` to `None` (or otherwise merge two inputs) where downstream code branches on the difference (`is not None`, truthiness, key presence)? Check every such normalization against the branches it feeds, and against the other surfaces that accept the same payload, so one surface does not treat an empty value differently from the rest (recorded as F142).
- **Raw input in `mode="before"`.** A `mode="before"` validator sees the input before any type check. Does it hash, test membership, call string methods on, or index raw input without first guarding its type? A list where a string was expected then raises `TypeError`, which escapes as a 500 instead of a 4xx (recorded as F118).
- **Values reaching pattern consumers.** Does an operator-, config- or environment-supplied value reach `grep -E`, SQL `LIKE`, a glob, an unquoted shell expansion, or an `IFS` split? It must be escaped or compared as a literal (`grep -F -x`, quoted expansion, `LIKE … ESCAPE`, a newline-proof split). Otherwise metacharacters let a non-equal value match, and a gate built on it fails open (recorded as F86; F88 is the same shape at `LIKE`).

**What "correct" looks like.** From `sage/models/schemas.py`:

```python
class LinkRequest(BaseModel):
    source_id: DocumentIdStr
    target_id: DocumentIdStr | None = None
    edge_type: EdgeType
    retracted_edge_id: EdgeIdStr | None = None
    notes: str | None = None
```

Every shape-bearing field carries its alias; `notes` is bare `str` because free text has no shape contract. The presence/absence pattern is the discipline.

---

## F4 — Incomplete site set

**What this catches.** A change to something with more than one site — a shared rule, invariant, vocabulary, registration, raise path, function, or type — that lands at the sites the author had in mind and misses the rest. The early cases were sweeps across parallel directories (the `98c03f9` alias remediation missed `tests/app/`). Most later ones were not described as sweeps at all: a raise path with three callers fixed at one (recorded as F76), a widened status vocabulary missing five consumers (F73), the next method down with the same unescaped shape (F88), two of three writers locked (F127).

**Already gated deterministically.** Nothing general. Some rules carry their own conformance gates, but a gate bounds only the sites it enumerates, and a gate satisfied by a declaration with no reader is a C1 finding.

**Trigger.** Any change to a shared rule, invariant, vocabulary or registration, and any change to a function, raise path or type with more than one consumer — not only commits described as a remediation, sweep, or conformance pass.

**Review prompts.**
- **Derive the site set from its authoritative source, not from the diff or from memory, and cite the command that produced it** (the `rg` invocation, AST walk, catalog dump, or schema query) in the review output. A list without its command counts as not derived. Key the search to what a site *does*, not to the spelling the author saw: a census of `raise ValueError` misses the exceptions libraries raise (recorded as F70), and a grep limited to `*.py` misses a TypeScript copy (F79). Walk every class:
  1. **Callers** of a changed function or raise path, on every transport, including construction sites that raise the changed rule (F68, F76, F143).
  2. **Writers** of the guarded state: every path that can put the state the change constrains (F127, F149). For stored shapes, R1 carries the full walk.
  3. **Consumers** of a changed type or vocabulary, on the same surface and across transports: enumerations in docstrings, classifiers' fallthroughs, CLI filters, health counts, the BFF, TypeScript mirrors (F73, F120, F131).
  4. **Sibling branches** in the same file or class: the next method with the same shape, the other parameter reaching the same branch, the paired operation of a read/reclaim pair, the other axes of a multi-axis check (F82, F88, F95, F99, F119).
  5. **The other direction** of a channel: upload for download, write for read, request for response (F101 against F100).
  6. **Restatements** in other languages (TypeScript, shell, Bicep), runbook command blocks, and test-plan prose (F67, F79).
  7. **Parallel directories**: `tests/sage/` ↔ `tests/app/` ↔ `tests/root_harness/`; per-module trees under `sage/services/`, `sage/api/routers/`, `sage/source_adapters/`; `sage/models/` ↔ `root_harness/models/`; `domains/<domain>/`.
- When a gate or check is *widened*, the code it newly admits is a site too: follow the newly admitted input to where it is consumed (recorded as F102).
- For each site, the diff either covers it or the commit message or PR body declares it out of scope, with a rationale. A missed site judged unreachable or short-lived is still reported as a finding, with its reachability stated, so the author makes the scope call rather than the reviewer (recorded as F99, where a read tolerated a missing table and its paired reclaim did not). A completeness claim in the commit message ("every", "all paths", "none left") is also a C1 claim and is checked against the derived set.

**What "correct" looks like.** A diff that has enumerated every class above from a cited command and either covered each site or **declared it out of scope, with a rationale, in the commit message or PR body** — not in an inline comment. Per §P1 and the *CAS Code-Surface Discipline* steering document, deferral scaffolding belongs on the ephemeral surface; an inline comment is right only when it states a code branch's own narrow scope as a durable property (e.g., "this branch does not yet honor `expected_head_version`; see CAS-ADR-038").

---

## F5 — Validation bypass via missing dependency

A vault-scoped route mounted outside `sage/api/routers/` (a service module, middleware, startup hook, or a router built in a loop) must carry `Depends(get_vault_id)` on the handler or at router level; routes under `sage/api/routers/` are gated by `tests/sage/test_router_conformance.py`.

---

## G1 — Non-discriminating verification (gate integrity)

**What this catches.** A test, gate, detector, or probe that passes identically against the correct implementation and a defective one, so it cannot go red while the condition it exists to enforce is breached. Recorded cases include an assertion that a directive appeared in a prompt, true whether or not the model obeyed it (F44); a fake whose only affordance was an error that never cleared, so the transition the code branched on was inexpressible (F45); a setup block no assertion read (F49); a control that compared two calls for agreement rather than the order for totality (F93); a comparison over one of the two halves a rewrite touched (F104); fixtures that all paired a status with the same flag, so a status-keyed rival passed 5466 tests (F69); and a test that called a helper the operator's entry point never reached (F116).

**Already gated deterministically.** Nothing, and this is where that statement is strongest. A non-discriminating verification is green from birth, and coverage counts it as covered because it executes the line it fails to constrain.

**Scope.** Every artifact in the diff whose job is to go red: a test, a scheduled drift or conformance check, a CI assertion, a gate helper, an evaluation instrument that makes a go/no-go call (F61, F78). Where a prompt says "test", read "verification".

**Why this section is executed rather than read.** Reasoned review of this section was measured blind against its anchors and reached some but not others. Two attempts to reword its prompts to close the absent-input axis — the input none of the fixtures varies, which F50, F56, F60, F61 and F63 all turned on — changed nothing on either anchor or either model tried (the passes are recorded in this skill's `tooling_entry` in the cas vault). More wording is therefore not the remedy. Running the verification against a broken implementation is.

**Review prompts.**

- **Executed audit (required).** For **every** new or modified verification in the diff, make at least one mutation or deletion of the code under test — the line the verification exists to protect — run the verification, record which tests went red, and restore exactly. Deleting a setup block is also a mutation, and it is how inert setup is found (F49, F109). A verification that stays green under the mutation is a finding. **A reasoned "none found" counts as not audited**, and so does a verification that cannot be run here; name the missing precondition instead of clearing it. Emit one line per verification before writing any finding:

  > `<verification>` — mutated: `<what was mutated or deleted>`; went red: `<tests, or "none">`; half-read: `<components claimed / components read>`; absent input: `<each branched-on dimension → the fixture varying it, or "unvaried">`; real path: `<entry point and dependency representations exercised, or the helper/fake substituted>`

  The three trailing fields are the items the mutation alone does not reach:
  - **Half-read subject.** List every component the verification claims to hold, and mark which it actually reads. A gate that checks output names but not the `run:` block binding them (F103), a comparison over the authored half of a surface whose derived half also changes (F104), a parser that reads the first code of a comma-listed bullet (F154), a role asserted anywhere in a module where a declaration already satisfies it (F80), a shape gate rendered through one of two serializers (F121).
  - **Absent input.** Name every input dimension the code under test branches on — type and subtype, platform and environment (`TMPDIR`, OS), cardinality (one vs many), ordering and ties, presence vs absence, character sets, co-varying fields — and the fixture that varies each. A mutation audit is blind along any unvaried dimension; vary it (run under the other value when that is cheap) or record the gap. Recorded as F69, F97, F98, F106, F124 and F153; in F117 the tests derived the temp base instead of observing it, and only a run with the temp directory under `/tmp` separates the two.
  - **Real path.** The verification goes through the real entry point, not a helper the entry point never calls (F116), and through the real representation of each dependency: the serializer each transport actually uses (F121), the status type the transport actually emits (F114), the CLI's real defaults and filtering rather than a fake that ignores its arguments (F112), the framework's own behaviour where the code re-implements it (F139, F140), and an input the boundary would actually admit (F144). Some of these are production defects that no mutation surfaces, because the existing test is red against a mutation and green against the defect. The remedy is to substitute the real representation and rerun.
- **Named rivals are a claim to audit.** Where a test names the rivals it excludes (docstring, comment, commit message), add to its line: named rivals, verified excluded yes/no, and **an unnamed rival that would also pass**. Calibrated on F45: five reviewers walked the introducing commit; the two required to emit the unnamed-rival field were the two that caught it.
- **Input as a stand-in for outcome.** An assertion that a directive, flag, config key, or call argument is *present* is a removal guard, not evidence the behaviour occurred (F44). Where the real property is unverifiable in the suite, the diff names where it *is* verified or records the gap.
- **Transitions and pairs.** For a failure, retry, or degraded path, can the fixture express the transition the code branches on (a condition that clears between calls, two concurrent callers)? Where one observable is asserted, would the defect produce the same value, and is the discriminating evidence a pair? A guard that is never entered, or an assertion over an empty collection, passes vacuously (F113, F128).

**What "correct" looks like.** From the fix in commit `5d23e61`, on the F45 provider. The fake client first gained the affordances it lacked — a per-call error sequence, and a gate holding a lookup open — and only then could the assertion discriminate, by pairing two observables:

```python
assert len(recorder.retrieve_calls) == 1
assert len(recorder.count_calls) == 2
```

Recording the discovery attempt before making it spends one lookup, so the lookup count alone passes against the defect; resolving without single-flight checks both documents but pays a lookup per caller, so the count alone passes too. One lookup *and* two counts is reachable only by holding the second caller until the first has an answer to share. That test's `Anti-coincidental-pass:` paragraph is a starting point, never a verdict; the pairing is checkable against the code, and the executed audit is what checks it.

---

## C1 — Claims against code

**What this catches.** A behavioural claim the diff adds or carries that the code does not honour, or that no test would turn red on. Claims live in docstrings, comments, advisory and error text, runbook lines, commit-message statements, and ticket premises the change builds on. The signature case is a field or declaration added only to satisfy a parity or conformance gate, with no reader: the gate goes green on a claim the code never honours (recorded as F120, where a BFF `dry_run` field was declared for the parity gate and never forwarded, so a dry run ingested). Other recorded cases: a teardown docstring promising confirmation the shared core skips for one vault id (F72); an advisory whose text holds for one of the five conditions that trigger it (F75); a ticket premise that ingestion never guarantees, verified by a test that wrote the premise into its own fixture (F81); a fusion docstring promising the matched passage's excerpt where the code keeps the first row (F87); a docstring stating the opposite direction from the arithmetic below it (F90); "the encoding the runtime delivers" measured with a different encoding (F91); a pagination helper assuming an order no query makes total (F92); "the default branch" where the argument defaults to `HEAD` (F96).

**Already gated deterministically.** Nothing. Docstrings are not executed, tests assert what the author believed, and parity gates check declarations rather than readers.

**Review prompts.**
- List every behavioural claim the diff adds, and every existing claim it carries into new reach (a docstring above changed code, a premise the change depends on). For each, name the code that makes it true **and** the test that would go red if it were false. A claim missing either is a finding.
- **Premises are verified, not inherited.** A premise taken from a ticket, a design note, or an earlier commit is checked in code. A test that supplies the premise in its own fixture does not verify it (F81).
- **Directional and quantified claims** ("never", "at most", "under-recommends", "every", "the default branch") are checked against the code's actual direction and default (F90, F96), and a quantifier by enumerating the set.
- **Claims about an external representation** ("the encoding the transport delivers", "what the CLI returns") are checked against that representation, never against a restatement of it — including a test helper that restates the same expression (F91).
- **Conditional text** (advisories, warnings, hints) is checked against every condition that triggers it, not only the one it was written for (F75).
- **Ordering and stability assumptions** name the total order that makes them true (F92).
- **A declaration added to satisfy a gate** — a model field, spec property, flag, or registration entry — names its reader. No reader means the claim the declaration makes is false (F120).

**What "correct" looks like.** Each claim in the diff can be pointed at twice: once at the line that makes it true, once at the test that fails if that line changes. A claim that can only be pointed at once is either unverified or unimplemented, and the review says which.

---

## S1 — Surface and contract parity

**What this catches.** The same input answered differently by the REST API, the MCP surfaces, and the BFF, or a real emitted envelope or accepted request that disagrees with the published contract. Recorded cases: a shared helper that stripped FastAPI location segments from plain Pydantic errors too, so an MCP argument named `path` moved from 400 to 422 (F135); a validation failure escaping REST as a plain-text 500 while MCP wrapped it (F137); a model made nullable while the spec kept a bare `$ref` (F138); per-tool MCP error schemas that reject envelopes those tools emit (F145); a discriminator whose mapping was dropped, so wire codes could not resolve (F146); a batch tool whose per-item validation lost the cross-item note the HTTP refusal carries (F150); a schema rewrite that lost a closed sort vocabulary the published catalog had (F152); condensed descriptions that dropped the only MCP-visible meaning of success-qualifying result fields (F155, which reached main).

**Already gated deterministically.** The parity and conformance tests under `tests/sage/` (for example `test_openapi_conformance.py`, `test_validation_envelope_parity.py`, `test_mcp_docstring_disclosure_parity.py`, `test_mcp_success_qualifier_disclosure.py`, `test_mcp_catalog_snapshot.py`) hold the cases they enumerate. They compare the surfaces with each other, so an edit made to both sides at once passes them (F155), and they cover only the inputs and envelopes they list.

**Review prompts.**
- For every changed handler, service path, or shared helper reachable from more than one transport, drive the same valid, invalid, and failing input through each (REST, both MCP surfaces, the BFF where it forwards) and compare status, error code, and envelope shape (F135, F137, F150).
- **Contract in both directions.** Every envelope the changed code can emit validates against the published schema, including discriminator mappings (F145, F146), and the published schema accepts exactly what the server accepts — nullability, required fields, closed vocabularies (F138).
- **Rewritten descriptions or schemas.** When tool descriptions, parameter prose, or input schemas are rewritten, condensed, or moved, render the published catalog (`python -m scripts.dump_mcp_catalog`) at HEAD and at `origin/main` and diff them. Account for every token lost: closed vocabularies (F152), and the meaning of any result field that qualifies a success, because MCP publishes no output schema and the description is the only place a caller learns it (F155).

**What "correct" looks like.** A cross-transport change whose review names the inputs driven through each surface and the diff of the rendered catalog, and explains each divergence as intended or fixes it.

---

## R1 — Stored-data writers and readers

**What this catches.** A change to a stored shape, marker, or backfill that is correct for the writer or reader in view and wrong for the others. Recorded cases: a migration that rewrote stored heading paths while the ingestion writer kept emitting the old shape, so re-ingest undid the migration (F84, high severity); filter columns copied onto a new document-level surface whose writer was never updated, so an archived document still matched under an `active` filter (F85, high severity); a backfill whose candidate test stayed true for documents it had examined and left unchanged, so every run re-read them (F129); a backfill that assumed a structure an older adapter version had not written (F130); `""` reserved as a marker when adapters already emitted `""` for another meaning (F132, which reached main).

**Already gated deterministically.** Migration and backfill tests exercise the change on the fixtures they build. Nothing checks that every writer emits the new shape, that every reader accepts both shapes while both are stored, or that a marker is new.

**Review prompts.**
- As in F4, both sets below are produced by a search whose command the review cites; a set reasoned from the diff counts as not derived.
- For any change to a stored shape (columns, JSON keys, path formats, lifecycle stamps, index surfaces), list **every writer** — ingest, re-ingest, metadata update, lifecycle transition, migration, backfill, and older adapter versions whose output is still stored (F130) — and **every reader** (both search arms, projections, exports, maintenance reports). Each writer emits the new shape (F84, F85); each reader accepts every shape still present in stores.
- **Backfill and migration candidates.** The predicate that selects candidates goes false once a document has been processed, including a document examined with nothing to change; a second run is a no-op (F129).
- **Sentinels and reserved values.** Before a value is given a new meaning, search the *producers* — every adapter, writer, and default that can already emit that value, for any reason — not only the readers that will interpret it. Cite the search, and name each producer found and the meaning it already gives the value. A membership test cannot tell two meanings apart (F132, where adapters already emitted `""` for an empty-text heading).

**What "correct" looks like.** The diff or its message names the writer and reader sets and the command that found them. The candidate predicate is shown to exclude processed documents, and each new sentinel is shown to be unproducible by existing writers.

---

## P1 — Public-repo posture drift (release-readiness gate)

**What this catches.** Newly added or modified code, docstrings, `#` comments, or file and directory *names* that would (a) leak the internal SDLC scaffolding (specific ticket ids, checklists, work plans, dispatcher prompts, decision sheets, cohorts, batches, PR or issue numbers), (b) pin the repo to a particular non-generic use case (e.g., PIM, theology, patent prosecution), or (c) expose the author's personal identity (`/Users/clifguy/...`, GitHub usernames, internal URLs). The repo is intended to be encountered by readers who have no knowledge of the production process that produced CAS/SAGE; the durable code surface must read accordingly.

**Convention reference.** The *CAS Code-Surface Discipline* steering document (vault: cas, `doc_type=steering_document`, tag `code-surface-discipline`) is the source-of-truth specification for what may and may not appear in durable surfaces, including the SAGE-as-ticketing and `domains/` exceptions and worked right-vs-wrong examples. Cite it when surfacing a finding so the rationale is traceable rather than re-derived.

**Already gated deterministically.** `tests/test_public_posture.py` is the authoritative deterministic gate and runs in CI. It catches hyphenated `T-NNNN` refs in `.py` docstrings and `#` comments (T2 — via AST + tokenize, so the same token inside an *unpublished* string literal is correctly NOT flagged), ticket ids in *published* strings (T15 — a string literal written at a published sink: a `description`/`help`/`epilog` keyword argument, a raise message, or a logging message in non-test `.py`, plus the whole text of every tracked `.yaml`/`.yml`/`.json` file), use-case terms outside `domains/` across `.py`/`.yaml`/`.yml`/`.json`/`.md` (T1), personal filesystem paths (T3), SDLC-scaffolding terms in `.py` docstrings/comments, and ticket ids or use-case terms in tracked file and directory *names* (T12 — a separate, wider ticket-id pattern, because a name carries the id unhyphenated as `t0074` or `t_0037`), and ticket ids in Python *names* — the names of functions, async functions, and classes at any nesting depth (T13), and the names bound by assignment targets and function parameters at any nesting depth (T14), both sharing T12's pattern for the same reason. It excludes the `domains/` and `.claude/` subtrees by design. Note what T13, T14, and T15 do **not** reach: an attribute or subscript target (`self.t0157_field`, `d['t0157']`), a `for` / `with ... as` / `except ... as` / walrus / comprehension target, an import alias, a ticket id inside an unpublished string literal (a fixture value, a dict payload, a non-sink keyword argument — T2 and T15 both exclude these by design, to distinguish a published surface from a fixture value), and a string that reaches a published sink *indirectly* through a variable, constant, or concatenation (T15 is literal-at-sink only). Those forms are this section's only enforcement. This section's residual value is the heuristic reach *beyond* that scanner: prose that leaks intent ("added for the X flow", "fix from issue #123"), PR or issue numbers, internal URLs, and use-case leakage in surfaces the scanner does not parse. **Anything `test_public_posture` would fail, flag here too** — the pre-commit heuristic and the CI gate must never disagree. For any mechanically-checkable case, prefer running `.venv/bin/pytest tests/test_public_posture.py` over eyeballing the diff.

**Scope note.** This is a forward-only ratchet on NEW changes. Legacy violations (the existing ~1,300 `T-NNNN` references and the broader use-case-specific surface area) are addressed by a separate cleanup pass and are not the concern of this section — the section fires when a diff *adds* a violation, not when an unrelated change touches a file that already contains one. Removing a legacy violation is always welcome but never required by this gate unless the diff also modifies the same docstring/comment. For names the same rule reads: the section fires when a diff *adds* a file or directory with a violating name, or *renames* one into a violating name — not when it edits the contents of a file that already has one.

**Review prompts.**

- For every docstring or `#` comment ADDED or MODIFIED in the diff, does it reference:
  - A ticket id matching `T-NNNN` (e.g., `T-0023`, `T-0195`)? Flag.
  - A non-ADR internal-scaffolding artifact: "checklist", "work plan", "decision sheet", "dispatcher prompt", "subagent contract", "cohort", "batch", a PR or issue number? Flag.
  - The motivating ticket, flow, or caller ("added for the X flow", "fix from issue #123", "used by Y")? Flag — that belongs in the commit message and PR body, not in the durable code surface.
- For every code element (identifier, string literal, module name, file path) ADDED or MODIFIED in the diff outside the `domains/` subtree, does it mention `PIM`, `pim_health`, `pim-health`, `theology`, `theological`, `patent`, `prosecution`, or other use-case-specific terminology? Flag.
- For every identifier ADDED or RENAMED in the diff — a function, class, fixture, constant, or parameter name — does it carry a ticket id? Flag. An identifier cannot hold the canonical hyphenated spacing (a hyphen is not legal in a Python name), so checking for `T-0023` finds nothing here; the forms that actually occur are `t0157`, `t_0148`, and `T0452`, as in `test_t0157_target_edges`, `_T0246_READ_TOOLS` (since renamed `_DOC_ID_ALIAS_READ_TOOLS`), and `t0076_retrieval_service`. A name that says what the code *asserts* or *holds* is the fix — `test_rejects_both_document_id_and_doc_id_when_equal`, not `test_t0246_rejects_both_equal`. T13 and T14 together gate definitions and bindings deterministically, and T15 gates published string literals (documentation keywords, raise and logging messages, tracked YAML/JSON text), so this prompt's residual reach is the forms none of the AST walks cover: attribute and subscript targets, loop and context-manager targets, import aliases, ids inside unpublished string literals, and strings that reach a published sink indirectly through a variable.
- For every file or directory ADDED or RENAMED in the diff, does any component of its path carry a ticket id or use-case term? Flag. Note that a name almost never carries the canonical spacing: check for `t0074`, `t_0037`, and `T0452` as well as `T-0452`. A name that describes what the file *does* is the fix — `probe_tag_filter_latency.py`, not `t0078_load_probe.py`.
- For every docstring, comment, or test fixture ADDED or MODIFIED, does it hardcode `/Users/clifguy/` or another personally identifying path, URL, or username? Flag.
- Acceptable references that MUST NOT be flagged:
  - `CAS-ADR-NNN` — ADRs are durable architectural citations and the only sanctioned non-code rationale anchor.
  - The `test` vault — used by the SAGE test suite; architecturally meaningful, not use-case-specific.
  - SAGE-as-ticketing patterns: `doc_type=ticket`, `tier3_metadata` ticket fields, and similar. These describe SAGE's expressly-supported architectural use (per CAS-ADR-028) and are NOT use-case contamination. Distinguish "the architectural pattern" (OK) from "a specific ticket id" (not OK).
  - The `domains/<domain>/` subtree — a domain example may carry its own domain-specific naming; flag only when changes outside `domains/` leak the same vocabulary.

**What "correct" looks like.**

- WRONG: `# T-0023: enforces the EdgeIdStr alias to prevent SQL-lookup hazard.`
- RIGHT: `# Enforces the EdgeIdStr alias to prevent SQL-lookup hazard; see CAS-ADR-019.`
- WRONG: `"""Process PIM document codes (PV##, NP##-yy) from the inbox."""`
- RIGHT: `"""Process document codes from the configured inbox per the active vault's metadata_extraction config."""`
- WRONG: `# Per checklist item 4 in the Q2 cleanup workplan, skip the legacy index.`
- RIGHT: `# Skip the legacy index — see CAS-ADR-NNN for the migration rationale.`
- WRONG: `scripts/t0074_load_probe.py`
- RIGHT: `scripts/probe_catalog_index_latency.py`

The rule is: durable surfaces (code, docstrings, comments, and the names of the files and directories that hold them) describe the pattern and cite ADRs only; ephemeral surfaces (commit messages, PR bodies) carry ticket and process scaffolding.

---

## Out of scope

- Style, formatting, naming conventions. Ruff and mypy own this surface. (Public-posture discipline IS in scope, in file and directory names as well as in contents; see P1. The line is that P1 asks whether a name *leaks* something, not whether it is well-chosen.)
- Generic security review. The `security-review` skill (separate, Anthropic-shipped) covers this.
- Test coverage measurement, performance regression, or dependency-policy review. These are Tier 1 surfaces from the SDLC survey and have their own tooling lanes.
- Architectural-decision review. ADRs in the cas vault (`doc_type=adr`) are the substrate for that.

The skill is tuned to the documented CAS failure modes. A pattern earns its place here when a failure record shows the commit-time review should have caught it: the defect was visible from the commit and its repository, and a review walking this file did not find it. Such a pattern is admitted. Its cost is controlled by consolidation, not refusal: widen an existing section before adding one, collapse a section once a deterministic gate owns its structural case, and keep every section's prompts executable rather than advisory. Prefer the framing that describes why a defect *survived review* over the one that describes how it was *written* — the first is what a review gate can act on.

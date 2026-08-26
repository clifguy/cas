---
name: cas-code-review
description: This skill should be used when the user asks to review a CAS commit, branch, or diff for the documented failure modes (F1-F5), the gate-integrity check (G1), and the public-posture gate (P1) catalogued in the skill's own section list. Trigger phrases include "cas code review", "review for CAS failure modes", "audit cas changes", "check this commit for cas drift", "run cas-code-review", and similar. The skill is tuned to the CAS repository specifically; do not invoke it on unrelated codebases.
version: 0.3.0
---

# cas-code-review

A repo-resident review pass keyed to the five failure modes documented in the AI-First SDLC Tooling Survey §3 (vault: cas, doc_type=reference_document, title "AI-First SDLC Tooling Survey"), plus the gate-integrity and public-posture sections that later failure records earned.

## Portability notes (forward-looking, not architectural)

This skill is currently single-tier: every section lives in this file. If a second repo someday needs its own code-review skill, the natural extraction split is:

- **Likely portable to a future universal layer.** F2 (tests-as-chronicle), F4 (remediation-pass scope gap), and G1 (non-discriminating assertion) are discipline failures whose abstract form should generalize to any tech stack. The CAS-specific elaborations they carry today (test-directory enumeration, the `_id(name)` helper reference, the provider fake) are anchoring examples, not the substance. G1 is the most portable of the three — it names a property of assertions, and mentions no framework at all.
- **CAS-specific in form.** F1 (service-as-load-bearer router pattern), F3 (typed-alias convention with `DocumentIdStr` and friends), and F5 (`Depends(get_vault_id)` pattern) embed stack details (FastAPI, Pydantic v2, the SAGE multi-vault architecture) that do not generalize as written.

Generalization is a hypothesis until observed in a second repo. Do not extract universal sections from this file pre-emptively. When repo #2 needs its own skill, that's the trigger to author the universal layer with two data points and refactor this file to elaborate-on rather than restate.

## Section structure

Each section below is structured the same way:

1. **What this catches.** One-sentence framing.
2. **Already gated deterministically.** What CI catches without the skill — the skill must not duplicate this work.
3. **Review prompts.** Questions to walk through against the diff under review.
4. **What "correct" looks like.** A short reference snippet from the canonical pattern.

Sections labeled `F` are code-correctness failure modes anchored to the *AI-First SDLC Tooling Survey*; they ask whether the code under review is wrong. Sections labeled `G` are gate-integrity checks; they ask whether the *gate* under review can fail — that is, why a defect of this shape would survive review rather than how it would be written. Sections labeled `P` are publicness-readiness gates for the public-repo posture; they are not code-correctness failures but release-discipline ratchets.

Section numbering is the skill's own and is not the failure-log numbering. Several sections are anchored to failure records that carry a different number (F4's section cites records F4 and F6; G1's cites records F44 and F45). Where a section names a record, it says "recorded as" to mark the reference.

The skill adds value in two places: catching the residue that deterministic gates cannot reach (F2 tests-as-chronicle, F4 remediation-pass scope gap, G1 non-discriminating assertion, P1 publicness drift), and giving early feedback before CI runs.

## How to invoke

The reviewer (Claude) reads the diff under review and walks every section. Diff sources, in order of preference:

- A specific commit: `git show <sha>` (or `git show <sha> -- <pathspec>` to scope).
- A branch's changes against main: `git diff main...HEAD`.
- Staged changes: `git diff --staged`.
- Unstaged changes: `git diff`.

If no diff is in scope, the skill returns "no diff in scope" and stops.

**Walk every section before emitting anything.** Produce a census first — for each section in this file, MET or NOT-MET, with one clause of justification. Visit every section: do not skip one because it looks irrelevant to the diff, and do not stop walking once you have enough findings to report. A section that is never reached fires on nothing, and in the emitted output an unvisited section is indistinguishable from one that was visited and found clean.

The census is working material, not output. Do not emit it unless asked.

Then, for each section that fired, emit:
- The section tag (F1-F5, G1, P1).
- The specific files or hunks that triggered the prompt.
- A short note on whether the change pulls toward or away from the canonical pattern.
- Any follow-up the reviewer should consider.

Do not emit sections that did not fire. **Brevity in the output, completeness in the walk.**

---

## F1 — API convention drift (residue only)

**What this catches.** Drift away from the service-as-load-bearer router pattern: routers that grow business logic, validation, file I/O, or storage calls in their function bodies instead of delegating to a service.

**Already gated deterministically.** `tests/sage/test_router_conformance.py` walks `sage/api/routers/` and asserts the canonical shape on every router; the four currently-drifted routers (`documents`, `staging_edges`, `pending_metadata`, `vaults`) are pinned in `KNOWN_VIOLATIONS` and the test fails on stale-allowlist entries. `pyproject.toml` import-linter contracts "Services do not import from routers" and "Routers do not import from storage directly" cover the layered case. The structural pattern of F1 is fully gated; this skill section covers residue only.

**Review prompts.**
- For any change to a `*.py` file under `sage/api/routers/`: does the diff add new logic to a router function body, or does it pull toward the canonical service-call shape?
- When a refactor lands for one of the four allowlisted routers (`documents`, `staging_edges`, `pending_metadata`, `vaults`), is `KNOWN_VIOLATIONS` in `tests/sage/test_router_conformance.py` updated to drop the drained entry, AND is the corresponding `ignore_imports` line in the `pyproject.toml` import-linter "Routers do not import from storage directly" contract dropped?
- For any new router file added to `sage/api/routers/`: does it ship in canonical form from day one, or does it land allowlisted? (Allowlisted-on-arrival is a smell; flag it.)
- For any router edit, does the diff introduce direct calls to storage, adapters, or Pydantic-model construction inside the route handler? These belong in services.

**What "correct" looks like.** A canonical thin endpoint from `sage/api/routers/graph_ops.py`:

```python
@router.post("/edges", response_model=Edge, status_code=201)
async def link(
    request: LinkRequest,
    vault_id: str = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> Edge:
    return await service.link(request)
```

The handler body is one line: a service call. Anything richer than that is a load-bearing-router smell.

---

## F2 — Tests-as-chronicle (primary skill territory)

**What this catches.** The diagnostic pattern from commit `98c03f9`: when validators are added or tightened on a request or storage model, tests that were green before only stay green if their fixtures/assertions are edited in the same diff. The risk is that the test edit is not a correction of an old, incorrect assertion but a quiet relaxation that masks a real semantic break.

**Already gated deterministically.** Nothing. Pydantic catches type drift at runtime; no tool catches "the test had to change because the code became more correct."

**Review prompts.**
- Does the diff add or tighten validators at any boundary surface — BaseModel fields in `sage/models/schemas.py` (or any `models.py`/`schemas.py` under `root_harness/`), FastAPI route parameters in `sage/api/routers/`, or FastMCP tool entry points in `sage/sage_api_tools.py`/`sage/app_tools.py`/`sage/mcp_server.py`? The *CAS Typed-Alias Boundary Conventions* steering document names all three site classes; F2 covers any of them. Examples: a new `AfterValidator`, a tightened regex, a new typed alias applied to an existing field, a new `model_validator`, a new module-scope `TypeAdapter` + in-body `validate_python` at an MCP entry point.
- Does the same diff edit fixtures or assertions in `tests/`? List every test file the diff touches alongside the model change.
- For each touched test: was the test asserting incorrect-but-permitted behaviour before the change (legitimate correction), or was it asserting correct behaviour that is now disallowed (semantic break being papered over)?
- Were any test inputs widened or made more permissive (a fixture changed from a strict to a loose value, an `assert ==` weakened to an `in` check, a parameterized case removed)? Flag each occurrence; widening tests in service of a "tightening" change is a smell.
- If the diff touches BOTH model validators AND tests, is at least one test added that asserts the new validator rejects the previously-permitted bad input? Without that, the change is asymmetric: production is stricter, but the regression surface is unobserved.

**What "correct" looks like.** When a typed alias like `DocumentIdStr` is applied to a field, a paired test asserts that an off-shape value (e.g., `"not-a-doc-id"`) raises `ValidationError`. The alias plus the negative test together close the loop.

---

## F3 — Boundary-validation gap (primary skill territory)

**What this catches.** A new shape-bearing field added to a request or storage model that has the right type but no shape constraint. Type-correct, range-incorrect.

**Already gated deterministically.** Pydantic enforces type. Nothing enforces that every shape-bearing field uses an explicit shape constraint; that lives in the convention.

**Convention reference.** The *CAS Typed-Alias Boundary Conventions* steering document (vault: cas, `doc_type=steering_document`) codifies where typed aliases must apply (`BaseModel` fields, FastAPI route params, FastMCP entry points), where they need not (service-internal and storage-internal signatures, which receive already-validated input from the boundary), the three boundary patterns for non-Pydantic-body entry points, and the discipline for adding new aliases. The prompts below derive from that convention; cite the steering doc when surfacing a finding so the rationale is traceable rather than re-derived.

**Review prompts.**
- Does the diff add or modify any field or parameter at a boundary surface — BaseModel fields in `sage/models/schemas.py`/`root_harness/`/`*/models.py`/`*/schemas.py`, FastAPI route parameters under `sage/api/routers/` (and `app/backend/`), or FastMCP tool entry-point parameters in `sage/sage_api_tools.py`/`sage/app_tools.py`/`sage/mcp_server.py`? The boundary patterns are Pattern 1 (alias on FastAPI route-param annotation), Pattern 2 (module-scope `TypeAdapter` + in-body `validate_python` at MCP entry points), and Pattern 3 (BaseModel field annotation) per the *CAS Typed-Alias Boundary Conventions* steering document.
- For each new or modified field/parameter, does it carry a known shape (a date, an id, a hash, a version label, a path, a URL, a UUID, a vault id, a section heading)?
- If yes, does the field/parameter use a typed alias from `sage/models/schemas.py` — `DocumentIdStr`, `EdgeIdStr`, `Sha256Str`, `DocumentDateStr`, `VaultIdStr`, `UserIdStr`, `FunctionIdStr`, or another existing alias — rather than the bare type (`str`, `str | None`)?
- If a new shape is being expressed and no alias exists, is one being added in the same diff? A field with a bespoke `Annotated[str, AfterValidator(...)]` inline is a smell; the alias exists so the validator is reusable.
- For dict-shaped payloads (like `IngestRequest.metadata`), does the diff carry a `model_validator` that walks shape-bearing keys (this is the pattern `IngestRequest._validate_metadata_dates` follows)?

**What "correct" looks like.** From `sage/models/schemas.py:267`:

```python
class LinkRequest(BaseModel):
    source_id: DocumentIdStr
    target_id: DocumentIdStr | None = None
    edge_type: EdgeType
    source_valid_from_version: DocumentIdStr | None = None
    target_valid_from_version: DocumentIdStr | None = None
    retracted_edge_id: EdgeIdStr | None = None
    notes: str | None = None
    rationale: str | None = None
```

Every shape-bearing field carries its alias. `notes` and `rationale` are bare `str` because they are free-text — they have no shape contract to enforce. The presence/absence pattern is the discipline.

---

## F4 — Remediation-pass scope gap (primary skill territory)

**What this catches.** A discipline failure: a remediation that swept one directory tree believes itself complete because the code in scope is fixed, while parallel directory trees containing analogous code are silently missed. Two documented cases: the May 5 typed-alias remediation (`98c03f9`, recorded as F4) rewrote eleven `tests/sage/` fixtures but missed twelve in `tests/app/`; the T-0023 alias introduction (`48b85f6`, recorded as F6) applied `EdgeIdStr` at one site but missed eight parallel `edge_id` sites that shared the same SQL-lookup hazard. The first instance was caught only after the test suite broke; the second was caught by this skill firing pre-fix during the T-0023 review pass.

**Already gated deterministically.** Nothing. Discipline failures are not structural.

**Review prompts.**
- Does the commit message, ticket reference, or PR body describe the change as a remediation, refactor-of-pattern, sweep, conformance pass, alignment, or normalization?
- If yes, enumerate every directory the diff touched.
- Now enumerate the *parallel* directories at the same level of the tree:
  - `tests/sage/` ↔ `tests/app/` ↔ `tests/root_harness/`
  - `sage/services/` (per-service modules) — did the change touch some services but not others that share the pattern?
  - `sage/api/routers/` (per-router modules) — same question.
  - `sage/models/` ↔ `root_harness/models/`
  - `sage/source_adapters/` (per-adapter modules)
  - `domains/<domain>/agents/` and `domains/<domain>/workflows/` (per-domain configs)
- For each parallel directory, does it contain analogous code that needs the same remediation? Search the parallel directory for the same pattern the remediation targeted (the old shape, not the new one).
- If analogous code exists and is not in the diff, surface every missing site for the commit message or PR body. Be specific: name the files, name the lines, name the pattern.

**What "correct" looks like.** A remediation diff that, before merging, has explicitly enumerated every parallel directory and either swept it or **declared it out of scope, with a rationale, in the commit message or PR body** — not in an inline code comment. Per §P1 / the *CAS Code-Surface Discipline* steering document, ticket and scope scaffolding (including "deferred to a follow-up, and here is why") belongs on the ephemeral surface, not the durable one. An inline code comment is the right home only when it states a *code branch's own* narrow scope as a durable property of the code (e.g., "this branch does not yet honor `expected_head_version`; see CAS-ADR-038"), never to enumerate deferred parallel sites. F4 is a discipline failure; the fix is to make the enumeration visible — in the commit message for deferred sites, in code comments only for in-code scope limits.

---

## F5 — Validation-bypass via missing dependency (narrow residue)

**What this catches.** A vault-scoped HTTP route mounted at a path containing `{vault_id}` that omits `Depends(get_vault_id)`, allowing requests with unknown vault ids to bypass the registry validation that every other vault-scoped endpoint performs.

**Already gated deterministically.** `tests/sage/test_router_conformance.py` asserts `Depends(get_vault_id)` on every vault-scoped route discovered under `sage/api/routers/`. The structural case is fully gated. This section covers the long-tail residue.

**Review prompts.**
- Does the diff mount any new HTTP route OUTSIDE `sage/api/routers/`? Examples: a route declared inside a service module, registered via middleware, declared in a startup hook, mounted from the MCP server side, or registered dynamically from a config file.
- If the route's mount path contains `{vault_id}`, does the handler signature include `vault_id: str = Depends(get_vault_id)`?
- For dynamic-mount cases (e.g., a router constructed in a loop), does the construction code apply `dependencies=[Depends(get_vault_id)]` at the router or app level, or include it on each generated handler?

**What "correct" looks like.** From `sage/api/dependencies.py:33`:

```python
async def get_vault_id(
    vault_id: str = Path(..., description="Vault identifier"),
    request: Request = None,
) -> str:
    """Validate vault_id against loaded vault registry."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    if vault_id not in registry:
        raise VaultNotFoundError(vault_id)
    return vault_id
```

Every vault-scoped endpoint takes `vault_id: str = Depends(get_vault_id)` as one of its parameters; the dependency executes the registry check before the handler body runs. Routes that skip it are silently permissive.

---

## G1 — Non-discriminating assertion (gate integrity)

**What this catches.** A test or gate that passes identically against the correct implementation and the defective one, so it cannot go red while the condition it exists to enforce is breached. Two documented cases, from opposite directions. In the first (recorded as F44), a test asserted that a directive string appeared in a constructed model prompt — true whether or not the model obeyed it, because model behaviour was never in the assertion's causal path; the constraint was breached for eleven weeks with the gate green throughout. In the second (recorded as F45), a test asserted that a failed capability lookup degrades to an unchecked call and warns once — true of both the correct implementation and the defective one, because the fake client's only affordance was an error that never cleared, so "a condition that clears between the first call and the second" could not be expressed at the fixture at all.

**Already gated deterministically.** Nothing, and this is the section where that statement is strongest. A non-discriminating assertion is green from birth: it never breaks, so no runner, linter, or type checker distinguishes it from a discriminating one. Coverage is actively misleading here rather than merely silent — both cases above *executed* the production line they targeted, so line and branch coverage counted them as covered.

**Review prompts.**

- For each assertion ADDED or MODIFIED in the diff: name the defective implementation it is meant to exclude, and state whether it would go red against that implementation. If no such implementation can be named, the assertion is a coverage artefact rather than a gate, and the diff should say which it is.
- **Where the test already names the rivals it excludes — in a docstring, a comment, or the commit message — treat that inventory as a claim to audit, not as evidence.** A confident, accurate list of excluded rivals reads as diligence, and is the most effective place for one more to hide.

  This prompt is procedural, not advisory, because stating it is demonstrably not enough. For **every** such test in the diff, emit one line before writing any finding, in this shape:

  > `<test name>` — named rivals: `<list>`; verified excluded: `<yes/no>`; **unnamed rival that would also pass: `<the rival, or "none found">`**

  The third field is the one that does the work, and it must be filled in per test rather than answered once for the diff. When the answer is "none found", say so explicitly — an unvisited inventory and an audited one look identical in the output otherwise, which is this section's own failure mode applied to this section.

  Calibrated against the F45 case. Five independent reviewers walked the introducing commit; the test at issue carried a note naming two rivals, and genuinely excluded both. Three passed it — one never reached it, two read the note and accepted it. The two that caught it were exactly the two required to emit the unnamed-rival field. The defect was a third rival the note did not name: one the fixture could not express. Wording this as advice rather than as a required emission was measured and did not work.
- Does the assertion target an **input** as a stand-in for an **outcome** — that a directive, flag, config key, marker, or call argument is *present*, rather than that the behaviour it governs *occurred*? Flag it. Keeping such an assertion as a removal-guard is legitimate and often correct; reading it as evidence that the constraint holds is not. Where the real property is unverifiable in the suite (a model's behaviour, a live-resource fact), the diff must name where it *is* verified, or record the gap — deferring it to a close-out step outside the deliverables is how the eleven weeks happened.
- For any test exercising a failure, retry, or degraded path: can the fixture express the **transition** the production code branches on — a condition that clears between calls, a second call returning something different from the first, two callers arriving concurrently? A fake whose behaviour is constant cannot separate "retried and recovered" from "never retried", and a fake whose parameter defaults match the value under assertion cannot separate "the caller passed it" from "the caller stopped passing it".
- Where a count, a flag, or a single observable is asserted, would the defective implementation produce the same value? Some defects are invisible to any one observable and separate only under a pair. If the diff asserts one where the discriminating evidence is a pair, say so.

**What "correct" looks like.** From the fix in commit `5d23e61`, on the concurrency case of the same provider the F45 record describes. The fake client first gained the two affordances it had lacked — a per-call error sequence, and a gate that holds a lookup open — which are precisely what made the defect inexpressible before. Only then could the assertion discriminate, and it does so by pairing two observables:

```python
assert len(recorder.retrieve_calls) == 1
assert len(recorder.count_calls) == 2
```

The pairing is the whole of it. Recording the discovery attempt *before* making it also spends exactly one lookup, so the lookup count alone passes against the defect; resolving without single-flight checks both documents but pays a lookup per caller, so the count alone passes too. One lookup *and* two counts is reachable only by holding the second caller until the first has an answer to share. Either number alone is non-discriminating; the pair is the gate.

That test also carries an explicit `Anti-coincidental-pass:` paragraph in its docstring, naming each rival implementation and what it would produce. Read such a paragraph as a starting point, never as a verdict. The F45 case had one too, and it was accurate: it named two rivals — propagating the failure, and re-warning per document — and the test did exclude both. It simply did not name the rival that mattered, the one the fixture could not express. What earns trust here is the *pairing* of observables above, which is checkable against the code; the paragraph is a claim about coverage, and coverage claims are the thing this section exists to distrust.

---

## P1 — Public-repo posture drift (release-readiness gate)

**What this catches.** Newly added or modified code, docstrings, or `#` comments that would (a) leak the internal SDLC scaffolding (specific ticket ids, checklists, work plans, dispatcher prompts, decision sheets, cohorts, batches, PR or issue numbers), (b) pin the repo to a particular non-generic use case (e.g., PIM, theology, patent prosecution), or (c) expose the author's personal identity (`/Users/clifguy/...`, GitHub usernames, internal URLs). The repo is intended to be encountered by readers who have no knowledge of the production process that produced CAS/SAGE; the durable code surface must read accordingly.

**Convention reference.** The *CAS Code-Surface Discipline* steering document (vault: cas, `doc_type=steering_document`, tag `code-surface-discipline`) is the source-of-truth specification for what may and may not appear in durable surfaces, including the SAGE-as-ticketing and `domains/` exceptions and worked right-vs-wrong examples. Cite it when surfacing a finding so the rationale is traceable rather than re-derived.

**Already gated deterministically.** `tests/test_public_posture.py` is the authoritative deterministic gate and runs in CI. It catches hyphenated `T-NNNN` refs in `.py` docstrings and `#` comments (T2 — via AST + tokenize, so the same token inside a string literal is correctly NOT flagged), use-case terms outside `domains/` across `.py`/`.yaml`/`.yml`/`.json`/`.md` (T1), personal filesystem paths (T3), and SDLC-scaffolding terms in `.py` docstrings/comments. It excludes the `domains/` and `.claude/` subtrees by design. This section's residual value is the heuristic reach *beyond* that scanner: prose that leaks intent ("added for the X flow", "fix from issue #123"), PR or issue numbers, internal URLs, and use-case leakage in surfaces the scanner does not parse. **Anything `test_public_posture` would fail, flag here too** — the pre-commit heuristic and the CI gate must never disagree. For any mechanically-checkable case, prefer running `.venv/bin/pytest tests/test_public_posture.py` over eyeballing the diff.

**Scope note.** This is a forward-only ratchet on NEW changes. Legacy violations (the existing ~1,300 `T-NNNN` references and the broader use-case-specific surface area) are addressed by a separate cleanup pass and are not the concern of this section — the section fires when a diff *adds* a violation, not when an unrelated change touches a file that already contains one. Removing a legacy violation is always welcome but never required by this gate unless the diff also modifies the same docstring/comment.

**Review prompts.**

- For every docstring or `#` comment ADDED or MODIFIED in the diff, does it reference:
  - A ticket id matching `T-NNNN` (e.g., `T-0023`, `T-0195`)? Flag.
  - A non-ADR internal-scaffolding artifact: "checklist", "work plan", "decision sheet", "dispatcher prompt", "subagent contract", "cohort", "batch", a PR or issue number? Flag.
  - The motivating ticket, flow, or caller ("added for the X flow", "fix from issue #123", "used by Y")? Flag — that belongs in the commit message and PR body, not in the durable code surface.
- For every code element (identifier, string literal, module name, file path) ADDED or MODIFIED in the diff outside the `domains/` subtree, does it mention `PIM`, `pim_health`, `pim-health`, `theology`, `theological`, `patent`, `prosecution`, or other use-case-specific terminology? Flag.
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

The rule is: durable surfaces (code, docstrings, comments) describe the pattern and cite ADRs only; ephemeral surfaces (commit messages, PR bodies) carry ticket and process scaffolding.

---

## Out of scope

- Style, formatting, naming. Ruff and mypy own this surface. (Public-posture content discipline IS in scope; see P1.)
- Generic security review. The `security-review` skill (separate, Anthropic-shipped) covers this.
- Test coverage measurement, performance regression, or dependency-policy review. These are Tier 1 surfaces from the SDLC survey and have their own tooling lanes.
- Architectural-decision review. ADRs in the cas vault (`doc_type=adr`) are the substrate for that.

The skill is tuned narrowly to the documented CAS failure modes. New patterns earn their way into this skill only after they appear in the failure log (`doc_type=failure_record` in the cas vault) with a closed-form Resolution. Meeting that bar makes a pattern eligible, not admitted: the skill's value comes from being walked in full on every commit, so each added section is paid for by every future review, and a pattern already reachable by an ordinary careful reader is not worth that price. Prefer the framing that describes why a defect *survived review* over the one that describes how it was *written* — the first is what a review gate can act on.

---
name: cas-code-review
description: This skill should be used when the user asks to review a CAS commit, branch, or diff for the documented failure modes (F1-F5) catalogued in the AI-First SDLC Tooling Survey. Trigger phrases include "cas code review", "review for CAS failure modes", "audit cas changes", "check this commit for cas drift", "run cas-code-review", and similar. The skill is tuned to the CAS repository specifically; do not invoke it on unrelated codebases.
version: 0.1.0
---

# cas-code-review

A repo-resident review pass keyed to the five failure modes documented in the AI-First SDLC Tooling Survey §3 (vault: cas, doc_type=reference_document, title "AI-First SDLC Tooling Survey").

## Portability notes (forward-looking, not architectural)

This skill is currently single-tier: every section lives in this file. If a second repo someday needs its own code-review skill, the natural extraction split is:

- **Likely portable to a future universal layer.** F2 (tests-as-chronicle) and F4 (remediation-pass scope gap) are discipline failures whose abstract form should generalize to any tech stack. The CAS-specific elaborations they carry today (test-directory enumeration, the `_id(name)` helper reference) are anchoring examples, not the substance.
- **CAS-specific in form.** F1 (service-as-load-bearer router pattern), F3 (typed-alias convention with `DocumentIdStr` and friends), and F5 (`Depends(get_vault_id)` pattern) embed stack details (FastAPI, Pydantic v2, the SAGE multi-vault architecture) that do not generalize as written.

Generalization is a hypothesis until observed in a second repo. Do not extract universal sections from this file pre-emptively. When repo #2 needs its own skill, that's the trigger to author the universal layer with two data points and refactor this file to elaborate-on rather than restate.

## Section structure

Each F1-F5 section below is structured the same way:

1. **What this catches.** One-sentence framing.
2. **Already gated deterministically.** What CI catches without the skill — the skill must not duplicate this work.
3. **Review prompts.** Questions to walk through against the diff under review.
4. **What "correct" looks like.** A short reference snippet from the canonical pattern.

The skill adds value in two places: catching the residue that deterministic gates cannot reach (F2 tests-as-chronicle, F4 remediation-pass scope gap), and giving early feedback before CI runs.

## How to invoke

The reviewer (Claude) reads the diff under review and walks every section. Diff sources, in order of preference:

- A specific commit: `git show <sha>` (or `git show <sha> -- <pathspec>` to scope).
- A branch's changes against main: `git diff main...HEAD`.
- Staged changes: `git diff --staged`.
- Unstaged changes: `git diff`.

If no diff is in scope, the skill returns "no diff in scope" and stops.

For each section that fires, emit:
- The failure mode tag (F1-F5).
- The specific files or hunks that triggered the prompt.
- A short note on whether the change pulls toward or away from the canonical pattern.
- Any follow-up the reviewer should consider.

Do not emit prompts that did not fire. Brevity > completeness.

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
- Does the diff add or tighten validators on Pydantic models in `sage/models/schemas.py` (or any `models.py`/`schemas.py` under `root_harness/`)? Examples: a new `AfterValidator`, a tightened regex, a new typed alias applied to an existing field, a new `model_validator`.
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
- Does the diff add or modify any field on a class derived from `BaseModel` in `sage/models/schemas.py`, `root_harness/`, or any `*/models.py`/`*/schemas.py`?
- For each new or modified field, does it carry a known shape (a date, an id, a hash, a version label, a path, a URL, a UUID, a vault id, a section heading)?
- If yes, does the field use a typed alias from `sage/models/schemas.py` — `DocumentIdStr`, `EdgeIdStr`, `Sha256Str`, `DocumentDateStr`, or another existing alias — rather than the bare type (`str`, `str | None`)?
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
- If analogous code exists and is not in the diff, surface every missing site as a follow-up. Be specific: name the files, name the lines, name the pattern.

**What "correct" looks like.** A remediation diff that, before merging, has explicitly enumerated every parallel directory and either swept it or carries an inline note explaining why it was out of scope. F4 is a discipline failure; the only fix is to make the discipline visible in the diff.

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

## Out of scope

- Style, formatting, naming. Ruff and mypy own this surface.
- Generic security review. The `security-review` skill (separate, Anthropic-shipped) covers this.
- Test coverage measurement, performance regression, or dependency-policy review. These are Tier 1 surfaces from the SDLC survey and have their own tooling lanes.
- Architectural-decision review. ADRs in the cas vault (`doc_type=adr`) are the substrate for that.

The skill is tuned narrowly to the five documented CAS failure modes. New patterns earn their way into this skill only after they appear in the failure log (`doc_type=failure_record` in the cas vault) with a closed-form Resolution.

# AI-First SDLC Tooling Survey

**Author:** Clif Guy with Claude (Cowork session, 2026-05-08)
**Status:** Draft v0.1
**Working scope:** Python 3.12+ / FastAPI / LanceDB / SQLite / LangGraph; single-developer macOS workflow; current Claude Code integration; PIM Health and CAS portfolios.
**Target consumers:** (1) the immediate CAS practice track, (2) v2.1 of the Software Development Vision, (3) future ROOT Harness contributors and reviewers.

---

## 1. Executive summary

This survey maps the modern AI-first SDLC tooling landscape against the four documented failure modes from the CAS repository's preceding seven days (§3) and the dark-factory aspiration in v2.0.1 of the Software Development Vision. The landscape stratifies into three tiers: deterministic tooling (linters, type checkers, schema validators, structural conformance), AI-augmented review (vendor and build-your-own surfaces), and AI-native CI patterns (still mostly research, included for orientation). Each tier was evaluated against eight criteria including false-positive rate, configuration locality, failure-mode coverage, and substrate alignment.

The five-step adoption sequence in §11 produces substantial coverage of failure modes F1 through F5 within roughly fifteen developer-hours over two to three weeks. The first three tools to adopt, ranked by failure-mode leverage:

**1. Custom Claude review skill at `.claude/skills/cas-code-review.md`** authored against the §3 failure record. This is the highest-leverage Tier 2 surface for this codebase because it is the only tool that can be tuned to detect F4 (remediation-pass scope gap), it costs zero at the margin, the discipline lives in the repository, and it compounds with the substrate work rather than ratcheting cost. Custom skills dominate the vendor options (CodeRabbit, Greptile, Diamond) on this use case because the most consequential rules are not generic and cannot ship as vendor defaults.

**2. Architectural conformance test at `tests/sage/test_router_conformance.py` plus `import-linter` contracts.** The substrate-recursive layer that addresses F1 (API convention drift) and F5 (validation bypass via missing dependency declaration). Deterministic, zero false-positive rate, sub-second runtime, runs on every commit. Allowlist the four currently-drifted routers as known violations with cleanup TODOs; let the allowlist drain over time. This is the gate that would have prevented F1 from accumulating across April 13 to May 1.

**3. Hypothesis property-based testing on the typed-alias regex surface** (`DocumentIdStr`, `EdgeIdStr`, `Sha256Str`, `DocumentDateStr` from commit `98c03f9`). Constraint-shaped tests that probe the boundary, structurally distinct from the chronicle-shaped tests that F2 revealed. The substrate-adequate response to F3 (boundary-validation gap). The Anthropic agentic-PBT paper from January 2026 demonstrates Claude Code can author these tests autonomously; the human authoring path is the manual case of the same workflow.

Behind these three: Ruff for the lint-shaped quality gate (the easiest 5x improvement, adopt immediately as foundational scaffolding), and a failure log at `docs/research/failure_log.md` (the measurement apparatus that makes the 100x target falsifiable). Both should be in place before any later tool earns adoption. The full sequence and migration path are detailed in §§11-12.

The survey also produces four candidate insertions for v2.1 of the Software Development Vision (§14): the intent-gap pairing as the dual of Formal Substrate, the adequacy-versus-authority distinction surfaced by the May 5 incident, the substrate-recursion observation, and the three-tier maturity model from §4 made explicit. Each is a surgical insertion into v2.0.1's existing structure, not a restructuring.

The single most consequential observation from this survey, one that earns its weight independently of any tool recommendation: **substrate authority is independent from substrate adequacy**. CLAUDE.md principle #8 (Pydantic models derived from schemas) establishes authority; the schema can still be type-correct while range-incorrect, which is the May 5 case. Tooling that gates only authority (e.g., that prevents parallel-author drift between code and schema) does not catch adequacy failures (e.g., underspecified schemas). The §11 sequence and the §14 maturity model are constructed around this distinction; once internalized, it becomes a durable lens for future substrate decisions.

---

## 2. Methodology

This survey draws on tool documentation and release notes (primary), vendor comparison articles and curated technical outlets (secondary), academic literature where applicable (tertiary), and the GitHub release-cadence and last-commit signals as a currency check. Currency cutoff is **May 2026**. The survey is informed but not operationally validated; I have not personally installed and run the tools against the CAS codebase. Where a tool's landscape is changing rapidly enough that this snapshot will need refresh inside 6 to 12 months, the entry says so explicitly.

The tier framing (deterministic / AI-augmented / AI-native) and the eight evaluation criteria are introduced in §4 and applied uniformly across §§5-7.

---

## 3. The empirical failure record

Every recommendation in this survey traces back to either (a) one of the four documented failures from the CAS repository's preceding seven days, (b) a class of failure the literature treats as predictable for AI-first development, or (c) the dark-factory aspiration in v2.0.1 of the vision paper. Generic catalog entries are excluded. The four documented failures, named for citation throughout:

**F1: API convention drift.** Across April 13 to May 1, 2026, four routers (`vaults.py`, `documents.py`, `staging_edges.py`, `pending_metadata.py`) progressively diverged from the canonical service-as-load-bearer pattern established by the early routers (`graph_ops`, `retrieval`, `utilities`, `ingestion`, `lifecycle`, `metadata`, `users`, `filename_parser`). Each session read the increasingly drifted state as the new normal. Surfaced through audit, not gate.

**F2: Tests-as-chronicle.** When boundary validators were added to request models on May 5 (commit `98c03f9`), twelve tests in `tests/app/` began failing immediately. The failures were not detected until May 7-8 (commit `268786c`). The discovery was made by running the suite, not by gate. Diagnostic property: the tests had to change when the code became more correct.

**F3: Boundary-validation gap.** Caller-supplied `document_date` flowed through `UpdateMetadataRequest` and `IngestRequest.metadata` with no shape validation, producing eight ISO-with-time records in PIM Health that broke `sage_traverse` (May 5, commit `2e52b9c`). The schema was authoritative but underspecified; type-correct, range-incorrect.

**F4: Remediation-pass scope gap.** The May 5 typed-alias remediation (`98c03f9`) believed itself complete after rewriting fixtures in eleven `tests/sage/` files; twelve fixtures in `tests/app/` were missed because they fell outside the working mental scope. The gap was invisible until a full-suite run, three days later. The remediation discipline had its own discipline gap.

A fifth latent issue, not yet manifest as a failure but visible during the §F1 audit, is worth noting for completeness:

**F5: Validation-bypass via missing dependency declaration.** `sage/api/routers/filename_parser.py` is mounted at `/sage_vaults/{vault_id}/parse-filename` but omits `vault_id: str = Depends(get_vault_id)`, so requests with unknown `vault_id` values bypass the validation that every other vault-scoped endpoint performs. Detectable by static analysis; will be caught by an architectural-conformance gate of the kind discussed in §5.7.

These five failure modes drive the §10 coverage matrix and the §11 adoption-sequence recommendations.

---

## 4. The three tiers and the eight evaluation criteria

The tooling landscape stratifies into three tiers, and treating them as separate strata rather than one undifferentiated catalog is the move that makes the rest of the analysis tractable.

**Tier 1: deterministic tooling.** Linters, formatters, type checkers, schema validators, contract testers, security scanners, structural conformance checks. Pattern-matching or rule-based, no language-model dependency. Eighty to ninety percent of the failure modes documented in §3 should be addressed at this tier because the cost-per-check is microseconds, the false-positive rate is well-characterized, and the artifacts encode discipline in a form that survives any AI session.

**Tier 2: AI-augmented review.** Tools where a language model reads a PR diff or a codebase and produces structured feedback against rules the team specifies. CodeRabbit, Greptile, Diamond by Graphite, Sourcery, plus the build-your-own path using Claude Code review skills and subagents. Cost-per-check is seconds and pennies; false-positive rate is configuration-dependent; the discipline lives in prompts and agent definitions rather than rules. Use this tier for the gaps Tier 1 structurally cannot fill (architectural drift across files, intent-shaped reasoning, naming and idiom conformance).

**Tier 3: AI-native CI patterns.** Newer and less mature: agent-evaluating-agent setups, AI-authored property generators, mutation testing where AI proposes mutations targeted at specific invariants, executable-specification workflows. This tier is where the dark-factory aspiration eventually lives. Today it is mostly research and early-adopter; adoption should be selective and targeted at high-stakes paths.

Each tool entry that follows is evaluated against eight criteria, applied uniformly:

1. **False-positive rate.** The killer of adoption. A gate that fires falsely trains the developer (or the AI) to ignore it; a gate that is ignored is worse than no gate.
2. **Configuration locality.** Whether rules live in version-controlled files inside the repository (preferred) or in a cloud dashboard (a place where opinion drifts and audit becomes hard).
3. **Failure-mode coverage.** Which of F1-F5 the tool addresses, and at what fidelity. The basis of the §10 coverage matrix.
4. **CI runtime impact.** Tools slower than ten seconds collectively make commit cycles slow enough that developers (or their AI agents) start pushing without running them.
5. **Substrate alignment.** Whether the tool encodes truth (failing builds when the substrate is violated) or merely encodes opinion (warning when style preferences are violated). The former earn permanent residence; the latter are a tax.
6. **Maturity signals.** Release cadence, last-commit recency, breaking-change pattern, downstream adoption.
7. **License and cost.** Important even for a single-developer project because tools that require commercial licenses tend to ratchet up in cost as use deepens.
8. **Vendor lock-in.** Whether the discipline encoded in the tool can survive the tool's removal or replacement.

---

## 5. Tier 1: deterministic tooling

### 5.1 Linting and formatting (Ruff)

**Recommendation: adopt immediately.** Ruff (Astral) consolidated the Python linting and formatting landscape between 2024 and 2026. As of May 2026 it carries 900+ implemented rules, 412 in the expanded preview default set (up from a stable default of 59), full replacement scope for Flake8 and its plugins, isort, Black, pyupgrade, autoflake, and pydocstyle, and is on Astral's published 2026 style guide. Configuration is local in `pyproject.toml`. The binary is Rust-implemented; runtime against the CAS repo would be sub-second on every commit.

Failure-mode coverage in our terms: F1 (convention drift) partially, where conventions are expressible as lint rules; F5 (validation bypass) not directly, but adjacent rules (e.g., requiring specific decorator patterns) can be authored. Substrate alignment is high for the rules that encode truth (unused imports, undefined names, type-confused operators) and lower for the stylistic rules (which should generally be deferred to the formatter rather than enforced as gates).

Adoption note: the temptation with 900+ rules is to enable broadly and triage the noise. The literature pattern that produces durable adoption is the inverse: enable conservatively, add rules one at a time as failure modes surface, document each rule's rationale in the §13 template. The 412-rule preview default is a reasonable starting point only if every false positive that arises is paired with a removal-or-rationale decision.

### 5.2 Type checking (mypy strict, pyright, Astral ty)

The type-checker landscape is in flux. The stable choice today is **mypy with `--strict`** and the orthodox `disallow_untyped_defs`, `disallow_any_explicit`, `warn_unused_ignores` configuration. Pyright (Microsoft) is faster, stricter on inference, and powers Pylance in VS Code; for a Mac-based single-developer setup using Claude Code, it is a defensible alternative.

The migration target is **Astral's `ty`**, which sits at version 0.0.34 as of May 1, 2026, in beta with 1.0 targeted within the year. Astral now uses ty exclusively in their own projects. Performance is striking: 10-60x faster than mypy or pyright on identical workloads. The recommendation is to track ty's progress toward 1.0 but not adopt as primary today; the 0.0.x versioning explicitly signals breaking changes between releases. When ty reaches 1.0 (probable late 2026), reassess.

Failure-mode coverage: F3 (boundary-validation gap) only at the type level. A type checker would have flagged a missing type annotation but not the missing `YYYY-MM-DD` shape constraint, because the type was already `str`. This bounds the type checker's contribution: it is necessary but not sufficient. The shape-constraint failure belongs to schema/contract enforcement (§5.3) and property-based testing (§5.4).

### 5.3 Schema and contract enforcement (Pydantic, datamodel-code-generator, OpenAPI Spectral, Pact)

This is the substrate-adequate response to F3 (boundary-validation gap) and the operational form of CLAUDE.md principle #8.

**Pydantic v2** is already in use; the discipline question is whether `Field` constraints (`pattern=`, `ge=`, `le=`, `min_length=`, `max_length=`) and typed Annotated aliases (`DocumentDateStr`, `DocumentIdStr`, etc., established in commit `98c03f9`) are applied to every shape-bearing field at the boundary. The May 5 commit's principle should be lifted to a §13 rationale entry: "the absence of an alias on a new field is the visual anomaly."

**datamodel-code-generator** is the operational mechanism for principle #8. It generates Pydantic v2 models from JSON Schema, eliminating the parallel-author-drift failure mode at its source. Adoption requires that `docs/fs/` schemas become the authoritative input and the generated Pydantic files are not hand-edited. A pre-commit hook regenerates models when schemas change and fails the commit if generated and committed versions diverge.

**OpenAPI Spectral** lints the OpenAPI specs themselves (the schemas in `docs/fs/sage/` and `docs/fs/root_harness/`). Catches missing operation IDs, inconsistent response shapes, missing examples, undocumented error responses. A schema-of-schemas check; cheap and high-leverage given the substrate framing.

**Pact** is contract testing for service-to-service interfaces. For Phase 1 ROOT Harness (in-process SAGE imports) it is overkill; once ROOT Harness moves to HTTP, Pact becomes the boundary contract guarantor. Defer adoption until that boundary becomes real.

### 5.4 Property-based testing (Hypothesis)

**Recommendation: highest-leverage Tier 1 adoption against the documented failure record.**

Hypothesis is the only mainstream property-based testing library for Python (current version 6.152.1, April 2026). It generates inputs that satisfy a developer-specified strategy and searches for inputs that violate a developer-specified property. The library has found bugs in CPython's standard library, NumPy, and dozens of major OSS projects.

The Anthropic Red Team's January 2026 paper *Agentic Property-Based Testing: Finding Bugs Across the Python Ecosystem* (Maaz et al., NeurIPS 2025 DL4C Workshop) is directly relevant. The authors built a custom Claude Code command that autonomously writes Hypothesis tests against an arbitrary target (file, module, or function), running on Opus 4.1 and Sonnet 4.5. Of 984 generated bug reports, 56% were valid bugs and 32% were both valid and reportable; with a 15-point ranking rubric scored by Opus 4.1, the top-scoring reports were 86% valid and 81% valid-and-reportable. The agent has filed merged patches against NumPy, AWS Lambda Powertools, HuggingFace tokenizers, and others. The repository is at `mmaaz-git/agentic-pbt` on GitHub; the bug catalog is at `mmaaz-git.github.io/agentic-pbt-site/`.

This matters for our case in two dimensions. First, applied to F3, a property-based test on the `document_date` field would have generated values violating `YYYY-MM-DD` and observed the database accept them, catching the failure long before it manifested as eight corrupted rows in PIM Health. Second, the agentic variant means Claude Code itself can be configured to author the property-based tests, reducing the authoring burden that historically constrained property-based testing adoption.

Failure-mode coverage: F3 directly, F2 partially (a property-based test is constraint-shaped by construction; it cannot drift into chronicle), F4 partially (mutation testing in §5.5 is the better instrument for F4), F5 indirectly. Substrate alignment is exceptionally high. Configuration locality is excellent (test code in version control). Cost-per-check is seconds-to-minutes per test (longer than unit tests; shorter than integration). False-positive rate is low when properties are well-specified, manageable when not.

The ThoughtWorks Technology Radar Vol 34 (April 2026) places property-based testing in the broader context of "feedback controls for coding agents," alongside mutation testing, as triggers for self-correction before human review.

### 5.5 Mutation testing (mutmut, cosmic-ray)

**Recommendation: adopt to prevent F2 recurrence; mutmut is the practical default.**

Mutation testing is the answer to tests-as-chronicle. The instrument introduces small synthetic faults into the code under test (changing a `<` to `<=`, replacing a constant with a different value, removing a return statement) and runs the test suite. A mutation that survives indicates a test that did not constrain the affected behavior; a mutation that is killed indicates a test that constrains it. The published research (and the ThoughtWorks Tech Radar Vol 34 in April 2026) flags mutation testing as critical in AI-assisted development specifically because high coverage percentages can mask logically hollow tests, and because AI-generated test cases are particularly vulnerable to the "perpetually green" failure mode.

**mutmut** is the dominant Python mutation tester. 2026 benchmarks show 88.5% detection rate, 1,200 mutants per minute via AST-based generation, 150MB memory baseline scaling linearly to 500MB on 50K LOC. **cosmic-ray** is the alternative (82.7% detection, build-tool integration). The mutmut speed advantage is decisive for routine CI use; cosmic-ray's build-tool integration matters more for large polyglot projects. Recommend mutmut.

Failure-mode coverage: F2 directly. A mutation testing pass against the test suite as it existed before the May 5 typed-alias remediation would have surfaced, in advance, that adding constraints to the request models would invalidate twelve tests, because the tests that "passed" against the unconstrained code would have killed too few mutations to demonstrate that they were constraining anything at all. Substrate alignment is high. Cost is significant (the suite runs once per mutation; mutmut's 1,200 mutants/min throughput keeps full-repo passes under an hour for a CAS-sized codebase). Run policy: nightly or weekly, not per-commit.

### 5.6 Security scanning, light treatment (Bandit, Semgrep defaults, gitleaks, Dependabot)

For a single-developer Mac setup with no current production deployment, the high-leverage security tools are cheap to adopt and cover most of the risk surface.

**Bandit** is the Python static security scanner. It catches common patterns: `subprocess.Popen` with `shell=True`, hardcoded passwords, weak crypto, SQL injection patterns. False-positive rate is moderate; the discipline is to triage early and `# nosec` with rationale. Subsumed partially by Ruff's `S` rules (Bandit-derived); running Ruff's S category may suffice without adopting Bandit standalone.

**Semgrep Community Edition** (LGPL-2.1, free, 2,800+ community rules) is the standout for this category. The pattern-matching engine accepts rules in YAML-ish syntax that reads like example code, and the matching is AST-aware, which is unusually well-suited to encoding the user's "absence of an alias on a new field is the visual anomaly" principle as an enforceable rule. Semgrep's Pro library (20,000+ rules) is a paid feature; the community edition is sufficient for this use case. Semgrep's 2026 introduction of Custom Workflows (early beta, plain Python) is a development worth tracking but not yet adopting.

**gitleaks** scans commits and history for accidentally committed credentials. Run as a pre-commit hook and as a CI step; cheap, high-leverage.

**Dependabot** (GitHub-native, free) monitors dependencies for published vulnerabilities and produces auto-PRs to update. Configure via `.github/dependabot.yml`; near-zero ongoing cost.

Failure-mode coverage: not directly addressing F1-F5, but covering classes of failure that are predictable for any project that grows beyond the single-developer phase. Worth adopting now to avoid retrofitting.

### 5.7 Architectural and structural conformance (import-linter, custom AST checks, fitness functions)

This is the load-bearing answer to F1 (convention drift) and F5 (validation bypass), and the operational form of *Building Evolutionary Architectures* (Ford, Parsons, Kua) in the Python ecosystem.

**import-linter** enforces import-graph contracts: layered architectures (lower layers may not import from upper layers), forbidden imports (the SAGE-only-via-stewards rule from CLAUDE.md), independent module boundaries. Configuration is YAML in the repository. Failure-mode coverage: directly addresses the "stewards do not import from orchestrators" rule the CLAUDE.md establishes as architectural principle 5; partially addresses F1 where convention drift manifests as boundary leaks (e.g., `vaults.py` importing `vault_management` directly).

**Custom AST checks** are the operational form of architectural fitness functions for the rules import-linter cannot express. Examples relevant to the F1 audit findings: every router file in `sage/api/routers/` must declare exactly one `APIRouter` at module scope and contain no top-level helper functions (a Python AST walker of approximately fifty lines). Every endpoint in a vault-scoped router must declare `Depends(get_vault_id)` (the F5 fix). Every router must have a corresponding service file in `sage/services/` and a `get_X_service` helper in `sage/api/dependencies.py` (the structural conformance check that prevents the convention drift documented in §F1). These are pytest tests that introspect the codebase at the AST level; they run in the regular test suite.

The handsonarchitects.com piece *Protecting Architecture with Automated Tests in Python* (2026) and the LukasNiessen/ArchUnitPython library are the current references for this discipline. ArchUnitPython is less mature than the Java original but provides scaffolding for the more elaborate fitness function patterns.

Substrate alignment is maximal. Configuration locality is excellent. Cost is millisecond per check. False-positive rate is exactly zero by construction (the rules are deterministic). This is the highest-leverage gate for AI-assisted development convention drift.

### 5.8 Performance regression (pytest-benchmark, py-spy, scalene)

For the CAS phase the repository is in (research/development, no production load), performance regression tooling is forward-looking rather than urgent. Worth knowing the landscape:

**pytest-benchmark** integrates with pytest to time specific functions and fail tests that regress beyond a configurable threshold. Configuration is local; runtime adds overhead to the test suite (run as a separate marker so it does not slow general test runs).

**py-spy** is a sampling profiler that attaches to running Python processes without modification. Useful for diagnosing hot spots after the fact.

**scalene** is a CPU+memory profiler with line-level granularity, particularly strong for identifying memory-allocation hot spots; relevant for SAGE's LanceDB and Qwen3 paths once those become performance-critical.

**Memray** is the modern memory profiler for Python (Bloomberg open-source). Stronger than scalene for memory-specific analysis.

Failure-mode coverage: not addressing F1-F5. Adopt when performance becomes a target metric, not before.

---

## 6. Tier 2: AI-augmented review

The Tier 2 landscape is in mid-2026 the most rapidly evolving stratum of the AI SDLC stack. Two factors make it volatile: the underlying language models improve every few months, and the vendors are still discovering which review modalities matter most for which team shapes. Recommendations in this section are calibrated to a single-developer Mac workflow with Claude Code already in use; conclusions will not generalize cleanly to enterprise teams.

### 6.1 The vendor landscape (CodeRabbit, Greptile, Diamond, Sourcery)

Published 2026 benchmarks across the major vendors give a calibrated baseline:

**CodeRabbit** is the most widely installed AI code review app, with 2 million+ repositories connected and 13 million+ PRs processed. The 2026 benchmark profile is precision-favored: 2 false positives per benchmark run, 44% bug-catch rate. The architectural limitation is diff-based analysis; CodeRabbit sees what changed in the PR but not how those changes interact with the rest of the codebase. Pricing is $24 per developer per month annual, $30 monthly; a free tier covers unlimited public and private repos with rate limits. Configuration via `.coderabbit.yaml` in the repository, which keeps the discipline auditable. The 40+ linter and SAST integrations are a meaningful asset; CodeRabbit can wrap and unify many Tier 1 tools under one review surface.

**Greptile** indexes the entire repository and builds a code graph, using multi-hop investigation to trace dependencies, check git history, and follow leads across files. The 2026 benchmark profile is recall-favored: 11 false positives per run (5.5x CodeRabbit's noise), 82% bug-catch rate (1.86x CodeRabbit's recall). Pricing is $30 per user per month. The whole-repo knowledge graph is the design move that distinguishes Greptile from every other Tier 2 tool; it is the only tool in this tier that would have caught failure F4 (the tests/app/ scope gap) on its own initiative, because the graph would have linked the schema change in `98c03f9` to every test file that imported affected fixtures.

**Diamond** (Graphite) targets teams already on Graphite's stacked-PR workflow. Profile: low comment volume, high actionability (less than 5% negative feedback on comments), 82% conversion-to-fix rate on the issues it does flag. Detection is meaningfully lower than CodeRabbit or Greptile; the published guidance is to use Diamond as a supplemental reviewer rather than the primary, and the value proposition is highest if the team is already invested in Graphite's stacked PR tooling.

**Sourcery** focuses more on AI refactoring than on review, and is increasingly positioned as a generation tool rather than a review tool. Out of scope for this survey beyond the mention.

### 6.2 The build-your-own path (Claude Code review skills, subagents)

The strategically significant alternative is to build review discipline directly into Claude Code via slash commands and subagents, using the `review` and `security-review` patterns Anthropic ships in the Claude Code marketplace plus custom skills authored in `.claude/skills/`.

The case for this path:
- **Configuration locality is maximal.** The discipline lives in version-controlled markdown files in your repository. Audit, diff, and revert work the same as for any code.
- **Cost at the margin is zero.** You are already paying for Claude usage; review skills consume the same budget.
- **No vendor lock-in.** The discipline encoded in skills is the discipline itself; it is portable to any sufficiently capable model.
- **Failure-mode tuning is precise.** A skill can encode "always check that any new field added to a Pydantic request model has a typed alias" with rationale that traces back to commit `98c03f9`. CodeRabbit's rule library does not contain that rule and cannot, because it is not a generic concern.
- **The Anthropic Tech Radar entry "Superpowers" (April 2026) is a published reference implementation.** It is a Claude Code plugin marketplace entry that wraps a coding agent in a structured workflow of skills enforcing brainstorming-before-coding, planning-before-implementation, red-green-refactor TDD, root-cause-first debugging, and post-implementation code review.

The case against:
- **Maintenance burden falls on you.** CodeRabbit ships rules curated by professionals; your skills encode discipline you authored. The trade-off is rule quality versus rule fidelity.
- **No published benchmarks.** You have only your own evaluation, not industry-comparable numbers.
- **Cross-cutting integrations (40+ linters in CodeRabbit's case) require manual wiring.**

The strategic recommendation, given your goals from §F2 (importing curated lessons) and §F3 (100x failure-mode reduction), is to build a custom Claude review skill as the primary surface and treat CodeRabbit or Greptile as a possible supplementary reviewer (free tier, augmenting rather than replacing). The custom skill is the path that compounds with your CLAUDE.md and substrate work; the vendor option is a hedge.

### 6.3 Workshopped comparison

**Synthetic, not run.** The comparison below is informed prediction based on documented capabilities, published benchmarks, and review samples from each vendor's marketing materials. None of the tools were actually invoked against the CAS repository. The exercise is useful for orientation but should not be confused with actual evaluation; if any specific recommendation lands, validation requires running the tool against a real diff before commitment.

**Sample diff:** commit `98c03f9` (May 5, 2026), "Apply typed-alias validators to shape-bearing request-model fields." Touches `sage/models/schemas.py` (introduces `DocumentIdStr`, `EdgeIdStr`, `Sha256Str`, `DocumentDateStr` typed aliases), eleven test fixture files in `tests/sage/`, and the corresponding request models. Net change: +287 / -119. The known latent gap: twelve tests in `tests/app/` were missed and broke silently for three days.

**Predicted CodeRabbit output (precision-focused, diff-bounded):**

CodeRabbit would emit between four and seven comments. Plausible content: (1) approval of the typed-alias adoption pattern with a one-line architectural commendation; (2) suggestion to apply the same pattern to one or two fields in adjacent models that the diff did not touch but that CodeRabbit can see are shape-bearing in the changed files; (3) a request to add or expand docstrings on the new aliases to document the regex; (4) a question about backward compatibility for any external callers of the changed request models. CodeRabbit would *not* surface the `tests/app/` scope gap because diff-based analysis structurally cannot reach files outside the diff. The comments would be high-quality and worth reading; the failure mode is precisely the one that tripped the human reviewer.

**Predicted Greptile output (recall-focused, whole-repo):**

Greptile would emit between twelve and twenty comments. Plausible content includes everything CodeRabbit would say plus: (1) a flag that `tests/app/test_app_backend.py` (and named other files) contains fixtures using mock IDs that will not satisfy the new validators, with specific line numbers; (2) a flag that the MCP layer calls these request models in code paths the diff does not touch, with implications listed; (3) several lower-priority observations linking to historical commits (e.g., "this typed-alias pattern parallels the structural-conformance approach taken in commit `[X]`"). The signal-to-noise ratio is lower than CodeRabbit's, but the F4 finding alone justifies the noise. This is the failure mode where Greptile's design pays off most.

**Predicted Diamond output (Graphite-native, low volume):**

Diamond would emit between two and four comments, focused on the immediate diff. Quality would be high; the comments would be the kind a senior reviewer would write at the end of a Graphite stack review. Diamond would not surface the F4 gap (scope-limited like CodeRabbit) and would offer fewer architectural observations than CodeRabbit. Without the Graphite stacked-PR workflow already in place, Diamond's value is reduced.

**Predicted custom Claude review skill output (configurable, your discipline):**

A custom skill authored against your CLAUDE.md and the §3 failure record would be tuned to surface (a) any field added to a request model without a typed alias (the `98c03f9` rationale codified), (b) any test file referencing a shape-bearing field with a mock value that does not satisfy the alias's regex, (c) any diff that touches `sage/models/schemas.py` without a corresponding update to all `tests/` directories that import the affected schema. Comment volume would be calibrated by your skill's prompt. F4 detection is a direct consequence of including the third rule above. Comment quality depends on the skill author's craft; with the §3 failure record as input, the rule set is concrete enough to be encoded today. Cost: zero at the margin.

**Comparative summary:**

| Dimension | CodeRabbit | Greptile | Diamond | Custom Claude skill |
| --- | --- | --- | --- | --- |
| Catches F4 on this diff | No | Yes | No | Yes (if rule encoded) |
| Comment volume | 4-7 | 12-20 | 2-4 | Tunable |
| Configuration locality | High (`.coderabbit.yaml`) | Low (cloud dashboard) | Medium (Graphite settings) | Maximal (`.claude/skills/`) |
| Cost (single dev) | Free tier or $24-30/mo | $30/mo (no free tier) | $30/mo | $0 |
| Vendor lock-in | Moderate | High | High | None |
| Discipline portability | Low | None | None | High |

The comparison reinforces the §6.2 recommendation: the custom Claude review skill earns first place on this diff because the highest-leverage finding (F4 detection) is encodable from the §3 failure record, and the skill compounds with the CLAUDE.md and substrate work. Greptile is a defensible supplementary choice if the maintenance burden of authoring custom skills proves prohibitive; CodeRabbit and Diamond are dominated for this use case.

### 6.4 Configuration locality and the vendor lock-in question

The substrate principle has a sharp implication for Tier 2 tool selection that deserves explicit naming. Tools whose discipline lives in the repository (CodeRabbit's `.coderabbit.yaml`, custom Claude skills) preserve the discipline if the tool is removed; the rules can be ported. Tools whose discipline lives in a cloud dashboard (Greptile's UI configuration, Diamond's Graphite settings) lose the discipline if the tool is removed; the rules go with the vendor. For a project building toward dark-factory development with substrate as load-bearing artifact, the cloud-dashboard pattern is structurally misaligned. This consideration is independent of vendor quality; it is about whether the vendor's contribution to your discipline can survive their absence.

---

## 7. Tier 3: AI-native CI patterns

This tier is where the dark-factory aspiration eventually lives, but it is also where claims should be most cautious. The published research is recent (most of it 2025-2026), the implementations are early-adopter, and the patterns are still being named. Adoption today should be selective and targeted; any of these patterns could plausibly be revised or replaced before the end of 2027. Treat this section as maturity-model orientation, not as immediate recommendations.

**Agent-evaluating-agent designs.** The simplest pattern, and the one with the strongest published track record, is to run a fresh-context agent as a reviewer of a peer agent's output. Anthropic's Claude Code best-practices documentation references this directly: a fresh Claude with no implementation history will catch deviations its sibling missed because it has no sunk cost in the new code. The Anthropic agentic property-based testing paper from §5.4 implements a more elaborate version: the same agent that generates bug reports also self-reflects ("if it failed, has the test truly discovered a bug, or does the test need to be adjusted?"), and a second agent applies a 15-point ranking rubric to surface the most-likely-valid reports. The ThoughtWorks Tech Radar Vol 34 names "Superpowers" (Claude Code plugin marketplace) as the published reference workflow that wraps a coding agent in plan-then-implement, red-green-refactor TDD, and post-implementation review skills. For your stack, the natural first step is to author a `review` skill in `.claude/skills/` that runs against the most recent commit on a fresh context, with explicit instructions to read the §3 failure record before reviewing.

**AI-authored property generators.** The standard pattern with Hypothesis is that the developer writes the strategy (the input domain) and the property (the invariant). The agentic-PBT pattern from §5.4 demonstrates that an LLM can author both, given the function signature, docstring, and surrounding context. The implication for substrate-driven development is that as your `docs/fs/` JSON Schemas become more rigorously specified, the schemas themselves contain enough information to generate property-based tests automatically. This is a future capability worth tracking; the substrate principle predicts it should be feasible, and the Anthropic paper provides early evidence. No tool today does this end-to-end automatically; the manual path is to point Claude at a schema and ask for Hypothesis tests, which is approximately the agentic-PBT workflow.

**AI-proposed mutation testing.** The dual of property-based testing. Today's mutation testers (mutmut, cosmic-ray) generate mutations from a fixed grammar of operators (replace `<` with `<=`, change `True` to `False`, etc.). An AI-aware variant would propose mutations targeted at specific invariants the developer cares about: "mutate the `document_date` validator regex to accept ISO-with-time, and verify that some test fails." This pattern is theoretically attractive because it focuses mutation testing's expensive runtime on the mutations most likely to expose chronicle-shaped tests, but no production tool currently does this. The Anthropic Tech Radar entry on mutation testing-as-feedback-control for coding agents is the closest published reference; the implementation gap is still real.

**Executable-specification workflows.** The umbrella for Spec Kit, BMAD, the ACM TOSEM "SE 3.0" research program, and the Lahiri intent-formalization vision. The pattern is that the specification (in markdown, in JSON Schema, or in a verification-aware language) is executable: the same artifact drives code generation, test generation, and verification. For substrate-aligned development, this is the destination. For today's CAS phase, the mature subset is "schema-first development with code generation," which is already in your CLAUDE.md as principle #8 and operationalized through datamodel-code-generator (§5.3). The fully executable-specification destination requires verification-aware specification languages (Dafny, F*, Verus); adoption of those is a separate, larger project beyond the scope of this survey.

**Substrate-recursive checks.** A category not yet named in the published literature, surfaced in the May 8 conversation that produced this survey. The observation: the Formal Substrate constrains domain code (consent state machines, adjudication invariants) but must also constrain the structural code that implements the substrate (API shapes, dependency-injection topology, test placement). Substrate is recursive. Tooling implication: the architectural conformance layer (§5.7) is the first layer of substrate-recursive checking; its rules should themselves be derived from substrate artifacts where possible (e.g., generate the import-linter contract from the JSON Schema's namespace inventory). This is research direction rather than tool category, included here for completeness against the dark-factory destination.

**Recommendation for Tier 3 today:** author one fresh-context agent-evaluating-agent review skill against the §3 failure record (concrete, low-cost, demonstrable value). Track the agentic-PBT research and the Tech Radar's quarterly updates for the next twelve months before considering broader Tier 3 adoption. Reassess at the time of v2.2 of the vision paper.

---

## 8. Pre-commit and CI orchestration

The orchestration layer is where Tier 1 tools combine into a working gate. Two questions dominate: which framework to use, and where to enforce each gate.

**pre-commit framework versus lefthook.** The pre-commit framework (Yelp, Python, 2014) has the larger Python ecosystem, with hundreds of pre-built community hooks and language-isolated execution that runs hooks in their own virtualenvs without local-machine dependencies. Lefthook (evilmartians, Go, 2019) is faster (the 2026 benchmarks show 22% faster runs and 40% less memory on Python/Java monorepos) and offers parallel execution out of the box. For a single-developer Python project with Claude Code in the loop, **pre-commit framework is the recommendation**: the broader Python ecosystem matters more than the speed delta, and the language-isolated execution avoids "works on my machine" failures when Claude Code runs in a different environment than your local terminal.

Configuration lives in `.pre-commit-config.yaml` at the repository root. The substrate principle applies: each hook entry should have a corresponding line in `tooling.md` (§13) that names the failure mode it gates.

**Where to enforce each gate.** A two-layer model works for the CAS phase:

*Local pre-commit hooks* (run at `git commit`): Ruff (lint and format), gitleaks, the import-linter contract check, AST-walker conformance checks for routers (the F1 fix). These should be sub-second collectively. If they exceed five seconds, commits stop happening cleanly; the pattern is that developers (or AI agents) add `--no-verify` and the gate becomes ceremonial rather than enforcing.

*CI gates* (run on push to remote, or in CI on every PR): mypy strict, the full test suite, mutation testing on a nightly cadence, security scanning (Bandit/Semgrep, Dependabot already runs on schedule), Spectral OpenAPI validation, schema-derived Pydantic-regeneration drift check. CI gates can take minutes; they fail builds, not commits.

A useful third tier for the highest-leverage but most expensive checks: *manual or scheduled* gates run weekly or on-demand. Mutation testing across the full repository is the canonical example. Property-based test runs with deep configurations also fit here.

**GitHub Actions for a single-developer Mac setup.** The path of least resistance is `.github/workflows/ci.yml` that runs on push and pull_request, with separate jobs for lint (Ruff), type-check (mypy strict), test (pytest), security (Bandit/Semgrep CE, gitleaks). For mutation testing, a separate `.github/workflows/mutation.yml` triggered on schedule (`cron: '0 2 * * 0'` for weekly Sunday at 02:00 UTC) and on workflow_dispatch for manual runs. Dependabot is configured via `.github/dependabot.yml`. The GitHub-Anthropic integration (Claude Code review on PRs) is configured via repository settings; it acts as the Tier 2 supplementary review.

**Runtime budget guidance.** Pre-commit hooks: target under 3 seconds collectively, never over 10. Per-PR CI: target under 5 minutes for green-path runs, under 15 minutes for changed-everything runs. Nightly mutation: under 60 minutes for the relevant subset (full-repo mutation testing every night is overkill for a single-developer pace; weekly is appropriate).

**Anti-pattern to avoid.** Stacking too many tools at the pre-commit layer. The 22 percent speed advantage of lefthook is irrelevant if pre-commit's local hooks finish in two seconds. The substrate-violating pattern is enabling many hooks of which several are stylistic-rather-than-correctness; each one trains the developer (or agent) to ignore the gate when it fires falsely.

---

## 9. Evaluation matrix

Tools (rows) scored against the eight evaluation criteria from §4. Scoring scale: H (high / strong), M (medium / acceptable), L (low / weak). Cost figures are May 2026; license is the OSS license name where applicable. "Substrate" is shorthand for substrate alignment per §4.5.

| Tool | FP rate | Config locality | F-mode coverage | CI runtime | Substrate | Maturity | License/cost | Lock-in |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ruff | M | H | M (F1 partial) | H (sub-second) | M-H | H | MIT, free | L |
| mypy --strict | L-M | H | L (F3 weak) | M (5-30s) | M | H | MIT, free | L |
| Astral ty (track) | L | H | L (F3 weak) | H (10-60x faster) | M | M (0.0.x) | Apache, free | L |
| pyright | L-M | H | L (F3 weak) | H | M | H | MIT, free | L |
| Pydantic + aliases | L | H | H (F3, F5) | H | H | H | MIT, free | L |
| datamodel-code-generator | L | H | H (F3) | H | H | H | MIT, free | L |
| OpenAPI Spectral | L | H | M (F3 schema-level) | H | H | H | Apache, free | L |
| Hypothesis | L | H | H (F2 partial, F3) | M (sec-min/test) | H | H | MPL, free | L |
| mutmut | L | H | H (F2 directly) | L (long runs) | H | H | BSD, free | L |
| Bandit / Ruff S-rules | M | H | L (security only) | H | M | H | Apache, free | L |
| Semgrep CE | L | H | M (custom rules) | M | H | H | LGPL, free | L |
| gitleaks | L | H | L (secret scan) | H | M | H | MIT, free | L |
| Dependabot | L | M (cloud + repo) | L (deps only) | H | M | H | GitHub-bundled | M |
| import-linter | L (deterministic) | H | H (F1) | H | H | H | BSD, free | L |
| Custom AST checks | L (deterministic) | H | H (F1, F5) | H | H | H | (your code) | L |
| pytest-benchmark | L | H | L (F-modes N/A) | M | M | H | BSD, free | L |
| pre-commit framework | N/A | H | (orchestration) | H | H | H | MIT, free | L |
| Custom Claude review skill | M | H | H (all of F1-F5) | M | H | M (early) | (your skills) | L |
| Greptile | M-H | L (cloud config) | H (F1, F4) | L (review time) | L (cloud) | H | $30/user/mo | H |
| CodeRabbit | L | M (.coderabbit.yaml) | M (F1 partial) | L (review time) | M | H | Free or $24+/mo | M |
| Diamond (Graphite) | L | M (Graphite UI) | L (diff-bound) | L | L | M | $30/user/mo | H |

**Reading the matrix.** The H/H/H/H/H/H corner of the table identifies tools that are strong on every dimension that matters for substrate-aligned development; this corner is dominated by the deterministic Tier 1 tools (Pydantic, Hypothesis, mutmut, import-linter, custom AST checks). The Tier 2 vendors all carry locality and lock-in penalties that the custom Claude skill path does not. Astral ty and Semgrep CE Custom Workflows are tools to track for adoption later. Ruff is the only Tier 1 tool with a moderate FP rate, and the rate is configurable downward by selective rule enabling (§5.1).

---

## 10. Coverage matrix

Failure modes from §3 (rows) mapped to tools that address them (columns). Strength annotations: *direct* (tool primarily addresses the failure mode), *partial* (tool addresses some aspect), *adjacent* (tool catches related but distinct failures), *—* (no coverage).

| | Pydantic + aliases | Hypothesis | mutmut | import-linter | AST checks | Custom Claude skill | Greptile | CodeRabbit | Full-suite CI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **F1** API convention drift | — | — | — | direct (boundaries) | direct (per-router invariants) | direct (rule-encoded) | partial (cross-file) | partial (diff-bound) | — |
| **F2** Tests-as-chronicle | — | partial (constraint-shaped by construction) | direct (mutation kills hollow tests) | — | — | partial (rule for chronicle patterns) | — | — | — |
| **F3** Boundary-validation gap | direct (typed aliases) | direct (probes boundaries) | adjacent | — | adjacent | partial (rule for missing aliases) | — | — | — |
| **F4** Remediation-pass scope gap | — | — | — | — | — | partial (rule for scope-completeness) | direct (whole-repo graph) | — | direct (catches downstream test failures) |
| **F5** Validation-bypass via missing dependency | — | — | — | — | direct (Depends declaration check) | direct (rule-encoded) | partial | partial | — |

**Reading the matrix.** The matrix reveals four insights:

First, no single tool covers all five failure modes. The substrate-adequate response is a tool stack, not a tool. The minimum coverage stack is: Pydantic typed aliases (F3), mutmut (F2), import-linter or AST checks (F1, F5), and full-suite CI on every commit (F4). Hypothesis adds depth on F3; the custom Claude skill adds breadth across all five.

Second, F4 (remediation-pass scope gap) is the most under-served by published tools. Full-suite CI on every commit is the default safety net (and its absence in the CAS repository is itself a finding). Greptile's whole-repo graph is the only purpose-built tool that addresses F4 at design time; the custom Claude skill can be tuned to the same end. The fact that F4 is hard to gate at all is part of why it surfaced late in the May 5-8 episode.

Third, the custom Claude skill column shows partial-or-direct coverage on every row. This is the column that justifies §6.2's recommendation: with sufficient configuration, the skill addresses every documented failure mode. The cost is the maintenance burden of authoring and refining the skill.

Fourth, the deterministic Tier 1 tools (left half of the matrix) are dominant in coverage strength but limited in breadth; the Tier 2 tools (right half) are broader but weaker per-cell. This matches the §4 framing: Tier 1 for the failure modes that admit deterministic gates; Tier 2 for the gaps Tier 1 structurally cannot fill.

---

## 10. Coverage matrix

*[Stub. Failure modes F1-F5 (rows) x tools that address them (columns), with strength annotations.]*

---

## 11. Recommended adoption sequence

The recommendation is a five-step sequence calibrated to give substantial F1-F5 coverage within roughly fifteen developer-hours of work, distributed over two to three weeks. Each step must produce a corresponding entry in `tooling.md` (§13) before the next step begins; the rationale-document discipline is what prevents tool sprawl.

**Step 1 (≈1 hour). Establish the failure log.** Create `docs/research/failure_log.md` with the schema named in the May 8 conversation: discovery date, introduction date, classification, link to the discovery commit, link to the fix commit. Backfill with F1-F5. Every defect discovered post-hoc from this point forward gets an entry. This is the measurement apparatus that supports the 100x target; without it, every later step is operating without feedback. The failure log itself is a formal-substrate artifact at the meta-tooling layer.

**Step 2 (≈3 hours). Adopt Ruff plus a minimal pre-commit configuration.** Install Ruff, configure `[tool.ruff]` in `pyproject.toml` to start with the F-rule subset and the formatter, add `.pre-commit-config.yaml` with Ruff and gitleaks as the only initial hooks. Run against the whole repository, triage and resolve or `noqa`-with-rationale every existing finding. Document each rule enabled in `tooling.md` with the failure mode it addresses. This is the easiest 5x improvement and the foundation for every subsequent step.

**Step 3 (≈4 hours). Build the architectural conformance test.** Create `tests/sage/test_router_conformance.py` (the artifact proposed in the §F1 audit) that asserts every router has a corresponding service, every endpoint declares `Depends(get_vault_id)`, every router contains no helper functions, every Pydantic model in routers comes from `sage.models.schemas`. Allowlist the four currently-drifted routers as known violations with TODO comments referencing follow-up commits. Add `import-linter` with a contract preventing routers from importing storage modules directly. Both tests run on every CI invocation. This addresses F1 and F5 directly and gives you the substrate-recursive layer the §7 discussion named.

**Step 4 (≈4 hours). Adopt Hypothesis property-based testing for the boundary-validation surface.** Author Hypothesis tests for every typed-alias regex (`DocumentIdStr`, `EdgeIdStr`, `Sha256Str`, `DocumentDateStr`) that probe the boundary: generate strings that nearly match but violate, generate strings outside the alphabet, generate empty and oversized inputs. The tests should fail today if any alias's regex is wrong; they should fail in the future if the substrate adequacy regresses. This addresses F3 directly and produces the constraint-shaped tests that contrast with the chronicle-shaped tests F2 revealed.

**Step 5 (≈3 hours). Author the custom Claude review skill.** Create `.claude/skills/cas-code-review.md` with explicit rules tied to each F1-F5 failure mode, the §3 failure record as input context, and instructions to read `tooling.md` before reviewing. Tune by running against the next three significant PRs and refining based on false-positive and false-negative observations. This is the Tier 2 surface that compounds rather than ratchets cost.

**Steps 6 and beyond (deferred, conditional).** Once steps 1-5 are settled and the failure log shows reduced incidence, add: mutation testing on a weekly cadence (mutmut, F2), Semgrep CE with a small custom-rules library (substrate-as-pattern, F1 and F5 reinforcement), datamodel-code-generator pipeline (F3 reinforcement), OpenAPI Spectral (schema-of-schemas validation). A second-cohort review pass at the three-month mark reassesses based on what the failure log has learned.

**Measurement plan.** Define success against the failure log. Baseline period: two weeks (count failures). Target after step 5 adoption: 80% reduction in introduced-but-not-caught defects within four weeks. Target after step 8 adoption: an additional 60% reduction within four weeks. The two compounded reductions yield approximately 96%, the leading edge of the 100x goal. The remaining 2-3 doublings come from Tier 3 patterns and from substrate-adequacy improvements in `docs/fs/`, both of which are longer-horizon.

**What this sequence deliberately does *not* include.** No CodeRabbit or Greptile in the first cohort, on the rationale that the custom Claude skill compounds with the substrate work and the vendor options do not. No mypy in the first cohort, on the rationale that Ruff already covers the lint-shaped portion of the type story and full mypy adoption against a 700-file codebase is itself a substantial effort. No mutation testing in the first cohort, on the rationale that mutation testing is an oracle for *test adequacy* and steps 1-5 should land first to give mutation testing something stable to evaluate. No commercial security tools, since gitleaks plus Dependabot plus Ruff's S-rules cover the high-leverage portion at zero cost.

---

## 12. Migration path

The CAS repository's current CI configuration is essentially absent: no `.github/workflows/`, no `.pre-commit-config.yaml`, no `tooling.md`. The 822-test pytest suite exists and is the only quality gate currently operating. Single-developer commit cadence is several commits per active session, with sessions spanning multiple days. This profile shapes the migration path in three ways.

**Constraint 1: gates must not slow the inner loop measurably.** A single-developer Mac workflow tolerates pre-commit gates only if they finish in a few seconds. Anything longer creates pressure to skip them. The migration path therefore enforces locally only the gates that complete sub-second collectively, and pushes longer-running gates to CI where they run asynchronously.

**Constraint 2: every new gate's adoption must follow a calibration period.** A gate that fires falsely from day one trains the developer to disable it. The migration sequence provides for a one-week calibration window after each gate is enabled, during which findings are triaged rather than auto-blocked, and the rule set is tuned to the observed false-positive rate before the gate becomes blocking.

**Constraint 3: the existing 822-test suite is an asset to protect.** Any new gate that fires findings against the existing suite must either be allowlist-bounded or paired with a remediation pass before becoming blocking. The §F1 four drifted routers are the canonical case: the conformance test should ship with them allowlisted, and the allowlist should drain over time.

**Migration sequence.**

*Week 1.* Step 1 (failure log) and Step 2 (Ruff + minimal pre-commit) from §11. Configure Ruff in warn-only mode for the first three days; flip to blocking once existing findings are resolved. Add `.github/workflows/ci.yml` that runs the existing pytest suite on every push and PR, with `--strict-markers` and `--strict-config`. The workflow runs in parallel with whatever commit cadence is active; the calibration is "do CI runs ever fail in a way the developer thinks is wrong." If yes, tune. If no, hold.

*Week 2.* Step 3 (architectural conformance test). Author the test against the §F1 audit, allowlist the four drifted routers explicitly, push to repo. CI runs the test on every PR. The allowlist is documented in the test itself with TODO references to specific cleanup commits planned. Add `import-linter` with the SAGE-only-via-stewards contract and the routers-do-not-import-storage contract. Calibration: confirm that the test catches fictional violations introduced into a scratch branch; if it does, ship.

*Week 3.* Step 4 (Hypothesis on typed aliases). Add `tests/sage/test_alias_invariants.py` with property-based tests against each typed alias's regex. These tests should fail if the regex is too permissive (which is the F3 risk in mirror-image), succeed otherwise. Runs as part of the regular pytest suite. Calibration: ensure the tests deliberately probe failure modes that would have surfaced F3 before the May 5 incident.

*Week 4.* Step 5 (custom Claude review skill). Author `.claude/skills/cas-code-review.md` against the §3 failure record. Run against the next three substantial PRs in parallel with whatever review process exists today. Calibration: log skill findings to the failure log under a special category; assess whether the findings are useful before relying on the skill as a primary gate.

*Months 2-3.* Steps 6-8 from §11 (mutation testing, Semgrep CE, datamodel-code-generator pipeline). Each adopted with the same one-week calibration window before becoming blocking.

**Failure mode this migration path is itself susceptible to.** The migration discipline can drift: a gate is added in warn-only mode, its calibration is never completed, and it sits warn-only indefinitely. To prevent this, every gate's `tooling.md` entry includes a mandatory calibration-end date. If calibration is not completed by that date, the gate is removed rather than left warn-only. This is the substrate-adequacy principle applied to the migration path itself: the discipline that admits ambiguous states is the discipline that lets ambiguous states accumulate.

**Rollback discipline.** Every step's `tooling.md` entry must include a sunset rule: under what condition would this tool be removed? "Tool removed from production for two weeks with no failure-log incidents traceable to its absence" is one such rule. The discipline of being able to remove tools is what prevents the cargo-cult sprawl.

---

## 13. Rationale-document template (`tooling.md`)

`tooling.md` is the substrate of the tooling layer. It lives at the repository root, is version-controlled, and contains one entry per active tool. The discipline that authoring this document enforces is the prevention of cargo-cult tool sprawl: a tool that cannot earn an entry is a tool that should not be in the pipeline.

**Entry template:**

```markdown
## [Tool name]

**Adopted:** YYYY-MM-DD
**Calibration end:** YYYY-MM-DD (one week after adoption; if not completed, tool is removed)
**Tier:** 1 / 2 / 3
**Where it runs:** pre-commit / CI per-PR / CI scheduled / manual

**Failure mode gated:** [Reference to F1-FN failure-log entry, or named class.]
[1-3 sentences explaining what specific defect this tool catches.]

**Configuration:** [File path of the rule configuration, or note that rules live in vendor cloud.]

**Cost:**
- Pre-commit run time: [seconds]
- CI run time per invocation: [seconds]
- License/subscription: [free / $X/mo / annual cost]
- Maintenance: [estimated hours per month]

**False-positive rate after calibration:** [percentage, or "to be measured"]

**Sunset rule:** [Specific condition under which this tool would be removed.
Example: "Removed if no failure-log entries trace to the tool's absence over a four-week window after disabling."]

**Substrate alignment:** [H/M/L per §4.5 of the tooling survey, with one-line rationale.]

**Replacement candidates considered:** [List of tools considered and rejected, with one-line rationale per rejection.]
```

**Worked example:**

```markdown
## Ruff

**Adopted:** 2026-05-15
**Calibration end:** 2026-05-22
**Tier:** 1
**Where it runs:** pre-commit (lint, format), CI per-PR (lint with `--diff`)

**Failure mode gated:** F1 (API convention drift) partially; broad class of
mechanical-quality drift.
Ruff catches unused imports, undefined names, type-confused operators, deprecated
syntax, and a curated subset of stylistic conventions. Its primary value in this
repository is preventing the lint-shaped portion of F1 (e.g., catching
inconsistent import order across newly-added routers).

**Configuration:** `pyproject.toml`, section `[tool.ruff]`. Rules enabled: `F`, `E`, `W`, `I`, `S`. Format: enabled.

**Cost:**
- Pre-commit run time: 0.4-0.8 seconds (whole repo).
- CI run time per invocation: <1 second.
- License/subscription: MIT, free.
- Maintenance: <1 hour per quarter (review new rules in releases).

**False-positive rate after calibration:** 1.2% (calibration period 2026-05-15 to 2026-05-22).

**Sunset rule:** Removed if a successor (e.g., Astral ty bundling lint) ships at
1.0 with feature parity. Or if false-positive rate exceeds 5% sustained over
two months without configuration relief.

**Substrate alignment:** H for the F-rule and bandit-derived S-rule subsets;
M for stylistic rules (which encode opinion not truth and are partially
controllable via formatter behavior).

**Replacement candidates considered:** flake8 + black + isort + pyupgrade
(rejected: 5x slower, three configuration files instead of one, ecosystem split).
Pylint (rejected: heavier, slower, and pylint's design encodes more opinion-rather-than-truth than Ruff).
```

**Why this template earns its weight.** Each field maps to a specific failure mode of tooling discipline. *Adopted* and *Calibration end* prevent indefinite warn-only purgatory. *Tier* and *Where it runs* document the orchestration decision so it is reconsiderable later. *Failure mode gated* prevents the tool from being a solution looking for a problem. *Cost* and *Maintenance* prevent the cumulative-tax pattern. *False-positive rate after calibration* is the empirical check on substrate alignment. *Sunset rule* is the discipline that lets tools be removed. *Replacement candidates considered* prevents future contributors from re-adopting tools their predecessors evaluated and rejected.

The template is itself a substrate-adequate artifact at the meta-tooling layer, in the same recursive sense §7 named.

---

## 14. Bridge to the vision paper

The three-tier framing in §4 maps onto the maturity model that is implicit in v2.0.1 of the Software Development Vision but not yet stated explicitly. The mapping is worth lifting into v2.1, because it converts the dark-factory aspiration from an evocative phrase into a defensible architecture and gives outside readers a way to locate any specific component of the stack on a calibrated scale.

**Maturity model, three-tier formulation.**

*Maturity level A (low-stakes):* substrate adequacy at the level of types and contract-shaped tests. Tooling: Ruff, mypy or pyright, Pydantic with typed aliases, OpenAPI Spectral, import-linter, custom AST checks, full-suite CI on every commit. The discipline gate is mechanical conformance plus example-based testing of the happy path. False-positive rate of the tooling: very low. Cost: hours of setup, near-zero per-commit. Failure modes structurally mitigated: F1, F3 partially, F5. This is where most of the CAS codebase will live for the foreseeable future, and it is the level that requires no new skills beyond what disciplined 1982 development required, just enforced mechanically.

*Maturity level B (medium-stakes):* substrate adequacy at the level of properties and behavioral invariants. Tooling: Hypothesis property-based testing, mutmut mutation testing, Semgrep CE custom rules, custom Claude review skills with explicit substrate-rule prompts. The discipline gate is constraint-shaped testing that probes the boundary, plus mutation testing that verifies the test suite is constraining rather than chronicling. False-positive rate: low to moderate, dependent on property authoring craft. Cost: substantial per-property authoring; modest per-run. Failure modes structurally mitigated: F2, F3 fully, F4 partially. This is where consent state machines, jurisdictional resolution, and adjudication invariants belong. Most of the CAS codebase will not need this level; the substrate-recursive layer (the API conformance check, the typed-alias invariants) is the natural starting case.

*Maturity level C (high-stakes, dark-factory):* substrate adequacy at the level of machine-checkable proofs. Tooling: verification-aware specification languages (Dafny, F*, Verus), verified compilation pipelines, formal contracts integrated into the build system. The discipline gate is mechanical proof of correctness against a formal specification. False-positive rate: zero (proofs do not fire spuriously). Cost: substantial per-property specification effort; this is where Lahiri's intent-formalization spectrum reaches its rigorous end. Failure modes structurally mitigated: complete coverage of the specified properties, by definition. This is the destination, not the starting point. For the PIM Health portfolio it would apply to consent-transition kernels, audit-log integrity invariants, and at most a handful of other safety-critical paths. For 95% or more of any codebase, this level is overkill.

**Substrate-recursion observation, formalized.**

The May 8 conversation surfaced a substrate-recursion observation that earns articulation in v2.1. The Formal Substrate constrains domain code, but it must also constrain the structural code that implements the substrate (API shapes, dependency-injection topology, test placement, the substrate's own rule set). Substrate is recursive: every layer that constrains the application is itself an application that needs constraints. The §13 rationale-document template is a substrate-recursive artifact at the meta-tooling layer; the §11 architectural conformance test is a substrate-recursive artifact at the API layer; the §3 failure log is a substrate-recursive artifact at the discipline layer. The maturity model applies at each layer of recursion. A maturity-level-A treatment of the API-shape layer (deterministic conformance checks, AST walkers) is sufficient because the failure modes at that layer admit deterministic gating; a maturity-level-B treatment of the same layer is overkill. Recognizing the recursion is what permits proportionate investment of substrate effort.

**Adequacy-versus-authority distinction.**

The May 8 conversation also surfaced the distinction between substrate authority (where the truth lives, who is allowed to change it) and substrate adequacy (whether the truth, once authoritative, is sufficient to gate the failure modes that matter). Your CLAUDE.md principle #8 establishes authority: Pydantic models derive from JSON Schemas. The May 5 incident demonstrated that authority alone is not sufficient: the schema was authoritative-but-underspecified, type-correct without being range-correct. Adequacy is independent from authority. Lahiri's spectrum corresponds to a spectrum of substrate adequacy at constant authority. v2.1 of the vision paper would benefit from explicit treatment of the two axes: at what authority is each substrate artifact, and at what adequacy. The matrix produces a decisional framework for prioritizing substrate work: low-authority + low-adequacy artifacts are the riskiest; high-authority + low-adequacy artifacts (the May 5 case) are the most insidious because they look settled.

**Recommended additions to v2.1 of the Software Development Vision.**

The bridge produces four candidate additions to v2.1, each of roughly one paragraph:

1. The intent-gap pairing (Lahiri's term as the dual of Formal Substrate; substrate is the cure, the gap is the disease).
2. The adequacy-versus-authority distinction, with the May 5 incident as the worked example.
3. The substrate-recursion observation, with the §13 rationale-document template and the §11 architectural conformance test as worked examples.
4. The three-tier maturity model from above, with at least one footnote indication of where each section of the vision paper currently sits.

These four additions are surgical insertions into v2.0.1's existing structure, not restructurings. None of them disturbs the document's core argument; each strengthens one part of it.

---

## 15. Open questions and gaps

This survey was written under three constraints worth surfacing as caveats. First, I did not personally install or run any of the Tier 1 or Tier 2 tools against the CAS repository; the recommendations are informed predictions, not validated measurements. Step 1 of the §11 adoption sequence (failure log) is the apparatus that converts predictions into measurements. Second, the AI-augmented review (Tier 2) landscape is changing fast enough that any specific vendor benchmark cited here will be outdated within 6 to 12 months; the §6.1 pricing and false-positive figures are May 2026 snapshots. Third, the AI-native CI patterns in §7 are early-research; the recommendation density there is deliberately lower than in Tiers 1-2.

Beyond those constraints, three substantive questions I was unable to resolve in this survey are worth flagging.

**Q1: Is full mypy adoption worth its cost on a 700-file codebase?** Ruff covers the lint-shaped portion of the type story. mypy strict adds pure type-correctness gating, but the cost of annotating an existing untyped-or-partially-typed codebase to mypy strict standard is substantial. The literature pattern is to adopt mypy strict on new modules and gradually backfill; this discipline is hard to maintain in practice. An alternative is to wait for Astral ty 1.0 and adopt that directly, on the rationale that ty's 10-60x speed advantage makes "type-check on every commit" practical in a way mypy does not. The reasoning depends on how soon ty reaches 1.0.

**Q2: Does Spec Kit or BMAD belong in the CAS workflow?** Both are spec-driven development frameworks oriented at AI-first workflows. Spec Kit is closer to "interview-then-implement" (markdown specs that drive code generation); BMAD is closer to "structured agentic workflow" (multi-agent role-played collaboration). Both might compose with your existing CLAUDE.md plus skills approach, but neither maps cleanly onto your formal-substrate architecture, where the JSON Schema is already the executable specification. The honest answer is that I do not know whether adopting one of these adds value beyond what your custom-skills work would produce; this question wants a focused experiment, not a survey conclusion.

**Q3: Where does the property-based testing investment plateau?** The §11 sequence places Hypothesis adoption at step 4 against the typed-alias surface. Beyond that surface, every richer property (e.g., "ingestion is idempotent under retry," "a supersede operation followed by a query returns only the new version") is more valuable but more expensive to author. The question of when to stop investing in additional properties remains open. The Anthropic agentic-PBT paper suggests it may eventually be Claude's job rather than yours; today the authoring work is human-effort-bound.

---

## 16. Reading list and primary sources

**Tools (current documentation, May 2026):**

- [Ruff documentation](https://docs.astral.sh/ruff/): Astral's official documentation site, including the rules catalog and 2026 style guide.
- [Astral ty](https://docs.astral.sh/ty/): beta type checker, current 0.0.34.
- [Hypothesis 6.152.1 documentation](https://hypothesis.readthedocs.io/): the canonical property-based testing reference.
- [mutmut on PyPI](https://pypi.org/project/mutmut/): current mutation tester recommendation.
- [Semgrep Community Edition](https://semgrep.dev/products/community-edition/): free tier specifications and rule library.
- [import-linter documentation](https://import-linter.readthedocs.io/en/stable/): architectural contract enforcement.
- [pre-commit framework](https://pre-commit.com/): pre-commit hook orchestration.
- [datamodel-code-generator on GitHub](https://github.com/koxudaxi/datamodel-code-generator): Pydantic from JSON Schema.

**AI-augmented review vendors:**

- [CodeRabbit](https://www.coderabbit.ai/): vendor site, including pricing and integration.
- [Greptile](https://www.greptile.com/): whole-repo knowledge graph approach.
- [Diamond by Graphite](https://graphite.com/): Graphite-native review.

**Foundational research:**

- Maaz, M., DeVoe, L., Hatfield-Dodds, Z., Carlini, N. (2026). *Finding bugs across the Python ecosystem with Claude and property-based testing*. Anthropic Red Team. [Blog post](https://red.anthropic.com/2026/property-based-testing/) | [arXiv paper](https://arxiv.org/abs/2510.09907) | [GitHub repository](https://github.com/mmaaz-git/agentic-pbt)
- Lahiri, S. (2026). *Intent Formalization: A Grand Challenge for Reliable Coding in the Age of AI Agents*. Microsoft Research. [arXiv:2603.17150](https://arxiv.org/abs/2603.17150)
- Ford, N., Parsons, R., Kua, P. (2017). *Building Evolutionary Architectures*. O'Reilly. (Architectural fitness functions, the canonical reference.)
- Meyer, B. (1992). *Applying Design by Contract*. IEEE Computer 25(10). (Foundational design-by-contract paper.)
- ThoughtWorks (April 2026). *Technology Radar Vol. 34*. [thoughtworks.com/radar](https://www.thoughtworks.com/radar). See the §F2 commentary on mutation testing, AI-aided test-first development, and the "Superpowers" plugin.

**Adjacent context:**

- Anthropic. *Claude Code Best Practices*. [code.claude.com/docs/en/best-practices](https://code.claude.com/docs/en/best-practices)
- HumanLayer. *Writing a good CLAUDE.md*. [humanlayer.dev/blog/writing-a-good-claude-md](https://www.humanlayer.dev/blog/writing-a-good-claude-md)
- Augment Code. *AI Spec-Driven Development Workflows*. [augmentcode.com/guides/ai-spec-driven-development-workflows](https://www.augmentcode.com/guides/ai-spec-driven-development-workflows)

**CAS-internal references:**

- `CAS_Project_Tracker.md` (root directory): current project status.
- `docs/cas_adr_store.json`: architectural decision records.
- `docs/fs/`: Formal Substrate (JSON Schemas + OpenAPI specs).
- SAGE-resident `SWDevVision v2.0.1` (`pim_health` vault, document `c18567cf_swdevvision`): primary vision-paper artifact this survey supports.

---

*End of survey body. Executive summary at §1 will be written from the conclusions of §§3-15.*

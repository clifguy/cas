# CAS checks procedure

**INACTIVE proposal.** See [authority transition](authority-transition.md).

Use the intended worktree's guide and locked environment, not imports from another
checkout. Provision with `uv sync --locked --extra test --extra mlx --extra dev --extra ocr`;
invoke `.venv/bin/python`. Preserve the disposable database harness and explicit TCP
SAGE_TEST_PG_DSN; never point tests at live vault storage. Real Qwen tests are relevant
only to adapter changes and run serially under their documented opt-in.

Before planning tests, trace CI and hooks to invoked scripts, not only tests/:
`.github/workflows/ci.yml` invokes substrate_changes and test-surface comparison.
Read relevant invariants and allowlists, including test-removal exceptions, without
editing unrelated exceptions. A downstream invariant with no allowlist matters too.
Pair defect cases with controls; remove each distinct setup block and mutate actual
protected behavior to show the expected failure, then restore exactly. Prose checks
need observed disposable decisions/effects; string presence proves retention only.

Apply the canonical `.claude/skills/cas-code-review/SKILL.md` to the complete candidate,
visiting F1–F5, G1 and P1. Retain that binding; do not relocate its content here.
Azure surfaces additionally require the existing Azure review. Run applicable Ruff,
pre-commit, instruction-pointer/public-posture checks, owning-package TypeScript lint
and build, schema/model and shared-version parity, and the required CI checks.
Documentation-only candidates use source/decision validation and relevant repository
checks; no production smoke or unrelated real-model run is implied.

Record applicability as well as results: skipped hooks are not acceptance. Selectively
stage authorized paths and both rename endpoints; preserve unrelated dirty work.
If hooks change the candidate, inspect the change, revalidate affected behavior and
refresh candidate identity. Required forge checks must cover the exact final head;
record CI merge-tree identity where the forge tests a synthesized merge. A green
different-head run or unavailable forge query is not a pass.

## Conditional dependency gates

Evaluate the complete candidate file set, including both sides of a rename and
authorized new files. The Azure binding selects `infra/*`, `deploy/*`,
`.github/workflows/infra.yml` and `.github/workflows/build-images.yml`. Resolve and
execute the existing Azure deploy-review skill against that same candidate. If
the required skill is unavailable, report the matching operation blocked; the
presence of this supplement is not an Azure review. An ordinary documentation
candidate does not select that binding.

The real-model binding selects `sage/adapters/abstraction_qwen3.py` and
`sage/adapters/embedding_nomic.py`. Resolve the worktree's documented runtime and
run its opt-in adapter tier serially with `SAGE_TEST_REAL_MODELS=1`; check no
competing model run is active. A rename out of either path still selects the gate.
Missing weights, runtime or required capability is blocked validation, never a
successful skip. Fixture tests validate selection without loading real weights.

CI's full pytest suite, substrate classification and active-test-surface comparison
are separate obligations. Preserve `KNOWN_TEST_REMOVALS` and other unrelated
exceptions. A focused conformance run does not replace the full required suite or
the exact-head forge result.

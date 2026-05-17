# Branch Protection for `main`

This document captures the intended branch-protection configuration for the `main` branch of the `cas` repository. It is the version-controlled, substrate-recursive home for a methodology setting that GitHub otherwise keeps only in the web UI.

## Intent

- The `main` branch is protected from direct push.
- Changes reach `main` only through pull requests.
- Pull requests must pass all required status checks (below) before merge.
- Pull requests must receive at least one approving review from a code owner declared in [`.github/CODEOWNERS`](../../.github/CODEOWNERS) before merge.

## Required status checks

The following CI jobs from [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) are required to pass on every pull request targeting `main`:

| Job | Purpose |
|---|---|
| `test` | Full pytest suite with coverage (fail-under floor enforced). |
| `lint` | Ruff `check` and `format --check` over the repo. |
| `lint-imports` | `import-linter` contract enforcement. |
| `gitleaks` | Secret-scanning over the full commit history. |

These four names match the `jobs:` keys in `ci.yml` verbatim. If a job is renamed, this document and the corresponding GitHub branch-protection rule must be updated together.

## Required reviews

- At least one approving review from a CODEOWNERS member is required.
- This is a single-developer repository today. The CODEOWNERS file lists `@clifguy` as the sole owner. The required-review setting therefore documents intent for the future multi-contributor case; it is not active gating today (see *Activation status*).

## Activation status

**The intent documented above is not currently enforced by GitHub.** Branch protection on private GitHub repositories requires a GitHub Pro (or higher) plan. This repository is private and on the free plan; `gh api repos/clifguy/cas/branches/main/protection` returns HTTP 403 with the message "Upgrade to GitHub Pro or make this repository public to enable this feature."

The intent here becomes enforceable, and should be activated immediately, under either of the following conditions:

- The repository is made public.
- The repository is upgraded to a GitHub plan that supports branch protection on private repos.

Until then, the discipline this document describes is honored procedurally: PR-based workflow, do not push directly to `main`.

## Reconciliation procedure

When activation conditions are met, apply the documented intent in the GitHub UI:

1. Navigate to **Settings → Branches → Branch protection rules → Add rule**.
2. Set **Branch name pattern** to `main`.
3. Enable **Require a pull request before merging**.
   - Under that, enable **Require approvals** with a minimum of **1**.
   - Enable **Require review from Code Owners**.
4. Enable **Require status checks to pass before merging**.
   - Enable **Require branches to be up to date before merging**.
   - In the status-check search box, add each of: `test`, `lint`, `lint-imports`, `gitleaks`.
5. Enable **Do not allow bypassing the above settings**.
6. Save.

If this document changes (a new status-check job is added, a job is renamed, the review policy changes), re-apply the corresponding boxes in the UI in the same session. Treat the document as the source of truth and the UI as the reflection.

## Rationale

This file exists because CAS Tooling Conventions establishes a substrate-recursive principle: methodology discipline should live where it survives any single GitHub session, not in a web-UI configuration that disappears from the agent's view the moment the tab closes. Branch protection is methodology — it encodes a discipline gate on merges — and so its configuration belongs in version control alongside the CI workflow it gates.

The vault-resident steering document that establishes this principle is *CAS Tooling Conventions* (`doc_type=steering_document`, id `aadd731e_cas_tooling_conventions`). See also the gap analysis in *CAS SDLC Methodology Review (Pre-T-0015)* §4.7 (id `55d6bf22_cas_sdlc_methodology_review_pre_t_0015`) that motivated the ticket (T-0067) under which this file was created.

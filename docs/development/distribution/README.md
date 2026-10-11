# CAS workflow distribution

Build with the compatible shared packager, `--declaration docs/development/distribution/composition.json --source cas=<candidate-root>`. The shared root is the packager checkout. This adds next and one shared smoke-test router plus complete project-owned deploy, Azure-review and unversioned batch entry-point components. Canonical procedures and references remain in this CAS repository and must be delivered at the same reviewed revision; the skill installer does not install repository documents. Their required bindings fail closed when missing. The historical repository-overlay declaration under proposed is not the skill-folder installer input.

The complete bundle retains the eleven shared core skills and the independently managed personal SAGE dependency selected below. Batch uses the supported null-version exception; historical batch version numbers are not a new release. No generated installed bundle is tracked. Build/verify and a disposable install/rollback establish packaging, not authority activation.

## Portable specialized runtime closure

The actual-host selector is `operations/references/runtime.md` (under
`docs/development/`), with canonical `codex-runtime.md` and `claude-runtime.md`
adapters beside it. All four procedures and the three distribution entry points
enter that selector. Use a compatible complete shared bundle containing
`project-policy/references/runtime.md`, both shared adapters and its reviewer asset.
Both Codex and Claude distribution layouts support guide-directed selection; layout
is not host identity. Missing repository documents block the dependent operation.

The primary repository already has an active installation recorded by its ignored
receipt. Historical cutover text describes that original transition; a new candidate
does not repeat activation or replace the active bundle. Stage and verify both layouts,
rehearse replacement/rollback on disposable copies, and record exact source revisions,
manifests, runtime hashes, host evidence and remaining live gates. A later replacement
requires fresh destination inventories, a complete plan and named owner authorization.
Preserve unrelated skills, CPML-finalize, reviewer assets and canonical code review;
verify restored bytes/modes/inventory and never overwrite later edits to force rollback.
Actual host trials are distinct from static checks, synthetic provider results and
live deployment/cohort/service acceptance. Packaging alone proves none of those.

## Personal SAGE dependency

New CAS consumer packages select the shared personal dependency contract. Use the
reviewed shared source at `ae5af582bfaf09cbb82526ea37d26908acd2e660` and the
CAS package from `545a65c0037855d8c96552a674ff1a9f99558937`, whose manifest
SHA-256 is `94f94e10e26f5cf4d98ef2133d0c54ed85c640fceddfa197bdb592520c7f0cfb`.
Verify the complete package before building. The trusted digest comes from this
reviewed declaration, never from an untrusted destination manifest.

From the compatible shared source, with absolute paths supplied explicitly:

```sh
python scripts/package_workflow_bundle.py "$OUTPUT" --platform codex \
  --declaration "$CAS/docs/development/distribution/composition.json" \
  --source "cas=$CAS" --sage-personal-manifest "$SAGE_PACKAGE/manifest.json" \
  --sage-manifest-sha256 94f94e10e26f5cf4d98ef2133d0c54ed85c640fceddfa197bdb592520c7f0cfb
python scripts/package_workflow_bundle.py "$OUTPUT" --verify
```

Build a separate output with `--platform claude`. These two personal-dependency
arguments are required for this adoption: omitting them selects the shared
builder's compatibility default, which is not the new CAS candidate. Confirm
`sage_capability.mode` is `personal`, its source commit and trusted digest match,
the complete CAS roster is present, and no `sage` component is owned by the
workflow package. Composition's host prerequisites describe authenticated access
and review capability; the package arguments express the personal dependency.
Do not hand-edit a generated manifest to select it.

The standalone CAS installer owns `skills/sage` and `.skill-install`; the shared
workflow installer owns its separate consumer transaction and reviewer asset.
The bundled transfer entry point delegates to the selected personal skill's
transfer reference. It remains present for complete dependency closure.

For each actual runtime, run the installed bundle's
`project-policy/scripts/sage_capability.py --bundle /absolute/installed/skills
--runtime codex --personal-root /absolute/verified/personal-root`, substituting
`claude` for that runtime and an explicit supported `--target-kind` when required
by its installation layout. The preflight must return
`personal_verified_live_preflight_required` and all seven verified instruction
paths. Then perform the selected skill's live preflight. A missing installation,
untrusted identity, pending transaction or competing project copy blocks use;
there is no fallback to compatibility.

This is source adoption and package preparation. Existing installations and
untransitioned worktrees continue using their receipt-selected compatibility
bundle and exact installed `sage-operations` plus bundled transfer. Upgrade only
named consumers under separate cutover authorization, refresh their activation
receipts with the exact installed identities, and retain compatibility while any
consumer needs it. Canonical review and repository/vault policy remain required.
See [migration and acceptance](../../sage-skill/migration.md).

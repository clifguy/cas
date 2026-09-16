# CAS release classification procedure

Selected candidate procedure. The required activation receipt blocks operational use until the coordinated cutover; see [authority transition](authority-transition.md).

This is a required CAS supplement for kickoff, commit, disposition and authorized
merge. Its existence does not configure other projects. Read the current live CAS
Release Classification authority and [substrate release procedure](../process/substrate-releases.md).
Patch is the default. Surface minor immediately for capability, caller-adaptation
or operator-adaptation changes; do not infer classification solely from file names.

Planning predicts the class and category. At commit run the actual classifier:

```sh
.venv/bin/python -m scripts.substrate_changes check --base <base> --head <candidate>
```

Resolve both revisions and record the effective merge base. `--head` is a revision;
omitting it reads the working tree, not the index. A staged candidate that differs
from the working tree must be materialized as a revision in an isolated disposable
checkout (including required records), or checked after selective commit with exact
tree equivalence to the intended index. Do not claim an unstaged working-tree check
validated a different staged candidate. Record base, candidate/tree identity, command,
result and equivalence evidence. A missing required classifier blocks classification;
it does not trigger release work or adoption of a substitute by script presence.

Contract changes require the existing schema-conformant change record under
`docs/fs/changes/`; use the authoring reference there. The classifier cannot replace
review of caller docstrings, tool annotations or operator behavior. Keep the
`Release classification: <patch | minor> — <reason/category>` PR line current after
every remediation, including a patch-to-minor expansion. Surface changed classification
when observed, not at a later checkpoint.

On separately authorized merge, freshly fetch/resolve the default branch and run:

```sh
.venv/bin/python -m scripts.substrate_changes status --ref <default-branch-sha>
```

Report that exact revision's release status, including concurrent merged records or
an untagged release. A failed fetch/read is unknown; stale PR text is not release
status. This procedure never invokes release-prepare or release-tag. Those are
separately authorized owner actions.

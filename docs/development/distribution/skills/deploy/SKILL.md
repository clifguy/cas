---
name: deploy
description: Route explicitly authorized CAS deploy work to the project-owned procedure.
metadata:
  version: 1.0.0
---

# CAS deploy

Distribution source entry point. Operational use requires the coordinated activation receipt.
Resolve the intended project root, then read the compatible shared project-policy
skill and resolve operation `deploy`. Require a selected, required binding
with responsibility `deploy-procedure` pointing to
`docs/development/operations/deploy.md`, resolved from that project root.
Read that complete canonical procedure and its runtime/dependency references.
Select the actual executing host through
`docs/development/operations/references/runtime.md`, using the selected complete
shared bundle; installation layout does not choose the host adapter.
Missing binding, procedure, authority or capability halts before probes or dispatch
with the missing item and needed configuration named. Do not use this source file
as implicit activation or fall back to a historical installed procedure.

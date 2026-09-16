---
name: batch
description: Route explicitly authorized CAS batch work to the project-owned procedure.
metadata:
  release_status: candidate-unversioned
---

# CAS batch

Distribution source entry point. Operational use requires the coordinated activation receipt.
Resolve the intended project root, then read the compatible shared project-policy
skill and resolve operation `batch`. Require a selected, required binding
with responsibility `batch-procedure` pointing to
`docs/development/operations/batch.md`, resolved from that project root.
Read that complete canonical procedure and its runtime/dependency references.
Missing binding, procedure, authority or capability halts before probes or dispatch
with the missing item and needed configuration named. Do not use this source file
as implicit activation or fall back to a historical installed procedure.

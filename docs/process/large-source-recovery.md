# Hosted SAGE restoration and ingest reconciliation

This is a reviewable procedure, not authorization to deploy, reactivate, write to a production vault, or close work. Keep the affected application revision deactivated until the candidate and recovery plan pass their prerequisite checks.

## Input work bound

Tokenization has a finite work bound of 32,768 Unicode characters per input,
including a passage's heading and separator. Large ordinary sections are divided
without discarding text. If an exceptionally large heading, combined crowded
section, document surface, or semantic query cannot fit, processing fails before
calling the tokenizer. Preserve the recorded error and source; shorten or divide
the offending context before a supported retry. A model's later token truncation
does not protect the allocation needed to tokenize its original input.

This narrows the formerly unbounded input path and is a caller-adaptation minor.
The immediate security-exposure exemption closes a reproduced process-exhaustion
weakness; it does not authorize service reactivation.

## Before reactivation

1. Resolve the intended deployment, application, active revision set, image/build, database, and target vault from current operator configuration and live inventory. Record the intentionally stopped state. Preserve retained sources, original upload/completion receipts, hashes, caller metadata, and uncertain outcomes.
2. Validate the exact candidate image with the real pinned tokenizer/model under the serving CPU/memory limits. Include headingless dense CSV, quoted multiline fields, long fields, oversized heading context, concurrent lightweight requests, and an actual interrupted-ingest restart. Record architecture, abstraction/source-store bindings, input sizes, concurrency, peak process/container memory, scheduling-inclusive latency, job outcomes, and container exits. A synthetic local run does not establish hosted acceptance.
3. Review the repair's treatment of whole-section tokenization, model input construction, and asynchronous work queues. A bounded sizing predicate alone is insufficient when an exceptional passage can bypass division and reach the model tokenizer whole.
4. Obtain the separate production deployment/reactivation authorization. Record the stop conditions and rollback procedure. Returning to the known failing image with the same pending work is not a recovery strategy.

## Controlled restoration

Deploy/reactivate only the reviewed candidate. Verify the serving build, ready replicas, expected vault load, gateway discovery and authenticated inventory/read/search using the target's live served contract. Observe CPU, peak memory, request latency, restart counts, and the recovered backlog. Stop/deactivate again if crashes, resource-limit kills, or stalled requests recur. Preserve evidence rather than repeatedly restarting.

After backlog settlement, observe at least 30 minutes of representative traffic and one separately authorized controlled restart/recovery check. Obtain representative client checks. The app-level Running/Healthy labels alone do not prove that a container is ready.

## Reconciliation ledger

Create one row for every source whose completion is uncertain. Record its original filename, exact bytes and SHA-256, intended metadata, upload and completion receipt identities, all persisted document/version matches, retained-source status, lifecycle, pipeline status/error, projection/search checks, repair action, and final evidence.

Identify the document startup recovery resumed. Resolve every matching record and its complete lineage through supported SAGE reads before replay. Upload success proves byte delivery, not document creation; completion errors and expired tokens do not establish absence. Reuse the correct existing record and supported repair path. Retry only after evidence establishes that replay is safe. Preserve intended metadata/provenance and valid lineages. Do not use direct database edits, blanket force-ingest, or duplicate replacement records to hide uncertainty.

Each source needs retained-source integrity, exactly one intended logical record unless a reviewed existing lineage explains otherwise, successful terminal pipeline processing, and representative early/middle/late projection/search checks. A failed/interrupted job, missing source, duplicate, or unknown outcome remains incomplete. Deployment, merge, upload receipts and ingest receipts alone do not satisfy this acceptance. Closure remains an explicit owner decision.

# Codex specialized workflow mechanics

First execute the selected complete shared bundle's
`project-policy/references/codex-workflow-runtime.md`. The common
[runtime contract](runtime.md) and canonical operation retain all policy obligations.

## Execution and waiting

Anchor each execution to the exact absolute worktree using `workdir` or `git -C`.
Long commands may return an execution session id: poll with the exposed session
continuation tool until terminal completion. A function-cell wait accepts only its
own running cell id. Keep waits bounded to 60 seconds and retain the single operation
deadline. Do not assume ending a turn schedules a wakeup. Stop the local watcher on
cancellation; remote CI/deployment cancellation requires separate authorization.

## Implementation workers

For the common batch worker lifecycle, use `collaboration.spawn_agent` with
`fork_turns="none"`, a descriptive task name and the exact scoped prompt as `message`.
Omit model/effort overrides for ordinary implementation. Record the returned agent id;
use exposed collaboration status/wait tools and retain terminal completion. Send a
focused continuation through `followup_task` only when supported; a running worker
may receive a message without spawning another. Use actual interrupt/status facilities
and inspect execution sessions before any takeover. Unknown state parks the task.

## Reviewers

Implementation defaults do not configure independent reviewers. Apply the shared
Codex same-model/next-supported-higher-effort contract using the originating author's
exposed or explicitly supplied baseline. Record requested and observed settings
separately; unavailable required capability blocks review pending an owner alternative.

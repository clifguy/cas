# Claude Code specialized workflow mechanics

First execute the selected complete shared bundle's
`project-policy/references/claude-workflow-runtime.md`. The common
[runtime contract](runtime.md) and canonical operation retain all policy obligations.
A guide-selected `.agents/skills` layout does not select Codex tools.

## Execution and waiting

Use the exposed Claude Code shell/execution tool with the explicit absolute worktree.
For long-running local watcher processes use the host's supported background/session
facility only when its actual contract exposes one; retain its handle and obtain
terminal completion through the supported status/output mechanism. Do not invent
TaskOutput, assume a background argument, or translate Codex tools. A supported bounded
foreground poll is acceptable; preserve the same single deadline across invocations,
pass required values explicitly, and keep waits at most 60 seconds. If neither safe
waiting nor cancellation is available, park the affected operation before dispatch.
Cancellation stops only the local watcher; do not cancel remote CI or deployment.

## Implementation workers

Use the actually exposed independent Agent mechanism with a fresh context and only
the explicit batch worker scope. Verify the host supports no inherited author history;
a conversation fork or a claim in the prompt does not prove it. Ordinary implementation
settings do not select the review agent. Retain the returned dispatch handle and await
actual terminal completion using supported facilities. Check the common lifecycle's
status and artifact identity before advancing. Use supported continuation/status/stop
facilities only when exposed; never invent resume or stop arguments. If a handle cannot
be continued, retain partial evidence and park the task until stopped-writer verification
permits a scoped fresh session. No takeover while the worker or its sessions may write.

## Reviewers

Use the matching registered reviewer definition from the selected shared bundle and
its Claude adapter. Verify definition registration and actual independent dispatch.
Keep requested model/effort/isolation and observed settings separate. Missing required
registration or unsupported configuration blocks review pending an explicit owner
alternative. Do not silently use an implementation worker as an independent reviewer.

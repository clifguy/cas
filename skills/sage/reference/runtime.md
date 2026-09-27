# Claude and Codex runtime guidance

This skill uses plain Markdown and relative references; it does not depend on
a callable Skill tool, Claude-only shell APIs, assumed PlanMode transitions,
or a fixed filesystem root. Load the complete selected folder, including
writing and transfer references, and use the current host's available tools.
Do not silently select an older same-named installation on another host.

Resolve the requested deployment using configured endpoints and live vault
inventory. Connected local MCP tools do not imply hosted access. When a vault
is absent from one deployment, check the other configured deployment before
reporting it unavailable. Read the target's live tool contract through the
available authenticated client/API; do not invent signatures, endpoints or
credentials. Missing capability blocks its dependent action, not unrelated
local work. A caller workflow's schema/uniqueness requirements remain required;
facets do not substitute for declared schemas or allocation guarantees.

Use the host's execution/file tools for byte movement outside the model stream.
Respect actual filesystem/network permissions; use its approval mechanism if
needed. Prior task authorization does not bypass sandbox permissions. A
permission denial or uncertain result is not a reason to switch to direct
vault-file edits. Preserve useful read-only progress and report the exact gap.

For long-running tools, await the supported completion mechanism in bounded
intervals and keep the task active until the result or a clear partial status.
Do not assume an unfinished tool call will wake a completed conversation.
Reconcile external writes before retrying; never label dispatch as completion.

A draft readable from Claude is not proof of Codex installation or discovery.
Verify complete-folder packaging, selected path, actual tool access and fresh
host behavior before replacing an existing consumer dependency. Local fixture
trials demonstrate only those tested decisions, not live service acceptance.

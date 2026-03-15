#!/bin/bash
# Stop hook: structured decision capture + session summary.
# Global — works for any project with .memory/ dir.
# NOTE: Only fires in CLI. VSCode extension discards hook output.
[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0

# VSCode extension runs hooks but discards output — skip entirely
[ -z "${VSCODE_PID:-}" ] && [ -z "${VSCODE_IPC_HOOK:-}" ] \
    && [ "${TERM_PROGRAM:-}" != "vscode" ] \
    || exit 0

[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] || exit 0

# Guard unset session ID to avoid marker collisions
[ -n "${CLAUDE_SESSION_ID:-}" ] || exit 0
SAFE_SID="${CLAUDE_SESSION_ID//[^a-zA-Z0-9_-]/_}"
MARKER="/tmp/.claude-mem-reminded-${SAFE_SID}"

if [ -f "$MARKER" ]; then
    exit 0
fi

touch "$MARKER"

cat << 'MSG'
SESSION END — persist what you learned:

1. DECISIONS: If you chose between approaches or made architectural
   calls, persist each with create_decision now:
     create_decision({
       title: "what was decided",
       rationale: "why this approach",
       alternatives: ["rejected option -- reason"],
       scope: "affected code area",
       related_entities: ["ComponentName"]
     })

2. OUTCOMES: If you revisited a prior decision and saw it succeed or
   fail, close the loop:
     update_decision_outcome({
       decision_name: "prior decision title",
       outcome: "successful|failed|revised",
       lesson: "what we learned"
     })

3. WARNINGS: If you found gotchas, fragile code, or foot-guns:
     create_entities([{
       name: "filename.py",
       entityType: "file-warning",
       observations: ["[WARNING] description of the gotcha"]
     }])

4. PATTERNS: If you discovered reusable knowledge (conventions,
   integration points, API quirks), persist as entities + relations.

Skip any that don't apply. Only persist what's genuinely useful.
MSG
exit 0

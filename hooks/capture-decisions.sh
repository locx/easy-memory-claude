#!/bin/bash
# Stop hook: structured decision capture + session summary.
# Global — works for any project with .memory/ dir.
[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0
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
If this session involved trade-off evaluations, approach selections,
or architectural decisions, persist them with create_decision:

  create_decision({
    title: "what was decided",
    rationale: "why this approach",
    alternatives: ["rejected option -- reason"],
    scope: "affected code area",
    related_entities: ["ComponentName"]
  })

For file-specific warnings (gotchas, fragile areas, known issues):

  create_entities([{
    name: "filename.py",
    entityType: "file-warning",
    observations: ["[WARNING] description of the gotcha"]
  }])
MSG
exit 0

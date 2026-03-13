#!/bin/bash
# Stop hook: one-time-per-session reminder to persist decisions.
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

echo "Reminder: If this conversation produced important decisions, architectural patterns, or user preferences worth preserving, use the MCP memory tools (create_entities, create_relations, add_observations) to store them in the knowledge graph."
exit 0

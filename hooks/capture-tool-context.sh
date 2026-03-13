#!/bin/bash
# PostToolUse hook: capture observations from relevant tool calls.
# Passes stdin via temp file to Python (no shell interpolation).
# Append-only writes (no full graph rewrite).
# Caps stdin read to 50KB.
# Throttled: skips if last capture was <30s ago.

[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0
[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] || exit 0
[ -n "${CLAUDE_SESSION_ID:-}" ] || exit 0

MEMORY_DIR="${CLAUDE_PROJECT_DIR}/.memory"
GRAPH="${MEMORY_DIR}/graph.jsonl"
# Create graph if missing — resilient bootstrapping
[ -f "$GRAPH" ] || touch "$GRAPH"

SAFE_SID="${CLAUDE_SESSION_ID//[^a-zA-Z0-9_-]/_}"

# Portable stat helper: get file mtime as epoch seconds
_file_mtime() {
    date -r "$1" +%s 2>/dev/null \
        || stat -c%Y "$1" 2>/dev/null \
        || python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$1" 2>/dev/null \
        || echo 0
}

# Throttle: skip if last capture was <30s ago
MARKER="/tmp/.claude-mem-toolcap-${SAFE_SID}"
if [ -f "$MARKER" ]; then
    NOW=$(date +%s)
    LAST=$(_file_mtime "$MARKER")
    ELAPSED=$(( NOW - ${LAST:-0} ))
    if [ "$ELAPSED" -lt 30 ]; then
        exit 0
    fi
fi
# Save stdin to temp file (capped at 50KB), pass path to Python
TMPINPUT=$(mktemp /tmp/.claude-toolcap-XXXXXX)
chmod 600 "$TMPINPUT"
trap 'rm -f "$TMPINPUT" 2>/dev/null' EXIT
head -c 51200 > "$TMPINPUT"

# Use standalone .py for bytecode caching (.pyc)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "${SCRIPT_DIR}/capture_tool_context.py" "$TMPINPUT" "$GRAPH"
PY_EXIT=$?

# Update throttle marker only after successful capture
if [ "$PY_EXIT" -eq 0 ]; then
    touch "$MARKER"
fi
exit 0

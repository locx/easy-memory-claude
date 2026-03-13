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
[ -f "$GRAPH" ] || exit 0

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

# Python reads from file — no shell variable interpolation
python3 - "$TMPINPUT" "$GRAPH" << 'PYEOF'
import json, os, sys, time

input_path = sys.argv[1]
graph_path = sys.argv[2]

try:
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(0)

tool = data.get('tool_name', '')
if tool not in ('Edit', 'Write', 'Bash', 'NotebookEdit'):
    sys.exit(2)  # non-matching tool — don't update throttle marker

# Build a terse observation
if tool == 'Edit':
    path = data.get('tool_input', {}).get('file_path', '?')
    obs = f'Edited {os.path.basename(path)}'
elif tool == 'Write':
    path = data.get('tool_input', {}).get('file_path', '?')
    obs = f'Created/wrote {os.path.basename(path)}'
elif tool == 'Bash':
    cmd = str(data.get('tool_input', {}).get('command', ''))[:80]
    obs = f'Ran: {cmd}'
else:
    obs = f'{tool} used'

ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
observation = f'[{ts}] {obs}'

# Append-only — just add a new line to the graph.
# The session-activity entity accumulates observations as
# separate entity lines. maintenance.py consolidation will
# merge them on next run (same name + type = merge).
entry = json.dumps({
    'type': 'entity',
    'name': 'session-activity',
    'entityType': 'activity-log',
    'observations': [observation],
    '_created': ts,
    '_updated': ts,
}, separators=(',', ':'))

try:
    with open(graph_path, 'a', encoding="utf-8") as f:
        f.write(entry + '\n')
except OSError:
    pass
PYEOF
PY_EXIT=$?

# Update throttle marker only after successful capture
if [ "$PY_EXIT" -eq 0 ]; then
    touch "$MARKER"
fi
exit 0

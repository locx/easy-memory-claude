#!/bin/bash
# SessionStart hook: run maintenance + smart memory recall.
# Global — works for any project with .memory/ dir.
# Runs in both CLI and VSCode (maintenance is always useful).

[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0

MEMORY_DIR="${CLAUDE_PROJECT_DIR}/.memory"

# Skip if project has no memory setup
[ -d "${MEMORY_DIR}" ] || exit 0

# Run maintenance regardless of environment (throttled internally to 1x/day)
if [ -f "${HOME}/.claude/memory/maintenance.py" ]; then
    ERR_LOG="${MEMORY_DIR}/maintenance.err"
    # Rotate error log if >100KB
    if [ -f "$ERR_LOG" ]; then
        ERR_SIZE=$(wc -c < "$ERR_LOG" 2>/dev/null | tr -d ' ') || ERR_SIZE=0
        if [ "${ERR_SIZE:-0}" -gt 100000 ] 2>/dev/null; then
            mv -f "$ERR_LOG" "${ERR_LOG}.old" 2>/dev/null || true
        fi
    fi
    # Run with timeout to avoid blocking SessionStart; background and wait briefly
    timeout 5 python3 "${HOME}/.claude/memory/maintenance.py" \
        "${CLAUDE_PROJECT_DIR}" 2>>"$ERR_LOG" &
    MAINT_PID=$!
    # Wait up to 5s; if still running let it finish in background
    wait "$MAINT_PID" 2>/dev/null || true
fi

# Smart recall — scored entities, compact, with relations + stats
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
if [ -f "${SCRIPT_DIR}/smart_recall.py" ]; then
    RECALL_OUT=$(python3 "${SCRIPT_DIR}/smart_recall.py" "${MEMORY_DIR}" 2>/dev/null)
    RECALL_EXIT=$?
    if [ $RECALL_EXIT -ne 0 ] || [ -z "$RECALL_OUT" ]; then
        echo "Memory: use \`\$HOME/.claude/memory/mem search <query>\` or \`\$HOME/.claude/memory/mem recall <query>\` for details."
    else
        echo "$RECALL_OUT"
    fi
else
    echo "Memory: use \`\$HOME/.claude/memory/mem search <query>\` or \`\$HOME/.claude/memory/mem recall <query>\` for details."
fi

exit 0

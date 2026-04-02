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
    python3 "${HOME}/.claude/memory/maintenance.py" \
        "${CLAUDE_PROJECT_DIR}" 2>>"$ERR_LOG" || true
fi

# Auto-sync native memory into graph (throttled to 1x/day via marker)
SYNC_MARKER="${MEMORY_DIR}/.last-native-sync"
if [ -f "${HOME}/.claude/memory/memory-cli.py" ]; then
    SYNC_AGE=999999
    if [ -f "$SYNC_MARKER" ]; then
        SYNC_MTIME=$(stat -f %m "$SYNC_MARKER" 2>/dev/null || stat -c %Y "$SYNC_MARKER" 2>/dev/null || echo 0)
        NOW_TS=$(date +%s)
        SYNC_AGE=$(( NOW_TS - SYNC_MTIME ))
    fi
    if [ "$SYNC_AGE" -gt 86400 ]; then
        python3 "${HOME}/.claude/memory/memory-cli.py" \
            --memory-dir "${MEMORY_DIR}" sync >/dev/null 2>&1 || true
        touch "$SYNC_MARKER"
    fi
fi

# Smart recall — scored entities, compact, with relations + stats
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
if [ -f "${SCRIPT_DIR}/smart_recall.py" ]; then
    python3 "${SCRIPT_DIR}/smart_recall.py" "${MEMORY_DIR}" 2>/dev/null
else
    echo "Memory: use \`mem search <query>\` or \`mem recall <query>\` for details."
fi

exit 0

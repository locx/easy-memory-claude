#!/bin/bash
# SessionStart hook: run maintenance + smart memory recall.
# Global — works for any project with .memory/ dir.

[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0

# VSCode extension runs hooks but discards output — skip entirely
[ -z "${VSCODE_PID:-}" ] && [ -z "${VSCODE_IPC_HOOK:-}" ] \
    && [ "${TERM_PROGRAM:-}" != "vscode" ] \
    || exit 0

MEMORY_DIR="${CLAUDE_PROJECT_DIR}/.memory"

# Skip if project has no memory setup
[ -d "${MEMORY_DIR}" ] || exit 0

# Run maintenance (throttled internally to 1x/day)
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

# Smart recall — scored entities, compact, with relations + stats
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
if [ -f "${SCRIPT_DIR}/smart_recall.py" ]; then
    python3 "${SCRIPT_DIR}/smart_recall.py" "${MEMORY_DIR}" 2>/dev/null
else
    # Fallback if smart_recall.py missing
    echo "Memory tools: semantic_search_memory | traverse_relations | create_entities | create_decision | graph_stats"
fi

exit 0

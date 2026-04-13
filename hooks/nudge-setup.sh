#!/bin/bash
# SessionStart hook: one-time nudge when a project lacks memory setup.
# Fires once per project per day — not noisy.

# Only nudge if memory infra is installed but project is NOT initialized
[ -f "${HOME}/.claude/memory/maintenance.py" ] || exit 0
[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0
[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] && exit 0

# Throttle with portable hash (cksum is POSIX — always available)
_hash_dir() {
    if command -v md5 &>/dev/null; then
        md5 -q -s "$1"
    elif command -v md5sum &>/dev/null; then
        echo -n "$1" | md5sum | cut -d' ' -f1
    else
        echo -n "$1" | cksum | cut -d' ' -f1
    fi
}

# Portable stat helper: get file mtime as epoch seconds
_file_mtime() {
    date -r "$1" +%s 2>/dev/null \
        || stat -c%Y "$1" 2>/dev/null \
        || python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$1" 2>/dev/null \
        || echo 0
}

PROJECT_HASH=$(_hash_dir "${CLAUDE_PROJECT_DIR}")
MARKER="/tmp/.claude-mem-nudge-${PROJECT_HASH}"

if [ -f "$MARKER" ]; then
    LAST=$(_file_mtime "$MARKER")
    AGE=$(( $(date +%s) - ${LAST:-0} ))
    if [ "$AGE" -lt 86400 ]; then
        exit 0
    fi
fi

touch "$MARKER"

SETUP_CMD="${HOME}/.claude/memory/.source-dir"
if [ -f "$SETUP_CMD" ]; then
    SETUP_CMD="$(cat "$SETUP_CMD")/setup-project.sh"
else
    # Fallback: use known install location
    if [ -f "${HOME}/.claude/memory/setup-project.sh" ]; then
        SETUP_CMD="${HOME}/.claude/memory/setup-project.sh"
    else
        SETUP_CMD="setup-project.sh"
    fi
fi

echo "This project does not have Claude memory set up."
echo "To enable persistent knowledge graph memory, run:"
echo "  '${SETUP_CMD}' '${CLAUDE_PROJECT_DIR}'"
echo ""
echo "This adds a .memory/ directory (gitignored) with a CLI bridge"
echo "for keyword search across conversations."
exit 0

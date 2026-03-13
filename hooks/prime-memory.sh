#!/bin/bash
# SessionStart hook: run maintenance + inject memory graph summary.
# Global — works for any project with .memory/ dir.

[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0

MEMORY_DIR="${CLAUDE_PROJECT_DIR}/.memory"
MEMORY_FILE="${MEMORY_DIR}/graph.jsonl"

# Skip if project has no memory setup
if [ ! -d "${MEMORY_DIR}" ]; then
    exit 0
fi

# Run maintenance (throttled internally to 1x/day)
if [ -f "${HOME}/.claude/memory/maintenance.py" ]; then
    ERR_LOG="${MEMORY_DIR}/maintenance.err"
    # Rotate error log if >100KB
    ERR_SIZE=0
    if [ -f "$ERR_LOG" ]; then
        ERR_SIZE=$(wc -c < "$ERR_LOG" 2>/dev/null | tr -d ' ') || ERR_SIZE=0
    fi
    if [ "${ERR_SIZE:-0}" -gt 100000 ] 2>/dev/null; then
        mv -f "$ERR_LOG" "${ERR_LOG}.old" 2>/dev/null || true
    fi
    python3 "${HOME}/.claude/memory/maintenance.py" \
        "${CLAUDE_PROJECT_DIR}" 2>>"$ERR_LOG" || true
fi

# Skip summary if graph is empty/missing
if [ ! -f "${MEMORY_FILE}" ] || [ ! -s "${MEMORY_FILE}" ]; then
    echo "Memory graph is empty. Use MCP memory tools (create_entities, create_relations) to build knowledge."
    exit 0
fi

# Use cached summary if graph hasn't changed since last run
SUMMARY_CACHE="${MEMORY_DIR}/.graph-summary.txt"
GRAPH_MTIME=0
CACHE_MTIME=0

# Get graph mtime portably
if [ -f "${MEMORY_FILE}" ]; then
    GRAPH_MTIME=$(date -r "${MEMORY_FILE}" +%s 2>/dev/null \
        || stat -c%Y "${MEMORY_FILE}" 2>/dev/null \
        || echo 0)
fi
if [ -f "${SUMMARY_CACHE}" ]; then
    CACHE_MTIME=$(date -r "${SUMMARY_CACHE}" +%s 2>/dev/null \
        || stat -c%Y "${SUMMARY_CACHE}" 2>/dev/null \
        || echo 0)
fi

if [ -f "${SUMMARY_CACHE}" ] && [ "${CACHE_MTIME}" -ge "${GRAPH_MTIME}" ] 2>/dev/null; then
    cat "${SUMMARY_CACHE}"
else
    # Single-pass: count entities/relations and list top 30 entities
    SUMMARY=$(python3 - "$MEMORY_FILE" << 'PYEOF'
import sys, json

graph_path = sys.argv[1]
ec = rc = 0
top30 = []

try:
    with open(graph_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                t = obj.get('type', '')
                if t == 'entity':
                    ec += 1
                    if len(top30) < 30:
                        name = obj.get('name', '?')
                        etype = obj.get('entityType', '?')
                        obs = obj.get('observations', [])
                        top30.append(
                            f'  {name} ({etype}) -- {len(obs)} obs'
                        )
                elif t == 'relation':
                    rc += 1
            except Exception:
                pass
except OSError:
    pass

print(f'=== Memory Graph: {ec} entities, {rc} relations ===')
for line in top30:
    print(line)
if ec > 30:
    print(f'  ... and {ec - 30} more')
PYEOF
    )

    echo "$SUMMARY"
    # Cache for future sessions
    echo "$SUMMARY" > "${SUMMARY_CACHE}" 2>/dev/null || true
fi

if [ -f "${MEMORY_DIR}/tfidf_index.json" ]; then
    echo ""
    echo "Semantic search available: use semantic_search_memory tool."
fi

echo ""
echo "Search: semantic_search_memory | Traverse: traverse_relations | Write: create_entities"
exit 0

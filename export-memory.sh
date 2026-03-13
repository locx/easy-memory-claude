#!/bin/bash
# Export a project's memory graph to a portable JSON bundle.
# Usage: export-memory.sh [project_dir] [output_file]
#
# Creates a self-contained JSON file with graph data + metadata.
# Transfer via git, cloud storage, or sneakernet.
# #5: Passes paths via sys.argv — no shell interpolation into Python.
set -euo pipefail

PROJECT_DIR="${1:-$(pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd)" || {
    echo "ERROR: Directory not found: ${1:-.}"
    exit 1
}
MEMORY_DIR="${PROJECT_DIR}/.memory"
GRAPH="${MEMORY_DIR}/graph.jsonl"

if [ ! -f "$GRAPH" ]; then
    echo "ERROR: No graph found at ${GRAPH}"
    exit 1
fi

# Default output: project-name_memory_YYYY-MM-DD.json
PROJECT_NAME=$(basename "$PROJECT_DIR")
DATE=$(date +%Y-%m-%d)
OUTPUT="${2:-${PROJECT_NAME}_memory_${DATE}.json}"

python3 - "$GRAPH" "$PROJECT_NAME" "$OUTPUT" << 'PYEOF'
import json, os, sys, time

graph_path = sys.argv[1]
project_name = sys.argv[2]
output_path = sys.argv[3]

entries = []
entity_count = 0
relation_count = 0
with open(graph_path, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    entries.append(obj)
                    t = obj.get('type')
                    if t == 'entity':
                        entity_count += 1
                    elif t == 'relation':
                        relation_count += 1
            except json.JSONDecodeError:
                continue

bundle = {
    'format': 'easy-memory-claude-export',
    'version': 1,
    'exported': time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime()
    ),
    'project': project_name,
    'stats': {
        'entities': entity_count,
        'relations': relation_count,
        'total_entries': len(entries),
    },
    'entries': entries,
}

# Atomic write
tmp = output_path + '.tmp'
try:
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(bundle, f, indent=2)
        f.write('\n')
    os.replace(tmp, output_path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise

print(f'Exported {entity_count} entities, '
      f'{relation_count} relations')
print(f'Output: {output_path}')
PYEOF

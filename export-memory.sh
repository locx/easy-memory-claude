#!/bin/bash
# Export a project's memory graph to a portable JSON bundle.
# Usage: export-memory.sh [project_dir] [output_file]
#
# Creates a self-contained JSON file with graph data + metadata.
# Transfer via git, cloud storage, or sneakernet.
# Passes paths via sys.argv — no shell interpolation into Python.
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

# Size guard — reject graphs larger than 50MB
GRAPH_SIZE=$(wc -c < "$GRAPH" | tr -d ' ')
if [ "$GRAPH_SIZE" -gt 52428800 ]; then
    echo "ERROR: Graph too large (${GRAPH_SIZE} bytes, max 50MB)"
    echo "Run maintenance to prune first: python3 ~/.claude/memory/maintenance.py ${PROJECT_DIR}"
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

# Stream entries to output — avoid loading all into list.
# Two-pass: count first, then stream to output JSON.
entity_count = 0
relation_count = 0
total_count = 0
with open(graph_path, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    total_count += 1
                    t = obj.get('type')
                    if t == 'entity':
                        entity_count += 1
                    elif t == 'relation':
                        relation_count += 1
            except json.JSONDecodeError:
                continue

# Stream write: header, then entries one by one, then close
sep = (',', ':')
tmp = output_path + '.tmp'
try:
    with open(tmp, 'w', encoding='utf-8') as out:
        # Write header as streaming fields (no slice hack)
        out.write('{\n')
        out.write('  "format": "easy-memory-claude-export",\n')
        out.write('  "version": 1,\n')
        out.write('  "exported": ')
        json.dump(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), out)
        out.write(',\n')
        out.write('  "project": ')
        json.dump(project_name, out)
        out.write(',\n')
        out.write('  "stats": ')
        json.dump({
            'entities': entity_count,
            'relations': relation_count,
            'total_entries': total_count,
        }, out)
        out.write(',\n')
        out.write('  "entries": [\n')

        # Stream entries from graph
        first = True
        with open(graph_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        continue
                except json.JSONDecodeError:
                    continue
                if not first:
                    out.write(',\n')
                out.write('    ')
                json.dump(obj, out, separators=sep)
                first = False

        out.write('\n  ]\n}\n')
        out.flush()
        os.fsync(out.fileno())
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

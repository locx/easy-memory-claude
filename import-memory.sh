#!/bin/bash
# Import a memory export bundle into a project's graph.
# Usage: import-memory.sh <bundle_file> [project_dir]
#
# Merges entities from the bundle, deduplicating by name+type.
# Creates a backup before merging.
# #5: Passes paths via sys.argv — no shell interpolation into Python.
# #30: Validates imported entity structure.
set -euo pipefail

BUNDLE="${1:?Usage: import-memory.sh <bundle_file> [project_dir]}"
PROJECT_DIR="${2:-$(pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd)" || {
    echo "ERROR: Directory not found: ${2:-.}"
    exit 1
}
MEMORY_DIR="${PROJECT_DIR}/.memory"
GRAPH="${MEMORY_DIR}/graph.jsonl"

if [ ! -f "$BUNDLE" ]; then
    echo "ERROR: Bundle not found: ${BUNDLE}"
    exit 1
fi

if [ ! -d "$MEMORY_DIR" ]; then
    echo "ERROR: Project not initialized. Run setup-project.sh first."
    exit 1
fi

# P3-4: Reject bundles larger than 50MB
BUNDLE_SIZE=$(wc -c < "$BUNDLE" | tr -d ' ')
if [ "$BUNDLE_SIZE" -gt 52428800 ]; then
    echo "ERROR: Bundle too large (${BUNDLE_SIZE} bytes, max 50MB)"
    exit 1
fi

# Backup existing graph
if [ -f "$GRAPH" ]; then
    cp "$GRAPH" "${GRAPH}.pre-import.bak"
    echo "Backed up existing graph to ${GRAPH}.pre-import.bak"
fi

python3 - "$BUNDLE" "$GRAPH" << 'PYEOF'
import json, os, sys, time

bundle_path = sys.argv[1]
graph_path = sys.argv[2]

# Load bundle
with open(bundle_path, encoding='utf-8') as f:
    bundle = json.load(f)

fmt = bundle.get('format', '')
if fmt not in ('easy-memory-claude-export', 'mem-handler-export'):
    print('ERROR: Not a valid easy-memory-claude export bundle')
    sys.exit(1)

import_entries = bundle.get('entries', [])
if not import_entries:
    print('Bundle is empty, nothing to import.')
    sys.exit(0)


def _validate_entity(entry):
    """#30: Validate entity has required fields + correct types."""
    name = entry.get('name')
    if not name or not isinstance(name, str):
        return False
    if not isinstance(entry.get('entityType', ''), str):
        return False
    obs = entry.get('observations', [])
    if not isinstance(obs, list):
        return False
    return True


def _validate_relation(entry):
    """#30: Validate relation has required fields."""
    fr = entry.get('from')
    to = entry.get('to')
    if not fr or not isinstance(fr, str):
        return False
    if not to or not isinstance(to, str):
        return False
    if not isinstance(entry.get('relationType', ''), str):
        return False
    return True


# Load existing graph
existing = []
existing_entities = {}  # (name, type) -> index
existing_rels = set()   # (from, to, relationType)

if os.path.exists(graph_path):
    with open(graph_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        existing.append(obj)
                        if obj.get('type') == 'entity':
                            key = (obj.get('name', ''),
                                   obj.get('entityType', ''))
                            existing_entities[key] = (
                                len(existing) - 1
                            )
                        elif obj.get('type') == 'relation':
                            rk = (obj.get('from', ''),
                                  obj.get('to', ''),
                                  obj.get('relationType', ''))
                            existing_rels.add(rk)
                except json.JSONDecodeError:
                    continue

added_e = 0
merged_e = 0
added_r = 0
skipped_r = 0
skipped_invalid = 0

for entry in import_entries:
    if not isinstance(entry, dict):
        skipped_invalid += 1
        continue
    t = entry.get('type')

    if t == 'entity':
        # #30: validate structure
        if not _validate_entity(entry):
            skipped_invalid += 1
            continue
        key = (entry.get('name', ''),
               entry.get('entityType', ''))
        if key in existing_entities:
            # Merge observations (deduplicate)
            idx = existing_entities[key]
            old_obs_raw = existing[idx].get(
                'observations', []
            )
            # P1-6: handle unhashable obs (dicts/lists)
            old_obs_str = set()
            for o in old_obs_raw:
                if isinstance(o, str):
                    old_obs_str.add(o)
                else:
                    old_obs_str.add(json.dumps(
                        o, sort_keys=True
                    ))
            new_obs = entry.get('observations', [])
            for obs in new_obs:
                k = obs if isinstance(obs, str) \
                    else json.dumps(obs, sort_keys=True)
                if k not in old_obs_str:
                    existing[idx].setdefault(
                        'observations', []
                    ).append(obs)
                    old_obs_str.add(k)
            merged_e += 1
        else:
            existing_entities[key] = len(existing)
            existing.append(entry)
            added_e += 1

    elif t == 'relation':
        # #30: validate structure
        if not _validate_relation(entry):
            skipped_invalid += 1
            continue
        rk = (entry.get('from', ''),
              entry.get('to', ''),
              entry.get('relationType', ''))
        if rk not in existing_rels:
            existing_rels.add(rk)
            existing.append(entry)
            added_r += 1
        else:
            skipped_r += 1
    else:
        existing.append(entry)

# Write merged graph atomically
tmp = graph_path + '.tmp.import'
try:
    with open(tmp, 'w', encoding='utf-8') as f:
        f.writelines(
            json.dumps(e, separators=(',', ':'))
            + '\n' for e in existing
        )
    os.replace(tmp, graph_path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise

print(f'Import complete from: {bundle_path}')
print(f'  Entities: {added_e} added, {merged_e} merged')
print(f'  Relations: {added_r} added, '
      f'{skipped_r} duplicates skipped')
if skipped_invalid:
    print(f'  Skipped {skipped_invalid} invalid entries')
print(f'  Total entries now: {len(existing)}')
PYEOF

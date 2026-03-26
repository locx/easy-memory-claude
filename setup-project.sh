#!/bin/bash
# Initialize a project for Claude memory infrastructure.
# Usage: setup-project.sh [project_dir]
#
# Creates .memory/, removes legacy MCP configs, bootstraps graph.
# Safe to re-run — skips existing files, merges configs.
set -euo pipefail

PROJECT_DIR="${1:-$(pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd)" || {
    echo "ERROR: Directory not found: ${1:-.}"
    exit 1
}
MEMORY_DIR="${PROJECT_DIR}/.memory"
VSCODE_DIR="${PROJECT_DIR}/.vscode"
GITIGNORE="${PROJECT_DIR}/.gitignore"
CLAUDE_HOME="${HOME}/.claude"
PROJECT_NAME="$(basename "$PROJECT_DIR")"

echo "=== Memory Setup: ${PROJECT_DIR} ==="

# Verify global infra exists
if [ ! -f "${CLAUDE_HOME}/memory/maintenance.py" ]; then
    echo "ERROR: Global memory tools not found at ${CLAUDE_HOME}/memory/"
    SOURCE_DIR_FILE="${CLAUDE_HOME}/memory/.source-dir"
    if [ -f "$SOURCE_DIR_FILE" ]; then
        echo "Run: $(cat "$SOURCE_DIR_FILE")/install.sh first"
    else
        echo "Run install.sh from the easy-memory-claude project first"
    fi
    exit 1
fi

# ---- 1. Create .memory directory ----
mkdir -p "${MEMORY_DIR}"
echo "  [ok] ${MEMORY_DIR}/"

# ---- 2. Create config.json template ----
CONFIG_FILE="${MEMORY_DIR}/config.json"
if [ ! -f "${CONFIG_FILE}" ]; then
    cat > "${CONFIG_FILE}" << 'CFGEOF'
{
  "_comment": "Optional overrides — delete keys to use defaults",
  "decay_threshold": 0.1,
  "max_age_days": 90,
  "throttle_hours": 24,
  "min_merge_name_len": 4
}
CFGEOF
    echo "  [ok] ${CONFIG_FILE} (template)"
else
    echo "  [skip] ${CONFIG_FILE} — already exists"
fi

# ---- 3. Bootstrap empty graph.jsonl ----
GRAPH_FILE="${MEMORY_DIR}/graph.jsonl"
if [ ! -f "${GRAPH_FILE}" ]; then
    touch "${GRAPH_FILE}"
    echo "  [ok] ${GRAPH_FILE} (empty)"
else
    echo "  [skip] ${GRAPH_FILE} — already exists"
fi

# ---- 4 & 5. Remove memory MCP servers from .mcp.json and .vscode/mcp.json ----
# VSCode extension spawns MCP servers on session start, causing
# multi-second delay with zero benefit (tools don't work).
# CLI bridge in CLAUDE.md covers both CLI and VSCode.
_remove_memory_servers() {
    local MCP_FILE="$1" SERVER_KEY="$2" LABEL="$3"
    if [ ! -f "${MCP_FILE}" ]; then
        echo "  [skip] ${LABEL} — not present"
        return
    fi
    python3 - "${MCP_FILE}" "${SERVER_KEY}" "${LABEL}" << 'PYEOF'
import json, sys, os

mcp_path, server_key, label = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(mcp_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError, OSError):
    sys.exit(0)

servers = cfg.get(server_key, {})
removed = [k for k, s in servers.items() if isinstance(s, dict) and (
    'MEMORY_DIR' in s.get('env', {})
    or 'semantic_server' in str(s.get('args', []))
    or (s.get('command') == 'npx' and 'server-memory' in str(s.get('args', [])))
    or k in ('memory', 'memory-search', 'memory-semantic-search'))]
for k in removed:
    del servers[k]
if not removed:
    print(f'  [skip] {label} — no memory servers to remove')
    sys.exit(0)
if servers:
    tmp = mcp_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, mcp_path)
else:
    os.unlink(mcp_path)
for k in removed:
    print(f'  [removed] {label} — "{k}" (causes VSCode startup delay)')
PYEOF
}

_remove_memory_servers "${PROJECT_DIR}/.mcp.json" "mcpServers" ".mcp.json"
_remove_memory_servers "${VSCODE_DIR}/mcp.json" "servers" ".vscode/mcp.json"

# ---- 6. Add .memory/ to .gitignore ----
if [ -f "${GITIGNORE}" ]; then
    if grep -q '\.memory/' "${GITIGNORE}" 2>/dev/null; then
        echo "  [skip] .memory/ already in .gitignore"
    else
        printf '\n# Memory\n.memory/\n' >> "${GITIGNORE}"
        echo "  [ok] Added .memory/ to .gitignore"
    fi
else
    printf '# Memory\n.memory/\n' > "${GITIGNORE}"
    echo "  [ok] Created .gitignore with .memory/"
fi

# ---- 7. Bootstrap: scan project and seed graph ----
if [ ! -s "${GRAPH_FILE}" ]; then
    echo ""
    echo "[bootstrap] Scanning project structure..."
    python3 - "${PROJECT_DIR}" "${GRAPH_FILE}" "${PROJECT_NAME}" << 'PYEOF'
import json, os, sys, time

project_dir = sys.argv[1]
graph_path = sys.argv[2]
project_name = sys.argv[3]

now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

# Collect project files by extension
ext_map = {
    '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
    '.tsx': 'TypeScript', '.jsx': 'JavaScript',
    '.rs': 'Rust', '.go': 'Go', '.java': 'Java',
    '.swift': 'Swift', '.sh': 'Shell', '.sql': 'SQL',
    '.md': 'Documentation', '.json': 'Config',
    '.yaml': 'Config', '.yml': 'Config', '.toml': 'Config',
}

skip_dirs = {
    '.git', '.memory', 'node_modules', '__pycache__',
    '.venv', 'venv', '.tox', 'dist', 'build', '.eggs',
    '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'target', '.next', '.nuxt',
}

entities = []
relations = []
dir_modules = {}  # track directories as modules
file_count = 0
tech_seen = set()

for root, dirs, files in os.walk(project_dir):
    # Prune skip dirs
    dirs[:] = [
        d for d in dirs
        if d not in skip_dirs and not d.startswith('.')
    ]
    rel_root = os.path.relpath(root, project_dir)
    if rel_root == '.':
        rel_root = ''

    for fname in sorted(files):
        if fname.startswith('.'):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ext_map:
            continue

        file_count += 1
        if file_count > 200:
            break

        rel_path = os.path.join(rel_root, fname) \
            if rel_root else fname
        tech = ext_map[ext]
        tech_seen.add(tech)

        # Read first 5 lines for observation
        obs = [f'{tech} file']
        fpath = os.path.join(root, fname)
        try:
            with open(fpath, encoding='utf-8',
                      errors='ignore') as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= 5:
                        break
                    lines.append(line.rstrip())
                # Extract docstring or first comment
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith(('#', '//', '/*',
                                           '"""', "'''")):
                        clean = stripped.lstrip(
                            '#/ *"\''
                        ).rstrip('*/"\' ')
                        if len(clean) > 5:
                            obs.append(clean[:120])
                            break
        except OSError:
            pass

        entities.append({
            'type': 'entity',
            'name': rel_path,
            'entityType': 'Module',
            'observations': obs,
            '_created': now,
            '_updated': now,
        })

        # Relate file to its directory
        if rel_root:
            dir_name = rel_root + '/'
            if dir_name not in dir_modules:
                dir_modules[dir_name] = True
                entities.append({
                    'type': 'entity',
                    'name': dir_name,
                    'entityType': 'Component',
                    'observations': [f'Directory in {project_name}'],
                    '_created': now,
                    '_updated': now,
                })
            relations.append({
                'type': 'relation',
                'from': dir_name,
                'to': rel_path,
                'relationType': 'contains',
            })

    if file_count > 200:
        break

# Add project entity
entities.insert(0, {
    'type': 'entity',
    'name': project_name,
    'entityType': 'Project',
    'observations': [
        f'{file_count} source files scanned',
        f'Technologies: {", ".join(sorted(tech_seen))}',
    ],
    '_created': now,
    '_updated': now,
})

# Relate top-level dirs to project
for d in dir_modules:
    if '/' in d.rstrip('/'):
        continue  # only top-level
    relations.append({
        'type': 'relation',
        'from': project_name,
        'to': d,
        'relationType': 'contains',
    })

# Write graph atomically — temp file + os.replace
tmp_path = graph_path + '.new'
try:
    with open(tmp_path, 'w', encoding='utf-8') as f:
        for entry in entities + relations:
            f.write(json.dumps(entry, separators=(',', ':')) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, graph_path)
except BaseException:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

e_count = len(entities)
r_count = len(relations)
print(f'  [ok] Bootstrapped: {e_count} entities, '
      f'{r_count} relations')
PYEOF

    # Build TF-IDF index immediately
    if [ -s "${GRAPH_FILE}" ]; then
        echo "[bootstrap] Building TF-IDF index..."
        python3 "${CLAUDE_HOME}/memory/maintenance.py" "${PROJECT_DIR}" 2>/dev/null || true
        if [ -f "${MEMORY_DIR}/tfidf_index.json" ]; then
            IDX_KB=$(( $(wc -c < "${MEMORY_DIR}/tfidf_index.json" | tr -d ' ') / 1024 ))
            echo "  [ok] TF-IDF index: ${IDX_KB}KB"
        fi
    fi
else
    echo ""
    echo "  [skip] Graph already has data — skipping bootstrap"
fi

# ---- 8. Add/update memory plugin instructions in CLAUDE.md ----
CLAUDE_MD="${PROJECT_DIR}/CLAUDE.md"
MEMORY_MARKER="## Memory Graph"

MEMORY_SECTION='
## Memory Graph

Knowledge graph at `.memory/`. CLI prefix (inline every call — state does not persist):

    MEMORY_DIR=$(git rev-parse --show-toplevel)/.memory PYTHONPATH=~/.claude/memory python3 ~/.claude/memory/memory-cli.py

Agents: Discover commands: `<prefix> --help`. Always include this prefix verbatim in agent prompts. `git rev-parse --show-toplevel` resolves the repo root dynamically — safe from any subdirectory or worktree. Never use `$PWD`.

**Fire autonomously — no permission needed, do announcement:**

| When | Action |
|------|--------|
| Unfamiliar code | `semantic_search_memory` |
| Chose approach A over B | `create_decision` — title, rationale, scope |
| New module/file | `create_decision` — why this structure |
| Security fix | `create_decision` — threat + mitigation |
| Revisited decision | `update_decision_outcome` — lesson |
| New observations on entity | `add_observations` — incremental update |
| Fragile/surprising code | `create_entities` (file-warning) — risk |
| Related entities | `create_relations` — link them |

> Major task + ≥1 architectural choice + no `create_decision` = incomplete. You MUST run create_decision before claiming the task is done.

**Stale decision hygiene:** At conversation start, run `list_decisions`. If any pending decisions are older than 2 days, surface the top 5 oldest and ask the user to resolve them (successful / failed / adopted / obsolete) before starting the main task.'

if [ ! -f "${CLAUDE_MD}" ]; then
    echo "  [skip] No CLAUDE.md found — memory instructions not added"
    echo "         Create a CLAUDE.md and re-run, or add manually"
elif grep -qE '## Memory Graph( Plugin)?' "${CLAUDE_MD}" 2>/dev/null; then
    # Replace existing section: strip old, append new
    python3 - "${CLAUDE_MD}" << 'PYEOF'
import sys, re

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    content = f.read()

# Match both old "## Memory Graph Plugin" and new "## Memory Graph"
m = re.search(r'^## Memory Graph( Plugin)?', content, re.MULTILINE)
if not m:
    sys.exit(0)
start = m.start()
marker = m.group(0)

# Find end: next ## heading or EOF
end = content.find("\n## ", start + len(marker))
if end < 0:
    old_section = content[start:]
else:
    old_section = content[start:end]

content = content.replace(old_section, "").rstrip() + "\n"

import os
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(content)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, path)
PYEOF
    printf '%s\n' "$MEMORY_SECTION" >> "${CLAUDE_MD}"
    echo "  [ok] Upgraded memory plugin section in CLAUDE.md"
else
    printf '%s\n' "$MEMORY_SECTION" >> "${CLAUDE_MD}"
    echo "  [ok] Added memory plugin section to CLAUDE.md"
fi

echo ""
echo "============================================================"
echo "  Setup complete: ${PROJECT_NAME}"
echo ""
echo "  Graph:    ${GRAPH_FILE}"
echo "  Access:   CLI bridge (Bash) — works in both CLI and VSCode"
echo ""
echo "  13 tools available:"
echo "    semantic_search_memory, traverse_relations,"
echo "    search_memory_by_time, create_entities,"
echo "    create_relations, add_observations,"
echo "    remove_observations, delete_entities,"
echo "    rename_entity, create_decision,"
echo "    update_decision_outcome, list_decisions,"
echo "    graph_stats"
echo "============================================================"

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

# ---- 4. Remove memory MCP servers from .mcp.json ----
# VSCode extension spawns MCP servers on session start, causing
# multi-second delay with zero benefit (tools don't work).
# CLI bridge in CLAUDE.md covers both CLI and VSCode.
MCP_ROOT="${PROJECT_DIR}/.mcp.json"
if [ -f "${MCP_ROOT}" ]; then
    python3 - "${MCP_ROOT}" << 'PYEOF'
import json, sys, os

mcp_path = sys.argv[1]
try:
    with open(mcp_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError, OSError):
    sys.exit(0)

servers = cfg.get('mcpServers', {})
removed = []
for key in list(servers.keys()):
    s = servers[key]
    if not isinstance(s, dict):
        continue
    env = s.get('env', {})
    args = s.get('args', [])
    cmd = s.get('command', '')
    if ('MEMORY_DIR' in env or 'semantic_server' in str(args)
            or (cmd == 'npx' and 'server-memory' in str(args))
            or key in ('memory', 'memory-search',
                       'memory-semantic-search')):
        removed.append(key)
        del servers[key]

if not removed:
    print('  [skip] .mcp.json — no memory servers to remove')
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
    print(f'  [removed] .mcp.json — "{k}" (causes VSCode startup delay)')
PYEOF
else
    echo "  [skip] .mcp.json — not present"
fi

# ---- 5. Remove .vscode/mcp.json memory server if present ----
# VSCode extension spawns MCP servers on session start even though
# it can't use the tools (known bug). This adds seconds of delay.
# CLAUDE.md CLI bridge covers both CLI and VSCode.
MCP_VSCODE="${VSCODE_DIR}/mcp.json"
if [ -f "${MCP_VSCODE}" ]; then
    python3 - "${MCP_VSCODE}" << 'PYEOF'
import json, sys, os

mcp_path = sys.argv[1]
try:
    with open(mcp_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError, OSError):
    sys.exit(0)

servers = cfg.get('servers', {})
removed = []
for key in list(servers.keys()):
    s = servers[key]
    if not isinstance(s, dict):
        continue
    env = s.get('env', {})
    args = s.get('args', [])
    if ('MEMORY_DIR' in env or 'semantic_server' in str(args)
            or key in ('memory', 'memory-search',
                       'memory-semantic-search')):
        removed.append(key)
        del servers[key]

if not removed:
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
    print(f'  [removed] .vscode/mcp.json — "{k}" (causes startup delay)')
PYEOF
else
    echo "  [skip] .vscode/mcp.json — not present"
fi

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
MEMORY_MARKER="## Memory Graph Plugin"

MEMORY_SECTION='
## Memory Graph Plugin

Knowledge graph at `.memory/`. All tools via Bash CLI bridge. Inline this full prefix in every Bash call (shell state does not persist between calls):

    MEMORY_DIR=$PWD/.memory PYTHONPATH=~/.claude/memory python3 ~/.claude/memory/memory-cli.py

Tools: graph_stats, semantic_search_memory, create_decision, update_decision_outcome, create_entities, create_relations, add_observations, delete_entities, traverse_relations, search_memory_by_time

**Mandatory behavior — do silently, never ask permission:**
- Before editing unfamiliar code → run `semantic_search_memory` for prior context
- After choosing between approaches → run `create_decision` with title, rationale, scope
- When finding fragile/surprising code → run `create_entities` with entityType `file-warning`
- After revisiting a prior decision → run `update_decision_outcome` with lesson learned
- After creating related entities → run `create_relations` to link them

These are autonomous actions like reading a file before editing it. Never ask "want me to record this?" — just do it.'

if [ ! -f "${CLAUDE_MD}" ]; then
    echo "  [skip] No CLAUDE.md found — memory instructions not added"
    echo "         Create a CLAUDE.md and re-run, or add manually"
elif grep -q "${MEMORY_MARKER}" "${CLAUDE_MD}" 2>/dev/null; then
    # Replace existing section: strip old, append new
    python3 - "${CLAUDE_MD}" << 'PYEOF'
import sys

path = sys.argv[1]
marker = "## Memory Graph Plugin"
with open(path, encoding="utf-8") as f:
    content = f.read()

start = content.find(marker)
if start < 0:
    sys.exit(0)

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
echo "  10 tools available:"
echo "    semantic_search_memory, traverse_relations,"
echo "    search_memory_by_time, create_entities,"
echo "    create_relations, add_observations, delete_entities,"
echo "    create_decision, update_decision_outcome, graph_stats"
echo "============================================================"

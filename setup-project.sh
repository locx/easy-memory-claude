#!/bin/bash
# Initialize a project for Claude memory infrastructure.
# Usage: setup-project.sh [project_dir]
#
# Creates .memory/, registers MCP server, bootstraps graph.
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

# ---- 4. Register MCP server (.mcp.json — Claude Code CLI) ----
MCP_ROOT="${PROJECT_DIR}/.mcp.json"
SEMANTIC_PKG="${CLAUDE_HOME}/memory"

python3 - "${MCP_ROOT}" "${SEMANTIC_PKG}" "${MEMORY_DIR}" << 'PYEOF'
import json, sys, os

mcp_path = sys.argv[1]
pkg_dir = sys.argv[2]
memory_dir = sys.argv[3]

new_server = {
    "command": "python3",
    "args": ["-m", "semantic_server"],
    "env": {"MEMORY_DIR": memory_dir, "PYTHONPATH": pkg_dir}
}

cfg = {}
if os.path.exists(mcp_path):
    try:
        with open(mcp_path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, ValueError):
        cfg = {}

servers = cfg.setdefault('mcpServers', {})
if 'memory' in servers:
    print('  [skip] .mcp.json — memory server already present')
    sys.exit(0)

# Remove old npx-based memory server if present
for key in list(servers.keys()):
    s = servers[key]
    if isinstance(s, dict):
        cmd = s.get('command', '')
        args = s.get('args', [])
        if cmd == 'npx' and any(
            'server-memory' in str(a) for a in args
        ):
            del servers[key]
            print(f'  [removed] .mcp.json — old npx server "{key}"')

servers['memory'] = new_server

tmp = mcp_path + '.tmp'
try:
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, mcp_path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print('  [ok] .mcp.json — memory server registered')
PYEOF

# ---- 5. Register MCP server (.vscode/mcp.json — VS Code) ----
mkdir -p "${VSCODE_DIR}"
MCP_VSCODE="${VSCODE_DIR}/mcp.json"

python3 - "${MCP_VSCODE}" << 'PYEOF'
import json, sys, os

mcp_path = sys.argv[1]

new_server = {
    "command": "python3",
    "args": ["-m", "semantic_server"],
    "env": {
        "MEMORY_DIR": "${workspaceFolder}/.memory",
        "PYTHONPATH": "${userHome}/.claude/memory"
    }
}

cfg = {}
if os.path.exists(mcp_path):
    try:
        with open(mcp_path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, ValueError):
        cfg = {}

servers = cfg.setdefault('servers', {})
if 'memory' in servers:
    print('  [skip] .vscode/mcp.json — memory server already present')
    sys.exit(0)

# Remove old npx-based servers
for key in list(servers.keys()):
    s = servers[key]
    if isinstance(s, dict):
        cmd = s.get('command', '')
        args = s.get('args', [])
        if cmd == 'npx' and any(
            'server-memory' in str(a) for a in args
        ):
            del servers[key]
            print(f'  [removed] .vscode/mcp.json — old npx server "{key}"')

# Remove old separate semantic-search server (now unified)
for key in ('memory-search', 'memory-semantic-search'):
    if key in servers:
        del servers[key]
        print(f'  [merged] .vscode/mcp.json — removed separate "{key}" (now unified in "memory")')

servers['memory'] = new_server

tmp = mcp_path + '.tmp'
try:
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, mcp_path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print('  [ok] .vscode/mcp.json — memory server registered')
PYEOF

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

echo ""
echo "============================================================"
echo "  Setup complete: ${PROJECT_NAME}"
echo ""
echo "  Graph:    ${GRAPH_FILE}"
echo "  MCP CLI:  ${MCP_ROOT}"
echo "  MCP VSC:  ${MCP_VSCODE}"
echo ""
echo "  7 MCP tools available (read + write):"
echo "    semantic_search_memory, traverse_relations,"
echo "    search_memory_by_time, create_entities,"
echo "    create_relations, add_observations, delete_entities"
echo ""
echo "  Restart Claude Code to activate the MCP server."
echo "============================================================"

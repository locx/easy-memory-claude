#!/bin/bash
# Initialize a project for Claude memory infrastructure.
# Usage: setup-project.sh [project_dir]
#
# Creates .memory/, removes legacy MCP configs, migrates auto-memory.
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

# ---- 7. Migrate built-in auto-memory into graph ----
# Claude Code's built-in auto-memory writes .md files to
# ~/.claude/projects/-<path-with-slashes-as-dashes>/memory/
# Parse those files and import as graph entities so nothing is lost.
AUTO_MEM_KEY=$(echo "${PROJECT_DIR}" | sed 's|^/||; s|/|-|g')
AUTO_MEM_DIR="${CLAUDE_HOME}/projects/-${AUTO_MEM_KEY}/memory"

if [ -d "${AUTO_MEM_DIR}" ]; then
    python3 - "${AUTO_MEM_DIR}" "${GRAPH_FILE}" << 'PYEOF'
import json, os, sys, time, re

auto_mem_dir = sys.argv[1]
graph_path = sys.argv[2]
now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

# Load existing entity names to avoid duplicates
existing = set()
if os.path.isfile(graph_path):
    with open(graph_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get('type') == 'entity':
                    existing.add(rec['name'])
            except (json.JSONDecodeError, KeyError):
                pass

migrated = 0
for fname in sorted(os.listdir(auto_mem_dir)):
    if not fname.endswith('.md') or fname == 'MEMORY.md':
        continue
    fpath = os.path.join(auto_mem_dir, fname)
    try:
        with open(fpath, encoding='utf-8') as f:
            content = f.read()
    except OSError:
        continue

    # Parse YAML frontmatter
    fm = {}
    body = content
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if m:
        body = content[m.end():]
        for line in m.group(1).splitlines():
            k, _, v = line.partition(':')
            k, v = k.strip(), v.strip()
            if k and v:
                fm[k] = v

    name = fm.get('name', os.path.splitext(fname)[0])
    entity_type = fm.get('type', 'reference')
    desc = fm.get('description', '')

    # Build observations from body lines
    observations = []
    if desc:
        observations.append(desc)
    for line in body.strip().splitlines():
        line = line.strip()
        if line and len(line) > 3:
            observations.append(line[:200])

    if not observations:
        continue
    # Prefix name to avoid collision with code entities
    entity_name = f'auto-memory: {name}'
    if entity_name in existing:
        continue

    entity = {
        'type': 'entity',
        'name': entity_name,
        'entityType': entity_type,
        'observations': observations,
        '_created': now,
        '_updated': now,
        '_migrated_from': 'auto-memory',
    }
    with open(graph_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entity, separators=(',', ':')) + '\n')
        f.flush()
        os.fsync(f.fileno())
    existing.add(entity_name)
    migrated += 1

if migrated:
    print(f'  [ok] Migrated {migrated} auto-memory entries into graph')
else:
    print(f'  [skip] No new auto-memory entries to migrate')
PYEOF
else
    echo "  [skip] No built-in auto-memory found for this project"
fi

# ---- 8. Build TF-IDF index if graph has data ----
if [ -s "${GRAPH_FILE}" ]; then
    echo ""
    echo "[index] Building TF-IDF index..."
    python3 "${CLAUDE_HOME}/memory/maintenance.py" "${PROJECT_DIR}" 2>/dev/null || true
    if [ -f "${MEMORY_DIR}/tfidf_index.json" ]; then
        IDX_KB=$(( $(wc -c < "${MEMORY_DIR}/tfidf_index.json" | tr -d ' ') / 1024 ))
        echo "  [ok] TF-IDF index: ${IDX_KB}KB"
    fi
fi

# ---- 9. Add/update memory plugin instructions in CLAUDE.md ----
CLAUDE_MD="${PROJECT_DIR}/CLAUDE.md"
MEMORY_MARKER="## Memory Graph"

MEMORY_SECTION='
## Memory Graph

Unified memory gateway via `mem`. Two stores, one interface:

- **Knowledge graph** (`.memory/`): Entities, relations, decisions — searchable, scored, branch-aware
- **Native memory** (`~/.claude/projects/.../memory/`): User prefs, feedback, references — always loaded in context

All commands:

    mem <command> [args]

### Reading Memory

SessionStart automatically prints a compact status line. Use these commands **on demand** when you need details:

| Command | When to use | What it returns |
|---------|-------------|-----------------|
| `mem search <query>` | Need context on a topic | Results from both graph + native memory |
| `mem recall <query>` | Need context + relationships | Search + 1-hop graph neighbors |
| `mem status` | Check health of both systems | Graph stats + native counts + decision nudge |
| `mem doctor` | Diagnose issues | Health checks across both stores |
| `mem rebuild` | Force index refresh | Re-scans and updates TF-IDF index |

**search vs recall**: Use `search` for facts. Use `recall` to understand how things connect (e.g. "what depends on AuthService?").

### Writing Memory

Fire autonomously — no permission needed.

| What to store | Command | Example |
|---------------|---------|---------|
| User prefs, feedback, role | `mem remember` | `mem remember --type feedback "Don'\''t mock the DB"` |
| Project decisions, rationale | `mem decide` | `mem decide '\''{"title":"Postgres","chosen":"PG"}'\''` |
| Code entities, architecture | `mem write` | `mem write '\''{"entities":[{"name":"Svc","observations":["..."]}]}'\''` |
| Entity relationships | `mem write` | `mem write '\''{"relations":[{"from":"A","to":"B"}]}'\''` |
| Quick references | `mem remember` | `mem remember --type reference --name "api-docs" "See confluence/api"` |
| Remove native memory | `mem forget` | `mem forget "memory-name"` |
| Remove graph entities | `mem remove` | `mem remove '\''{"entity_names":["Old"]}'\''` |
| Sync both stores | `mem sync` | `mem sync` |

### Memory Routing

**Native memory** (via `mem remember`) loads automatically every session at zero token cost. Use for:
- Your role, expertise, preferences (`--type user`)
- Corrections and workflow feedback (`--type feedback`)
- External references, links (`--type reference`)

**Knowledge graph** (via `mem write`/`mem decide`) requires `mem search` to query but supports relations, scoring, and decay. Use for:
- Architectural decisions and their rationale
- Code component relationships
- File warnings and known issues
- Facts that benefit from graph traversal

> **Rule:** Major task + >=1 architectural choice + no `mem decide` = incomplete.

### How SessionStart Works

At session start a hook prints a compact status like:

    Memory: 42e 28r 3d 0w | Native: 3 (1 user, 1 feedback, 1 project) | branch:main
      Top: SyncManager(component) [1 conn], AuthService(service) [2 conn]

This is ~50 tokens. Call `mem search` or `mem recall` only when you need deeper context.

### Aliases

Project-specific synonyms in `.memory/aliases.json` improve search. Format:

```json
{"groups": [["cache", "memoize", "memoization"], ["api", "endpoint", "route"]]}
```

### Rules

- **Always search before writing:** Run `mem search` before creating entities to avoid duplicates.
- **Pending decisions:** If SessionStart shows pending decisions relevant to your task, resolve them with `mem decide` before starting new work.
- **Use the right store:** Native memory for personal context (always loaded). Graph for project knowledge (searchable, relational).
- **`mem` is the gateway:** All memory operations go through `mem` commands. Do not write to native memory files or graph.jsonl directly.'

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
echo "  Commands:"
echo "    search, recall, write, decide, remember,"
echo "    forget, sync, remove, status, doctor, rebuild"
echo "============================================================"

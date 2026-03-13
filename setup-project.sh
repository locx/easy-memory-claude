#!/bin/bash
# Initialize a project for Claude memory infrastructure.
# Usage: setup-project.sh [project_dir]
#
# Creates .memory/, .vscode/mcp.json, updates .gitignore.
# Safe to re-run — skips existing files.
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

# Create .memory directory
mkdir -p "${MEMORY_DIR}"
echo "  [ok] ${MEMORY_DIR}/"

# Create config.json template (if not present)
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

# Create .vscode/mcp.json
mkdir -p "${VSCODE_DIR}"
MCP_CONFIG="${VSCODE_DIR}/mcp.json"
if [ -f "${MCP_CONFIG}" ]; then
    # Check if memory servers already configured
    if python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        cfg = json.load(f)
    servers = cfg.get('servers', {})
    if 'memory' in servers or 'memory-search' in servers:
        sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" "${MCP_CONFIG}" 2>/dev/null; then
        echo "  [skip] ${MCP_CONFIG} — memory servers already present"
    else
        echo "  [warn] ${MCP_CONFIG} exists but lacks memory servers"
        echo "         Add manually (see ~/.claude/memory/install.sh)"
    fi
else
    cat > "${MCP_CONFIG}" << 'MCPEOF'
{
  "servers": {
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory@1"],
      "env": {
        "MEMORY_FILE_PATH": "${workspaceFolder}/.memory/graph.jsonl"
      }
    },
    "memory-search": {
      "command": "python3",
      "args": ["${userHome}/.claude/memory/semantic_server.py"],
      "env": {
        "MEMORY_DIR": "${workspaceFolder}/.memory"
      }
    }
  }
}
MCPEOF
    echo "  [ok] ${MCP_CONFIG}"
fi

# Add .memory/ to .gitignore
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

echo ""
echo "Done. Restart Claude Code to activate MCP servers."
echo "Graph: ${MEMORY_DIR}/graph.jsonl"
echo "Index: ${MEMORY_DIR}/tfidf_index.json"

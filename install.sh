#!/bin/bash
# ============================================================
# Claude Memory Infrastructure — Single-File Installer
# ============================================================
# Deploys runtime memory tools to ~/.claude/ for all projects.
# Run from the easy-memory-claude project directory.
#
# Layout after install:
#   ~/.claude/memory/              Runtime scripts only
#     maintenance.py               Decay/prune/consolidate/TF-IDF
#     semantic_server.py           MCP server for semantic search
#   ~/.claude/hooks/               Global lifecycle hooks
#     prime-memory.sh              SessionStart — maintenance + context
#     capture-decisions.sh         Stop — persist decision reminder
#   ~/.claude/settings.json        Updated with hook wiring
#
# Dev/install files stay in easy-memory-claude project:
#   ~/projects/easy-memory-claude/
#     install.sh                   This installer
#     hooks/                       Hook source files (copied on install)
#       prime-memory.sh            SessionStart — maintenance + context
#       capture-decisions.sh       Stop — persist decision reminder
#       nudge-setup.sh             SessionStart — setup nudge
#       capture-tool-context.sh    PostToolUse — observation capture
#     setup-project.sh             Initialize any project for memory
#     requirements.txt             Optional deps for neural upgrade
#     .venv/                       Dedicated Python venv
#
# Per-project setup (run after install):
#   ~/projects/easy-memory-claude/setup-project.sh /path/to/project
#
# Requirements: python3 3.10+, node 18+ (for npx), git
# Platform: macOS / Linux
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_HOME="${HOME}/.claude"
MEMORY_DIR="${CLAUDE_HOME}/memory"
HOOKS_DIR="${CLAUDE_HOME}/hooks"
SETTINGS="${CLAUDE_HOME}/settings.json"

echo "=== Claude Memory Infrastructure Installer ==="
echo "  Source: ${SCRIPT_DIR}"
echo ""

# --- Preflight checks ---
echo "[1/5] Preflight checks..."

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "  ERROR: $1 not found. Install it first."
        exit 1
    fi
    echo "  [ok] $1"
}

check_cmd python3
check_cmd node
check_cmd npx
check_cmd git

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "  ERROR: Python 3.10+ required, found $PY_VER"
    exit 1
fi
echo "  [ok] Python $PY_VER"
echo ""

# --- Create directories ---
echo "[2/5] Creating directories..."
mkdir -p "${MEMORY_DIR}" "${HOOKS_DIR}"
echo "  [ok] ${MEMORY_DIR}/"
echo "  [ok] ${HOOKS_DIR}/"
echo ""

# --- Deploy runtime scripts ---
echo "[3/5] Deploying runtime scripts..."

# Source files must exist from git checkout
for pyfile in maintenance.py semantic_server.py; do
    if [ ! -f "${SCRIPT_DIR}/${pyfile}" ]; then
        echo "  ERROR: ${SCRIPT_DIR}/${pyfile} not found."
        echo "         Run this installer from the easy-memory-claude project directory."
        exit 1
    fi
done

cp "${SCRIPT_DIR}/maintenance.py" "${MEMORY_DIR}/maintenance.py"
cp "${SCRIPT_DIR}/semantic_server.py" "${MEMORY_DIR}/semantic_server.py"
chmod +x "${MEMORY_DIR}/maintenance.py" "${MEMORY_DIR}/semantic_server.py"
echo "  [ok] maintenance.py → ${MEMORY_DIR}/"
echo "  [ok] semantic_server.py → ${MEMORY_DIR}/"
printf '%s' "${SCRIPT_DIR}" > "${MEMORY_DIR}/.source-dir"
echo "  [ok] .source-dir → ${MEMORY_DIR}/"
echo ""

# --- Deploy hooks ---
echo "[4/5] Deploying global hooks..."

HOOK_SRC="${SCRIPT_DIR}/hooks"
for hook in prime-memory.sh capture-decisions.sh nudge-setup.sh capture-tool-context.sh; do
    if [ ! -f "${HOOK_SRC}/${hook}" ]; then
        echo "  ERROR: ${HOOK_SRC}/${hook} not found."
        echo "         Run this installer from the easy-memory-claude project directory."
        exit 1
    fi
    cp "${HOOK_SRC}/${hook}" "${HOOKS_DIR}/${hook}"
    chmod +x "${HOOKS_DIR}/${hook}"
    echo "  [ok] ${hook} → ${HOOKS_DIR}/"
done
echo ""

# --- Update settings.json ---
echo "[5/5] Configuring hooks in settings.json..."

if [ ! -f "${SETTINGS}" ]; then
    cat > "${SETTINGS}" << 'SETEOF'
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/prime-memory.sh",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/nudge-setup.sh",
            "timeout": 3
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/capture-tool-context.sh",
            "timeout": 3
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/capture-decisions.sh",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
SETEOF
    echo "  [ok] Created ${SETTINGS} with hooks"
else
    python3 - "${SETTINGS}" << 'PYEOF'
import json, sys, shutil

settings_path = sys.argv[1]

try:
    with open(settings_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError):
    print(f'  [warn] {settings_path} is corrupt — backing up and recreating')
    shutil.copy2(settings_path, settings_path + '.bak')
    cfg = {}

hooks = cfg.setdefault('hooks', {})

# Memory hook entries to ensure exist
memory_hooks = {
    'SessionStart': [
        {'type': 'command', 'command': '$HOME/.claude/hooks/prime-memory.sh', 'timeout': 10},
        {'type': 'command', 'command': '$HOME/.claude/hooks/nudge-setup.sh', 'timeout': 3},
    ],
    'PostToolUse': [
        {'type': 'command', 'command': '$HOME/.claude/hooks/capture-tool-context.sh', 'timeout': 3},
    ],
    'Stop': [
        {'type': 'command', 'command': '$HOME/.claude/hooks/capture-decisions.sh', 'timeout': 3},
    ],
}

changed = False
for event, new_entries in memory_hooks.items():
    groups = hooks.setdefault(event, [])
    # Find or create the catch-all group (matcher='')
    catch_all = None
    for g in groups:
        if g.get('matcher', '') == '':
            catch_all = g
            break
    if catch_all is None:
        catch_all = {'matcher': '', 'hooks': []}
        groups.append(catch_all)
    existing_cmds = {h.get('command', '') for h in catch_all.get('hooks', [])}
    for entry in new_entries:
        if entry['command'] not in existing_cmds:
            catch_all.setdefault('hooks', []).append(entry)
            changed = True

if changed:
    with open(settings_path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
    print(f'  [ok] Merged memory hooks into {settings_path}')
else:
    print(f'  [skip] Memory hooks already present in {settings_path}')
PYEOF
fi

echo ""
echo "============================================================"
echo "  Installation complete!"
echo ""
echo "  Runtime:       ~/.claude/memory/ (2 scripts)"
echo "  Hooks:         ~/.claude/hooks/  (4 hooks)"
echo "  Settings:      ~/.claude/settings.json"
echo "  Dev/source:    ${SCRIPT_DIR}/"
echo ""
echo "  To set up a project:"
echo "    ${SCRIPT_DIR}/setup-project.sh /path/to/project"
echo ""
echo "  Then restart Claude Code to activate MCP servers."
echo "============================================================"

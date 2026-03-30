#!/bin/bash
# Claude Memory Infrastructure — Installer
# Deploys runtime tools to ~/.claude/memory/ and hooks to ~/.claude/hooks/.
# Run from the easy-memory-claude project directory.
# Requirements: python3 3.10+, git | Platform: macOS / Linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_HOME="${HOME}/.claude"
MEMORY_DIR="${CLAUDE_HOME}/memory"
HOOKS_DIR="${CLAUDE_HOME}/hooks"
SETTINGS="${CLAUDE_HOME}/settings.json"

echo "=== Claude Memory Infrastructure Installer ==="
echo "  Source: ${SCRIPT_DIR}"
echo ""

# --- Step 1: Preflight checks (python3, git, version) ---
echo "[1/5] Preflight checks..."

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "  ERROR: $1 not found. Install it first."
        exit 1
    fi
    echo "  [ok] $1"
}

check_cmd python3
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

# --- Step 2: Create ~/.claude/memory/ and ~/.claude/hooks/ ---
echo "[2/5] Creating directories..."
mkdir -p "${MEMORY_DIR}" "${HOOKS_DIR}"
echo "  [ok] ${MEMORY_DIR}/"
echo "  [ok] ${HOOKS_DIR}/"
echo ""

# --- Step 3: Deploy runtime scripts to ~/.claude/memory/ ---
echo "[3/5] Deploying runtime scripts..."

# Verify source files exist
for src in maintenance.py semantic_server/__init__.py semantic_server/maintenance_utils.py; do
    if [ ! -f "${SCRIPT_DIR}/${src}" ]; then
        echo "  ERROR: ${SCRIPT_DIR}/${src} not found."
        echo "         Run this installer from the easy-memory-claude project directory."
        exit 1
    fi
done

# Deploy maintenance.py
cp "${SCRIPT_DIR}/maintenance.py" "${MEMORY_DIR}/maintenance.py"
chmod +x "${MEMORY_DIR}/maintenance.py"
echo "  [ok] maintenance.py → ${MEMORY_DIR}/"

# Deploy semantic_server package (clean copy)
rm -rf "${MEMORY_DIR}/semantic_server"
cp -r "${SCRIPT_DIR}/semantic_server" "${MEMORY_DIR}/semantic_server"
find "${MEMORY_DIR}/semantic_server" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
echo "  [ok] semantic_server/ → ${MEMORY_DIR}/"

# Deploy backwards-compatible entry point shim
cp "${SCRIPT_DIR}/semantic_server.py" "${MEMORY_DIR}/semantic_server.py"
chmod +x "${MEMORY_DIR}/semantic_server.py"
echo "  [ok] semantic_server.py (compat shim) → ${MEMORY_DIR}/"
printf '%s' "${SCRIPT_DIR}" > "${MEMORY_DIR}/.source-dir"
echo "  [ok] .source-dir → ${MEMORY_DIR}/"

# Deploy CLI bridge for VSCode fallback
cp "${SCRIPT_DIR}/memory-cli.py" "${MEMORY_DIR}/memory-cli.py"
chmod +x "${MEMORY_DIR}/memory-cli.py"
echo "  [ok] memory-cli.py (CLI bridge) → ${MEMORY_DIR}/"

# Deploy 'mem' CLI wrapper
cp "${SCRIPT_DIR}/mem" "${MEMORY_DIR}/mem"
chmod +x "${MEMORY_DIR}/mem"
echo "  [ok] mem (CLI wrapper) → ${MEMORY_DIR}/"

# Optional: orjson for faster graph I/O
if python3 -c "import orjson" 2>/dev/null; then
    echo "  [ok] orjson already installed"
else
    echo ""
    echo "  orjson is an optional dependency that speeds up graph I/O by 3-10x."
    echo "  The server works fine without it (falls back to stdlib json)."
    printf "  Install orjson now? [y/N] "
    read -r ans
    case "$ans" in
        [yY]|[yY][eE][sS])
            if python3 -m pip install --user --quiet "orjson>=3.9" 2>/dev/null; then
                echo "  [ok] orjson installed"
            else
                echo "  [skip] pip install failed — continuing with stdlib json"
            fi
            ;;
        *)
            echo "  [skip] orjson — using stdlib json"
            ;;
    esac
fi
echo ""

# --- Step 4: Deploy hook scripts to ~/.claude/hooks/ ---
echo "[4/5] Deploying global hooks..."

HOOK_SRC="${SCRIPT_DIR}/hooks"
for hook in prime-memory.sh capture-decisions.sh nudge-setup.sh capture-tool-context.sh capture_tool_context.py smart_recall.py; do
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

# --- Step 5: Wire hooks into settings.json ---
echo "[5/5] Configuring hooks in settings.json..."

if [ ! -f "${SETTINGS}" ]; then
    # Create fresh settings with all memory hooks
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
    # Merge memory hooks into existing settings (idempotent)
    python3 - "${SETTINGS}" << 'PYEOF'
import json, os, sys, shutil

settings_path = sys.argv[1]

try:
    with open(settings_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError):
    print(f'  [warn] {settings_path} is corrupt — backing up and recreating')
    shutil.copy2(settings_path, settings_path + '.bak')
    cfg = {}

hooks = cfg.setdefault('hooks', {})

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
    # Find or create catch-all matcher group
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
    tmp = settings_path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, settings_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f'  [ok] Merged memory hooks into {settings_path}')
else:
    print(f'  [skip] Memory hooks already present in {settings_path}')
PYEOF
fi

echo ""
echo "============================================================"
echo "  Installation complete!"
echo ""
echo "  Runtime:       ~/.claude/memory/ (5 files + semantic_server package)"
echo "  Hooks:         ~/.claude/hooks/  (4 shell + 2 Python)"
echo "  Settings:      ~/.claude/settings.json"
echo "  Dev/source:    ${SCRIPT_DIR}/"
echo ""
echo "  To use the 'mem' command globally:"
echo "    export PATH=\"\$HOME/.claude/memory:\$PATH\""
echo "    (Add this to your ~/.bashrc or ~/.zshrc)"
echo ""
echo "  To set up a project:"
echo "    ${SCRIPT_DIR}/setup-project.sh /path/to/project"
echo ""
echo "  Then restart Claude Code to activate hooks."
echo "============================================================"

#!/bin/bash
# ============================================================
# easy-memory-claude — Cleanup Utility
# ============================================================
# Removes all easy-memory-claude artifacts for a fresh start.
#
# Modes:
#   cleanup.sh project [dir]   Remove per-project memory data
#   cleanup.sh global          Remove global runtime + hooks
#   cleanup.sh all [dir]       Remove everything (global + project)
#
# Safe: prompts before each destructive step unless --yes flag.
# ============================================================
set -euo pipefail

CLAUDE_HOME="${HOME}/.claude"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
    echo "Usage: cleanup.sh <mode> [options]"
    echo ""
    echo "Modes:"
    echo "  project [dir]   Remove .memory/, .mcp.json memory entry,"
    echo "                  .vscode/mcp.json memory entry from a project"
    echo "                  (defaults to current directory)"
    echo "  global          Remove ~/.claude/memory/, hooks, settings entries"
    echo "  all [dir]       Both global + project cleanup"
    echo ""
    echo "Options:"
    echo "  --yes           Skip confirmation prompts"
    echo "  --dry-run       Show what would be removed without deleting"
    exit 1
}

# --- Parse args ---
MODE=""
PROJECT_DIR=""
AUTO_YES=false
DRY_RUN=false

while [ $# -gt 0 ]; do
    case "$1" in
        project|global|all) MODE="$1" ;;
        --yes) AUTO_YES=true ;;
        --dry-run) DRY_RUN=true ;;
        --help|-h) usage ;;
        *)
            if [ -z "$PROJECT_DIR" ] && [ -d "$1" ]; then
                PROJECT_DIR="$1"
            else
                echo "Unknown argument: $1"
                usage
            fi
            ;;
    esac
    shift
done

[ -z "$MODE" ] && usage

if [ "$MODE" = "project" ] || [ "$MODE" = "all" ]; then
    PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
    PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd)" || {
        echo "ERROR: Directory not found: $PROJECT_DIR"
        exit 1
    }
fi

confirm() {
    if $AUTO_YES || $DRY_RUN; then
        return 0
    fi
    printf "${YELLOW}  %s [y/N] ${NC}" "$1"
    read -r ans
    case "$ans" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

remove_path() {
    local path="$1"
    local label="$2"
    if [ -e "$path" ]; then
        if $DRY_RUN; then
            echo "  [dry-run] Would remove: $path"
        else
            rm -rf "$path"
            printf "  ${GREEN}[removed]${NC} %s\n" "$label"
        fi
    else
        echo "  [skip] $label — not found"
    fi
}

# ============================================================
# Project cleanup
# ============================================================
cleanup_project() {
    local dir="$1"
    echo ""
    printf "${RED}=== Project Cleanup: ${dir} ===${NC}\n"

    # 1. Remove .memory/ directory
    if [ -d "${dir}/.memory" ]; then
        ENTITY_COUNT=0
        if [ -f "${dir}/.memory/graph.jsonl" ]; then
            ENTITY_COUNT=$(grep -c '"entity"' "${dir}/.memory/graph.jsonl" 2>/dev/null || echo 0)
        fi
        echo "  Found .memory/ with ~${ENTITY_COUNT} entities"
        if confirm "Delete ${dir}/.memory/ ?"; then
            remove_path "${dir}/.memory" ".memory/"
        fi
    else
        echo "  [skip] .memory/ — not found"
    fi

    # 2. Remove memory entry from .mcp.json
    if [ -f "${dir}/.mcp.json" ]; then
        python3 - "${dir}/.mcp.json" "$DRY_RUN" << 'PYEOF'
import json, sys, os

mcp_path = sys.argv[1]
dry_run = sys.argv[2] == 'true'

try:
    with open(mcp_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError, OSError):
    print('  [skip] .mcp.json — unreadable')
    sys.exit(0)

servers = cfg.get('mcpServers', {})
removed = []
for key in list(servers.keys()):
    s = servers[key]
    if not isinstance(s, dict):
        continue
    args = s.get('args', [])
    env = s.get('env', {})
    is_memory = (
        'semantic_server.py' in str(args)
        or 'server-memory' in str(args)
        or 'MEMORY_DIR' in env
        or 'MEMORY_FILE_PATH' in env
        or key in ('memory', 'memory-search',
                    'memory-semantic-search')
    )
    if is_memory:
        removed.append(key)
        del servers[key]

if not removed:
    print('  [skip] .mcp.json — no memory servers found')
    sys.exit(0)

if dry_run:
    for k in removed:
        print(f'  [dry-run] Would remove server "{k}" from .mcp.json')
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
    print(f'  \033[0;32m[removed]\033[0m .mcp.json server "{k}"')
PYEOF
    else
        echo "  [skip] .mcp.json — not found"
    fi

    # 3. Remove memory entry from .vscode/mcp.json
    if [ -f "${dir}/.vscode/mcp.json" ]; then
        python3 - "${dir}/.vscode/mcp.json" "$DRY_RUN" << 'PYEOF'
import json, sys, os

mcp_path = sys.argv[1]
dry_run = sys.argv[2] == 'true'

try:
    with open(mcp_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError, OSError):
    print('  [skip] .vscode/mcp.json — unreadable')
    sys.exit(0)

servers = cfg.get('servers', {})
removed = []
for key in list(servers.keys()):
    s = servers[key]
    if not isinstance(s, dict):
        continue
    args = s.get('args', [])
    env = s.get('env', {})
    is_memory = (
        'semantic_server.py' in str(args)
        or 'server-memory' in str(args)
        or 'MEMORY_DIR' in env
        or 'MEMORY_FILE_PATH' in env
        or key in ('memory', 'memory-search',
                    'memory-semantic-search')
    )
    if is_memory:
        removed.append(key)
        del servers[key]

if not removed:
    print('  [skip] .vscode/mcp.json — no memory servers found')
    sys.exit(0)

if dry_run:
    for k in removed:
        print(f'  [dry-run] Would remove server "{k}" from .vscode/mcp.json')
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
    # Remove .vscode/ if now empty
    vscode_dir = os.path.dirname(mcp_path)
    if os.path.isdir(vscode_dir) and not os.listdir(vscode_dir):
        os.rmdir(vscode_dir)

for k in removed:
    print(f'  \033[0;32m[removed]\033[0m .vscode/mcp.json server "{k}"')
PYEOF
    else
        echo "  [skip] .vscode/mcp.json — not found"
    fi

    # 4. Remove .memory/ from .gitignore
    if [ -f "${dir}/.gitignore" ]; then
        if grep -q '\.memory/' "${dir}/.gitignore" 2>/dev/null; then
            if $DRY_RUN; then
                echo "  [dry-run] Would remove .memory/ lines from .gitignore"
            else
                # Remove the .memory/ line and preceding comment if it's "# Memory"
                python3 - "${dir}/.gitignore" << 'PYEOF'
import sys

path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    lines = f.readlines()

out = []
skip_next = False
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped == '.memory/' or stripped == '.memory':
        # Also remove preceding "# Memory" comment
        if out and out[-1].strip() == '# Memory':
            out.pop()
        # Remove trailing blank line if it was preceded by blank
        continue
    out.append(line)

# Remove trailing blank lines
while out and out[-1].strip() == '':
    out.pop()
if out:
    out[-1] = out[-1].rstrip('\n') + '\n'

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(out)
print('  \033[0;32m[removed]\033[0m .memory/ from .gitignore')
PYEOF
            fi
        else
            echo "  [skip] .gitignore — no .memory/ entry"
        fi
    fi

    # 5. Clean up temp nudge marker for this specific project
    if ! $DRY_RUN; then
        # Compute same hash as nudge-setup.sh
        _proj_hash=""
        if command -v md5 &>/dev/null; then
            _proj_hash=$(md5 -q -s "$dir")
        elif command -v md5sum &>/dev/null; then
            _proj_hash=$(echo -n "$dir" | md5sum | cut -d' ' -f1)
        else
            _proj_hash=$(echo -n "$dir" | cksum | cut -d' ' -f1)
        fi
        rm -f "/tmp/.claude-mem-nudge-${_proj_hash}" 2>/dev/null || true
    fi
}

# ============================================================
# Global cleanup
# ============================================================
cleanup_global() {
    echo ""
    printf "${RED}=== Global Cleanup ===${NC}\n"

    # 1. Remove ~/.claude/memory/
    if [ -d "${CLAUDE_HOME}/memory" ]; then
        echo "  Found ~/.claude/memory/"
        if confirm "Delete ${CLAUDE_HOME}/memory/ ?"; then
            remove_path "${CLAUDE_HOME}/memory" "~/.claude/memory/"
        fi
    else
        echo "  [skip] ~/.claude/memory/ — not found"
    fi

    # 2. Remove hooks
    HOOKS=(prime-memory.sh capture-decisions.sh nudge-setup.sh capture-tool-context.sh)
    for hook in "${HOOKS[@]}"; do
        remove_path "${CLAUDE_HOME}/hooks/${hook}" "~/.claude/hooks/${hook}"
    done
    # Remove hooks dir if empty
    if [ -d "${CLAUDE_HOME}/hooks" ] && [ -z "$(ls -A "${CLAUDE_HOME}/hooks" 2>/dev/null)" ]; then
        if ! $DRY_RUN; then
            rmdir "${CLAUDE_HOME}/hooks" 2>/dev/null || true
        fi
    fi

    # 3. Remove hook wiring from settings.json
    if [ -f "${CLAUDE_HOME}/settings.json" ]; then
        python3 - "${CLAUDE_HOME}/settings.json" "$DRY_RUN" << 'PYEOF'
import json, sys

settings_path = sys.argv[1]
dry_run = sys.argv[2] == 'true'

try:
    with open(settings_path, encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError, OSError):
    print('  [skip] settings.json — unreadable')
    sys.exit(0)

hooks = cfg.get('hooks', {})
if not hooks:
    print('  [skip] settings.json — no hooks section')
    sys.exit(0)

memory_commands = {
    '$HOME/.claude/hooks/prime-memory.sh',
    '$HOME/.claude/hooks/capture-decisions.sh',
    '$HOME/.claude/hooks/nudge-setup.sh',
    '$HOME/.claude/hooks/capture-tool-context.sh',
}

changed = False
for event in list(hooks.keys()):
    groups = hooks[event]
    if not isinstance(groups, list):
        continue
    for group in groups:
        hook_list = group.get('hooks', [])
        original_len = len(hook_list)
        group['hooks'] = [
            h for h in hook_list
            if h.get('command', '') not in memory_commands
        ]
        if len(group['hooks']) < original_len:
            changed = True
    # Remove empty groups
    hooks[event] = [
        g for g in groups
        if g.get('hooks')
    ]
    # Remove empty events
    if not hooks[event]:
        del hooks[event]
        changed = True

if not changed:
    print('  [skip] settings.json — no memory hooks found')
    sys.exit(0)

if dry_run:
    print('  [dry-run] Would remove memory hooks from settings.json')
    sys.exit(0)

# Remove hooks key entirely if empty
if not hooks:
    del cfg['hooks']

import os
tmp = settings_path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, settings_path)
print('  \033[0;32m[removed]\033[0m memory hooks from settings.json')
PYEOF
    fi

    # 4. Clean up all temp markers
    if ! $DRY_RUN; then
        rm -f /tmp/.claude-mem-* 2>/dev/null || true
    fi
}

# ============================================================
# Execute
# ============================================================
case "$MODE" in
    project)
        cleanup_project "$PROJECT_DIR"
        ;;
    global)
        cleanup_global
        ;;
    all)
        cleanup_project "$PROJECT_DIR"
        cleanup_global
        ;;
esac

echo ""
if $DRY_RUN; then
    printf "${YELLOW}Dry run complete — no files were modified.${NC}\n"
    echo "Run without --dry-run to apply changes."
else
    printf "${GREEN}Cleanup complete.${NC}\n"
    if [ "$MODE" = "global" ] || [ "$MODE" = "all" ]; then
        echo "Restart Claude Code to apply changes."
        echo "Re-run install.sh + setup-project.sh for a fresh start."
    elif [ "$MODE" = "project" ]; then
        echo "Re-run setup-project.sh to reinitialize this project."
    fi
fi

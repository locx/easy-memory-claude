# easy-memory-claude

Persistent knowledge graph memory for Claude Code. Works across all projects.

## Prerequisites

> **All three must be available in your shell's `$PATH` before running `install.sh`.**

| Requirement | Minimum | Check |
|-------------|---------|-------|
| **Python** | 3.10+ | `python3 --version` |
| **Node.js** | 18+ | `node --version` |
| **npx** | (ships with Node) | `npx --version` |
| **Git** | any | `git --version` |

- **macOS**: `brew install python node git`
- **Ubuntu/Debian**: `sudo apt install python3 nodejs npm git`
- **Arch**: `sudo pacman -S python nodejs npm git`

No Python packages, no `pip install`, no virtual environment needed. Everything runs on the standard library.

## Why Use This Over CLAUDE.md / MEMORY.md

| | Flat files (`CLAUDE.md`) | easy-memory-claude (knowledge graph) |
|---|---|---|
| **Structure** | Linear text, no relationships | Graph of entities, relations, observations — Claude traverses connections |
| **Search** | Keyword grep only | Keyword + TF-IDF semantic similarity (finds "sync conflict resolution" even if those exact words aren't stored) |
| **Maintenance** | Manual — you prune and organize | Automatic — daily decay scoring, pruning, deduplication, index rebuild |
| **Growth** | Unbounded, degrades context quality | Self-regulating — stale low-value entities are pruned, duplicates merged |
| **Cross-session** | Requires manual "remember X" instructions | Automatic — hooks inject graph summary at session start, remind to persist at session end |
| **Multi-project** | Separate files per project, no shared infra | Global infrastructure, per-project graphs, one-command setup |
| **Resilience** | File gets too large, context window wasted | Atomic writes, auto-backup, graceful degradation on missing files |
| **Discovery** | Must know what to search for | Semantic search surfaces related knowledge you didn't think to look for |

### When flat files are still better

- **Static rules** that never change (code style, project constraints) — keep those in `CLAUDE.md`
- **Short-lived projects** where you won't have more than a few sessions
- **Projects with <10 facts** to remember — the overhead isn't worth it

easy-memory-claude complements `CLAUDE.md` — it doesn't replace it. Use `CLAUDE.md` for rules and conventions, use the knowledge graph for evolving project knowledge.

## The Problem

Claude Code starts every conversation from zero. Your `CLAUDE.md` and flat `MEMORY.md` files help, but they're:
- **Manual** — you must remember to update them
- **Unstructured** — linear text with no relationships between concepts
- **Unsearchable** — keyword grep only, no semantic understanding
- **Unbounded** — they grow forever with no decay or consolidation

## How It Works

easy-memory-claude wraps the official **MCP Memory Server** (from the Model Context Protocol team) with three augmentations that run as pure Python — zero external dependencies.

### Knowledge Graph (MCP Memory Server)

Claude stores what it learns as a graph of **entities**, **relations**, and **observations**:

```
[SyncManager] --uses--> [ProviderRegistry]
     |                        |
     |-- "Uses LWW            |-- "Singleton pattern"
         resolution"          |-- "resolve() is async"
     |-- "Custom HTTP sync"
```

This is richer than flat text — Claude can traverse relationships, not just grep keywords.

### Automatic Maintenance (maintenance.py)

Runs once per day on session start. Pure Python, no deps.

1. **Backup** — copies `graph.jsonl` before any mutation
2. **Score** — each entity gets `relevance = observation_count / (1 + days_stale)`
3. **Prune** — removes low-score entities with zero inbound relations
4. **Consolidate** — merges entities with overlapping names and same type
5. **Index** — rebuilds TF-IDF vectors for semantic search
6. **Stamp** — tags entities with `_branch` and `_updated` metadata

All writes are atomic (temp file + `os.replace`).

### Semantic Search (semantic_server.py)

A lightweight MCP server exposing one tool: `semantic_search_memory`. Uses TF-IDF cosine similarity over entity observations — no vector database, no embeddings model, no network calls.

Query "sync conflict resolution" and it returns:
```json
{
  "results": [
    {
      "entity": "SyncManager",
      "score": 0.5137,
      "entityType": "component",
      "observations": ["Uses LWW resolution", "Custom HTTP sync"]
    }
  ]
}
```

### Lifecycle Hooks

Two global hooks fire automatically for every project with a `.memory/` directory:

| Hook | When | What |
|------|------|------|
| `prime-memory.sh` | Session start | Runs maintenance, injects graph summary into context |
| `capture-decisions.sh` | Session end | One-time reminder to persist important decisions |
| `nudge-setup.sh` | Session start | Notifies if the current project has no memory setup |
| `capture-tool-context.sh` | Post tool use | Captures observations from file edits, writes, and shell commands (throttled to 1x/30s) |

Projects without `.memory/` are silently skipped — no noise, no errors.

### Per-Project Configuration

After running `setup-project.sh`, edit `.memory/config.json` to override defaults:

```json
{
  "decay_threshold": 0.1,
  "max_age_days": 90,
  "throttle_hours": 24,
  "min_merge_name_len": 4
}
```

Delete any key to use the default. Long-lived projects may want `max_age_days: 365`; fast-moving prototypes may prefer `throttle_hours: 12`.

### Cross-Machine Sync

Export and import memory graphs between machines:

```bash
# On source machine
~/projects/easy-memory-claude/export-memory.sh /path/to/project

# Transfer the .json bundle, then on target machine
~/projects/easy-memory-claude/import-memory.sh bundle.json /path/to/project
```

Import merges entities (deduplicating by name+type) and relations. A backup is created before merging.

## Architecture

```
~/.claude/                          GLOBAL — runtime only (lean)
  settings.json                     Hook wiring
  hooks/
    prime-memory.sh                 SessionStart: maintenance + context
    capture-decisions.sh            Stop: persist reminder
    nudge-setup.sh                  SessionStart: setup nudge
    capture-tool-context.sh         PostToolUse: observation capture
  memory/
    maintenance.py                  Decay / prune / consolidate / TF-IDF
    semantic_server.py              MCP server for semantic search

~/projects/easy-memory-claude/      SOURCE — install + dev
  install.sh                        Deploys runtime to ~/.claude/
  setup-project.sh                  Initializes any project
  export-memory.sh                  Export graph to portable bundle
  import-memory.sh                  Import/merge bundle into project
  *.py                              Source copies
  README.md                         This file

<any-project>/                      PER-PROJECT — gitignored
  .memory/
    graph.jsonl                     Knowledge graph
    tfidf_index.json                Search index
    config.json                     Per-project decay/throttle overrides
    graph.jsonl.bak                 Auto-backup
    pruned.log                      Maintenance log
  .vscode/mcp.json                  MCP server config
```

## Installation

> Verify [prerequisites](#prerequisites) first — `python3 --version` should show 3.10+, `node --version` 18+.

### New Machine

```bash
# 1. Get the project
git clone <repo-url> ~/projects/easy-memory-claude
# or copy it:
cp -r /path/to/easy-memory-claude ~/projects/easy-memory-claude

# 2. Deploy runtime to ~/.claude/
cd ~/projects/easy-memory-claude
chmod +x install.sh
./install.sh
```

The installer will:
1. Verify prerequisites (python3, node, npx, git)
2. Deploy `maintenance.py` and `semantic_server.py` to `~/.claude/memory/`
3. Deploy hooks to `~/.claude/hooks/`
4. Wire hooks into `~/.claude/settings.json` (safe JSON merge — preserves existing config)

### New Project

```bash
~/projects/easy-memory-claude/setup-project.sh /path/to/your/project
```

This creates:
- `.memory/` directory (gitignored)
- `.vscode/mcp.json` with both MCP servers configured
- Adds `.memory/` to `.gitignore` if not already present

Then **restart Claude Code** to activate the MCP servers.

### Verify

```bash
# Check runtime is deployed
ls ~/.claude/memory/maintenance.py ~/.claude/hooks/prime-memory.sh

# Check project is initialized
ls /path/to/project/.memory/ /path/to/project/.vscode/mcp.json

# Test maintenance manually
python3 ~/.claude/memory/maintenance.py /path/to/project
```

## Usage

Once installed, memory works transparently:

**Session start** — Claude sees a graph summary:
```
=== Memory Graph: 12 entities, 8 relations ===
  SyncManager (component) -- 3 obs
  ProviderRegistry (component) -- 2 obs
  ...
Keyword: search_nodes/open_nodes | Semantic: semantic_search_memory
```

**During conversation** — Claude uses MCP tools:
- `create_entities` / `create_relations` — store new knowledge
- `search_nodes` — keyword search
- `semantic_search_memory` — TF-IDF similarity search
- `open_nodes` — retrieve entity details + connections

**Session end** — Claude gets a reminder to persist decisions.

**Daily maintenance** — stale entities are pruned, duplicates merged, index rebuilt. All automatic.

## Resilience

| Scenario | Behavior |
|----------|----------|
| Project venv deleted | No effect — all tools use system `python3`, pure Python |
| Branch switch | Hooks live in global `~/.claude/`, not project tree. Memory persists (gitignored). Entities stamped with `_branch` |
| graph.jsonl corrupt | Backup taken before every maintenance run. Atomic writes prevent partial corruption |
| graph.jsonl missing | Hooks exit 0 gracefully — no errors, no blocking |
| Project not initialized | Hooks skip silently. Nudge hook shows one-time notice |
| npm cache cleared | `npx` re-downloads MCP server transparently |

### Self-Guard Pattern

Every hook guards against missing project state:
```bash
[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] || exit 0   # project initialized?
[ -n "${CLAUDE_SESSION_ID:-}" ] || exit 0            # session context available?
```

Always `exit 0` — never blocks Claude.

## 12 MCP Tools Available

**Keyword (official MCP Memory Server):**

| Tool | Purpose |
|------|---------|
| `create_entities` | Add nodes with type + observations |
| `create_relations` | Directed edges between entities |
| `add_observations` | Append facts to existing entities |
| `delete_entities` | Remove with cascading relation cleanup |
| `delete_observations` | Remove specific facts |
| `delete_relations` | Remove connections |
| `read_graph` | Export complete graph |
| `search_nodes` | Keyword search across names, types, observations |
| `open_nodes` | Retrieve specific entities + their connections |

**Semantic (custom TF-IDF server):**

| Tool | Purpose |
|------|---------|
| `semantic_search_memory` | Cosine similarity search, ranked results with observations |
| `traverse_relations` | BFS graph traversal from a start entity, returns connected subgraph |
| `search_memory_by_time` | Find entities by time range, sorted by most recent |

## Comparison with Alternatives

See [competition-comparison.md](competition-comparison.md) for a detailed feature, performance, and stability comparison with [Hebbs](https://github.com/hebbs-ai/hebbs) (Rust-based cognitive memory engine) and [Claude-Mem](https://github.com/thedotmack/claude-mem) (Claude Code plugin with lifecycle hooks).

## License

MIT

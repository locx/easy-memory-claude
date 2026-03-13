# easy-memory-claude

Persistent knowledge graph memory for Claude Code. Works across all projects.

Pure Python, zero external dependencies, zero configuration required.

## Prerequisites

> **Both must be available in your shell's `$PATH` before running `install.sh`.**

| Requirement | Minimum | Check |
|-------------|---------|-------|
| **Python** | 3.10+ | `python3 --version` |
| **Git** | any | `git --version` |

- **macOS**: `brew install python git`
- **Ubuntu/Debian**: `sudo apt install python3 git`
- **Arch**: `sudo pacman -S python git`

No Node.js, no Python packages, no `pip install`, no virtual environment needed. Everything runs on the standard library.

## Why Does This Exist?

Claude Code starts every conversation with a blank slate. Without memory infrastructure, Claude forgets everything the moment a session ends — your architecture decisions, the bugs you've already fixed, the patterns your team prefers. You end up repeating context in every single conversation.

Flat files like `CLAUDE.md` and `MEMORY.md` help, but they have real limitations:

- **Manual upkeep** — you have to remember to write things down and keep them organized
- **No structure** — it's just text in a file, with no way to express relationships between concepts
- **No search intelligence** — you can only find things by exact keyword matches
- **Unbounded growth** — they grow forever with no automatic cleanup, eventually wasting Claude's context window

### How easy-memory-claude Solves This

easy-memory-claude gives Claude a **knowledge graph** — a structured network of entities, relationships, and observations that persists across sessions. Think of it like a second brain for Claude that:

1. **Remembers automatically** — lifecycle hooks capture what Claude does during each session
2. **Organizes itself** — daily maintenance prunes stale knowledge and merges duplicates
3. **Searches intelligently** — TF-IDF similarity finds related concepts even when the exact words don't match
4. **Scales gracefully** — the graph self-regulates its size through decay scoring and consolidation

### Comparison With Flat Files

| | Flat files (`CLAUDE.md`) | easy-memory-claude (knowledge graph) |
|---|---|---|
| **Structure** | Linear text, no relationships | Graph of entities, relations, observations — Claude traverses connections |
| **Search** | Keyword grep only | Keyword + TF-IDF semantic similarity (finds "sync conflict resolution" even if those exact words aren't stored) |
| **Maintenance** | Manual — you prune and organize | Automatic — daily decay scoring, pruning, deduplication, index rebuild |
| **Growth** | Unbounded, degrades context quality | Self-regulating — stale low-value entities are pruned, duplicates merged |
| **Cross-session** | Requires manual "remember X" instructions | Automatic — hooks inject graph summary at session start, remind to persist at session end |
| **Multi-project** | Separate files per project, no shared infra | Global infrastructure, per-project graphs, one-command setup |
| **Resilience** | File gets too large, context window wasted | Atomic writes with fsync, auto-backup, file locking, graceful degradation |
| **Discovery** | Must know what to search for | Semantic search surfaces related knowledge you didn't think to look for |

### When Flat Files Are Still Better

- **Static rules** that never change (code style, project constraints) — keep those in `CLAUDE.md`
- **Short-lived projects** where you won't have more than a few sessions
- **Projects with <10 facts** to remember — the overhead isn't worth it

easy-memory-claude **complements** `CLAUDE.md` — it doesn't replace it. Use `CLAUDE.md` for rules and conventions, use the knowledge graph for evolving project knowledge.

## How It Works — The Big Picture

There are four main pieces working together. If you're new to this, read them in order — each one builds on the previous.

### 1. The Knowledge Graph (Your Data)

All memory is stored in a single file called `graph.jsonl` inside each project's `.memory/` directory. It uses a format called JSONL (JSON Lines) — each line is one JSON object.

There are two kinds of objects in the graph:

**Entities** — things Claude knows about. Each entity has a name, a type, and a list of observations (facts):

```json
{"type":"entity","name":"SyncManager","entityType":"component","observations":["Uses LWW resolution","Custom HTTP sync","Handles offline queue"]}
```

**Relations** — directed connections between entities:

```json
{"type":"relation","from":"SyncManager","to":"ProviderRegistry","relationType":"uses"}
```

Together, these form a graph that Claude can traverse:

```
[SyncManager] --uses--> [ProviderRegistry]
     |                        |
     |-- "Uses LWW            |-- "Singleton pattern"
         resolution"          |-- "resolve() is async"
     |-- "Custom HTTP sync"
```

This is much richer than flat text. Instead of searching for keywords, Claude can follow relationships to discover connected knowledge it didn't know to look for.

### 2. The MCP Server (How Claude Talks to the Graph)

The MCP server (`semantic_server/` package) is a long-running process that Claude Code communicates with. It speaks a protocol called JSON-RPC 2.0 over stdio — don't worry about the details, Claude handles all of this automatically.

The server exposes **7 tools** that Claude can call:

**Reading tools:**
- `semantic_search_memory` — finds entities by meaning, not just keywords (uses TF-IDF cosine similarity)
- `traverse_relations` — follows connections from one entity to find related ones (BFS graph traversal)
- `search_memory_by_time` — finds recently updated entities

**Writing tools:**
- `create_entities` — stores new knowledge (merges if the entity already exists)
- `create_relations` — creates connections between entities
- `add_observations` — adds new facts to existing entities
- `delete_entities` — removes entities and their relations

All write operations use **file locking** (to prevent corruption from concurrent writes) and **atomic writes** (to prevent partial writes from crashes). If a write fails, the graph stays intact.

### 3. Automatic Maintenance (Self-Cleaning)

The maintenance script (`maintenance.py`) runs automatically once per day when you start a new Claude session. It keeps the graph healthy:

1. **Backup** — copies `graph.jsonl` before any mutation (hard link for O(1), fallback to copy)
2. **Stamp** — tags new entities with `_branch` and `_created` metadata
3. **Score** — calculates `relevance = observation_count / (1 + days_stale)` with Hebbian recall boost (entities Claude searches for more get boosted)
4. **Prune** — removes low-score entities that have zero inbound relations
5. **Consolidate** — merges entities with overlapping names and same type (token-indexed to avoid O(n²) pairwise comparison)
6. **Cap observations** — activity-log entities capped at 50, others at 200 (keeps newest)
7. **Prune recall counts** — removes recall tracking for entities that no longer exist
8. **Build TF-IDF index** — rebuilds search vectors and postings for semantic search

All writes are atomic (temp file + `fsync` + `os.replace`). Concurrent access is protected by `flock`.

### 4. Lifecycle Hooks (Automatic Triggers)

Four global hooks fire automatically for every project with a `.memory/` directory. They require zero manual intervention:

| Hook | When It Fires | What It Does |
|------|---------------|-------------|
| `prime-memory.sh` | Session start | Runs maintenance (if due), injects graph summary into Claude's context |
| `capture-tool-context.sh` | After each tool use | Captures observations from file edits, writes, and shell commands (throttled to 1x/30s) |
| `capture-decisions.sh` | Session end | One-time reminder to persist important decisions |
| `nudge-setup.sh` | Session start | Notifies if the current project has no memory setup |

Projects without `.memory/` are silently skipped — no noise, no errors.

## Architecture

### Directory Layout

```
~/.claude/                          GLOBAL — runtime only (lean)
  settings.json                     Hook wiring
  hooks/
    prime-memory.sh                 SessionStart: maintenance + context
    capture-decisions.sh            Stop: persist reminder
    nudge-setup.sh                  SessionStart: setup nudge
    capture-tool-context.sh         PostToolUse: observation capture
    capture_tool_context.py         PostToolUse: Python worker
  memory/
    maintenance.py                  Decay / prune / consolidate / TF-IDF
    semantic_server/                MCP server package (see below)
    semantic_server.py              Backwards-compatible shim

~/projects/easy-memory-claude/      SOURCE — install + dev
  install.sh                        Deploys runtime to ~/.claude/
  setup-project.sh                  Initializes any project
  cleanup.sh                        Remove artifacts (project/global/all)
  export-memory.sh                  Export graph to portable JSON bundle
  import-memory.sh                  Import/merge bundle into project
  maintenance.py                    Source copy
  semantic_server.py                Backwards-compatible shim (source)
  semantic_server/                  MCP server package (source)
  README.md                         This file

<any-project>/                      PER-PROJECT — gitignored
  .mcp.json                         MCP server config (Claude Code CLI)
  .memory/
    graph.jsonl                     Knowledge graph (JSONL)
    tfidf_index.json                Search index (rebuilt daily)
    config.json                     Per-project overrides
    graph.jsonl.bak                 Auto-backup before maintenance
    .graph.lock                     Write lock (flock)
    recall_counts.json              Hebbian recall frequency data
    pruned.log                      Maintenance log
  .vscode/mcp.json                  MCP server config (VS Code)
```

### MCP Server Package Structure

The MCP server is organized as a Python package (`semantic_server/`) with each module handling a distinct responsibility:

```
semantic_server/
  __init__.py      Package entry point — exposes main()
  __main__.py      Allows: python3 -m semantic_server
  config.py        Constants, limits, pre-compiled regex
  cache.py         Mtime-based caches with size-aware eviction
  recall.py        Hebbian recall tracking (OrderedDict LRU)
  graph.py         JSONL parsing, loading, locking, atomic writes
  search.py        TF-IDF cosine similarity + time-based search
  traverse.py      BFS relation traversal with cached adjacency
  tools.py         Write operations (create, update, delete)
  protocol.py      MCP tool schemas + JSON-RPC 2.0 dispatch
  server.py        Main stdio loop, signal handling
```

| Module | Responsibility | Key Exports |
|--------|---------------|-------------|
| `config.py` | All tunable constants and limits | `MAX_CACHE_BYTES`, `PROTOCOL_VERSION`, `now_iso()` |
| `cache.py` | In-memory caches for index, entities, relations, adjacency | `estimate_size()`, `maybe_evict_caches()` |
| `recall.py` | Tracks how often entities appear in search results (Hebbian reinforcement) | `record_recalls()`, `flush_recall_counts()` |
| `graph.py` | All disk I/O for `graph.jsonl` — parse, cache, lock, append, rewrite | `load_graph_entities()`, `GraphLock`, `append_jsonl()` |
| `search.py` | TF-IDF cosine similarity search + time-range queries | `search()`, `search_by_time()` |
| `traverse.py` | BFS graph traversal with cached adjacency lists | `traverse_relations()` |
| `tools.py` | CRUD operations that mutate the graph | `create_entities()`, `delete_entities()` |
| `protocol.py` | MCP tool schema definitions + JSON-RPC message routing | `TOOLS`, `handle_message()` |
| `server.py` | Stdio event loop, signal handling, cooperative index reload | `main()` |

**Dependency flow** (no circular imports):
```
config ← cache ← recall
                ← graph ← search ← protocol ← server
                         ← traverse
                         ← tools
```

## Installation

> Verify [prerequisites](#prerequisites) first — `python3 --version` should show 3.10+.

### Step 1: Get the Project

```bash
# Clone the repository
git clone <repo-url> ~/projects/easy-memory-claude

# Or copy from another machine:
cp -r /path/to/easy-memory-claude ~/projects/easy-memory-claude
```

### Step 2: Install the Runtime

```bash
cd ~/projects/easy-memory-claude
chmod +x install.sh
./install.sh
```

The installer will:
1. Verify prerequisites (python3 3.10+, git)
2. Deploy `maintenance.py` and the `semantic_server/` package to `~/.claude/memory/`
3. Deploy 5 hook files to `~/.claude/hooks/`
4. Wire hooks into `~/.claude/settings.json` (safe JSON merge — preserves existing config)

### Step 3: Set Up a Project

```bash
~/projects/easy-memory-claude/setup-project.sh /path/to/your/project
```

This creates:
- `.memory/` directory with empty `graph.jsonl` (gitignored)
- `.mcp.json` for Claude Code CLI + `.vscode/mcp.json` for VS Code
- Bootstraps graph by scanning project structure (up to 200 files, dirs, tech stack)
- Builds TF-IDF index immediately
- Adds `.memory/` to `.gitignore` if not already present

Then **restart Claude Code** to activate the MCP server.

### Verify Installation

```bash
# Check runtime is deployed
ls ~/.claude/memory/maintenance.py ~/.claude/memory/semantic_server/__init__.py

# Check project is initialized
ls /path/to/project/.memory/ /path/to/project/.mcp.json

# Test maintenance manually
python3 ~/.claude/memory/maintenance.py /path/to/project
```

## Usage

Once installed, memory works transparently — you don't need to do anything special.

### What Happens Automatically

**Session start** — Claude sees a graph summary injected by the `prime-memory.sh` hook:
```
=== Memory Graph: 12 entities, 8 relations ===
  SyncManager (component) -- 3 obs
  ProviderRegistry (component) -- 2 obs
  ...
Semantic search available: use semantic_search_memory tool.
Search: semantic_search_memory | Traverse: traverse_relations | Write: create_entities
```

**During conversation** — Claude uses MCP tools to read and write the graph. You can also ask Claude to remember things explicitly ("remember that we decided to use LWW for conflict resolution").

**After tool calls** — The `capture-tool-context.sh` hook automatically records what files Claude edited, created, or what commands it ran. This builds a session activity log in the graph.

**Session end** — Claude gets a one-time reminder to persist important decisions.

**Daily maintenance** — stale entities are pruned, duplicates merged, search index rebuilt. All automatic.

> **Note:** Entities created during a session are immediately available for graph traversal and time-based search. Semantic (TF-IDF) search results update after the next maintenance run rebuilds the index — typically on the next session start.

### The 7 MCP Tools

All tools are served by the `semantic_server` package:

**Read:**

| Tool | Purpose | Limits |
|------|---------|--------|
| `semantic_search_memory` | TF-IDF cosine similarity search, ranked results with observations | top_k max 100 |
| `traverse_relations` | BFS graph traversal from a start entity, returns connected subgraph | max_depth 1–5 |
| `search_memory_by_time` | Find entities by time range, sorted by most recent | limit max 100 |

**Write:**

| Tool | Purpose | Limits |
|------|---------|--------|
| `create_entities` | Add or merge entities with type + observations | 50/call |
| `create_relations` | Directed edges between entities (deduplicates) | 100/call |
| `add_observations` | Append facts to existing entities (append-only, O(1)) | 50 obs/call, 5000 chars/obs |
| `delete_entities` | Remove with cascading relation cleanup | — |

Write operations are protected by file locking (`flock`) and use atomic writes (`fsync` + `os.replace`). Graph size is capped at 50MB.

## Per-Project Configuration

After running `setup-project.sh`, edit `.memory/config.json` to override defaults:

```json
{
  "decay_threshold": 0.1,
  "max_age_days": 90,
  "throttle_hours": 24,
  "min_merge_name_len": 4
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `decay_threshold` | 0.1 | Entities scoring below this are pruned (0.0–10.0) |
| `max_age_days` | 90 | Entities older than this (with no updates) score poorly (1–3650) |
| `throttle_hours` | 24 | Minimum hours between maintenance runs (0.1–720) |
| `min_merge_name_len` | 4 | Names shorter than this only merge on exact match (1–100) |

Delete any key to use the default. Long-lived projects may want `max_age_days: 365`; fast-moving prototypes may prefer `throttle_hours: 12`.

## Cross-Machine Sync

Export and import memory graphs between machines:

```bash
# On source machine
~/projects/easy-memory-claude/export-memory.sh /path/to/project

# Transfer the .json bundle, then on target machine
~/projects/easy-memory-claude/import-memory.sh bundle.json /path/to/project
```

Import merges entities (deduplicating by name+type) and relations. A backup is created before merging. Both export and import validate structure and reject oversized graphs (>50MB).

## Performance

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| `semantic_search_memory` | O(k) via postings | Only scores candidates sharing query terms |
| `traverse_relations` | O(V+E) BFS | Cached adjacency lists, invalidated on graph change |
| `create_entities` (new) | O(1) append | No graph rewrite — appends JSONL lines |
| `create_entities` (merge) | O(1) append | Appends duplicate; maintenance consolidates later |
| `add_observations` | O(1) append | Append-only — maintenance merges duplicates later |
| `delete_entities` | O(n) rewrite | Must rewrite to remove entries |
| `maintenance` | O(n log n) | Token-indexed consolidation avoids O(n²) pairwise |

**Memory bounds:**

| Resource | Cap | Eviction |
|----------|-----|----------|
| Entities parsed from graph | 100,000 | Excess silently skipped |
| Recall count entries | 10,000 | Lowest-count evicted on overflow (LRU) |
| Combined cache size | 50 MB | Largest cache evicted first (index → adjacency → entity) |
| Graph file size | 50 MB | Write operations rejected above this |
| Observations per entity | 50 (activity-log) / 200 (other) | Oldest trimmed during maintenance |

## Resilience

| Scenario | Behavior |
|----------|----------|
| Project venv deleted | No effect — all tools use system `python3`, pure stdlib |
| Branch switch | Hooks live in `~/.claude/`, not project tree. Memory persists (gitignored). Entities stamped with `_branch` |
| graph.jsonl corrupt | Backup taken before every maintenance run. Atomic writes prevent partial corruption |
| graph.jsonl missing | Hooks create it on first write — no errors, no blocking |
| Project not initialized | Hooks skip silently. Nudge hook shows one-time notice per day |
| Concurrent writes | File locking (`flock`) prevents corruption from simultaneous MCP server + hook writes |
| Power loss during write | `fsync` + temp file + `os.replace` ensures either old or new data survives, never partial |
| Maintenance already running | Lock-based mutual exclusion — second instance skips gracefully |
| Write fails (disk full) | In-memory cache stays consistent — writes operate on copies, cache only cleared after success |
| Observation bloat | Activity-log entities capped at 50 observations, others at 200 — keeps newest, trims during maintenance |
| Recall count bloat | Stale recall entries pruned during maintenance. In-memory dict capped at 10,000 entries with LRU eviction |
| Duplicate entity names | Append-only writes create duplicate JSONL entries. Server merges on parse with cached dedup keys for O(1) amortized |
| SIGTERM / SIGINT | Server flushes recall counts and exits cleanly on both signals |
| Lock contention in hooks | `capture-tool-context` uses non-blocking `LOCK_NB` with retry — skips write on timeout to prevent corruption |
| Out of memory during maintenance | `MemoryError` caught during graph load — maintenance skips gracefully |

### Self-Guard Pattern

Every hook guards against missing project state:
```bash
[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] || exit 0   # project initialized?
[ -n "${CLAUDE_SESSION_ID:-}" ] || exit 0            # session context available?
```

Always `exit 0` — never blocks Claude.

## Cleanup

Remove all easy-memory-claude artifacts for a fresh start:

```bash
# Dry run — see what would be removed
~/projects/easy-memory-claude/cleanup.sh project /path/to/project --dry-run

# Remove per-project data (.memory/, MCP configs, .gitignore entry)
~/projects/easy-memory-claude/cleanup.sh project /path/to/project

# Remove global runtime + hooks + settings entries
~/projects/easy-memory-claude/cleanup.sh global

# Remove everything (global + project)
~/projects/easy-memory-claude/cleanup.sh all /path/to/project
```

All modes prompt before destructive steps. Use `--yes` to skip prompts.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| MCP server not starting | Check `.mcp.json` has correct `PYTHONPATH` pointing to `~/.claude/memory/`. Look at stderr for startup messages. |
| Semantic search returns no results | Index needs rebuilding. Run `python3 ~/.claude/memory/maintenance.py /path/to/project` manually. |
| "Graph too large" error | Run maintenance to prune: `python3 ~/.claude/memory/maintenance.py /path/to/project`. Or increase `decay_threshold` in `.memory/config.json`. |
| Hook not firing | Verify `~/.claude/settings.json` has the hook wiring. Re-run `install.sh` to repair. |
| Maintenance not running | Check `.memory/.last-maintenance` timestamp. Delete it to force a re-run. Or lower `throttle_hours` in config. |
| Import fails | Verify bundle is a valid `easy-memory-claude-export` format JSON. Must be <50MB. Target project must be initialized first. |
| Server crashes on large graph | Entity count is capped at 100,000. If your graph exceeds this, run maintenance to prune stale entities. |
| Slow searches on rapid queries | Recall file stat checks are throttled to 1x/60s. If still slow, check graph size and run maintenance. |
| Existing projects after upgrade | Re-run `install.sh` then `setup-project.sh` for each project. Old `.mcp.json` configs work via the backwards-compatible shim. |

## Known Limitations

- **TF-IDF vs neural embeddings**: Semantic search uses term-frequency similarity, not deep embeddings. It finds lexically related concepts but won't understand synonyms or paraphrases that share no words. Future: optional neural upgrade via `sentence-transformers` (see `requirements.txt`).
- **No read locking**: The `prime-memory.sh` hook reads `graph.jsonl` without a lock. During a concurrent full rewrite, it could read partial data. Impact: garbled session summary (self-corrects on next session). Append-only writes are safe to read concurrently.
- **camelCase splitting**: `normalize_name()` in maintenance handles ASCII + Latin-1 supplement only. CJK or other scripts won't be split on case boundaries (they still merge on exact match).

## Platform Support

| Platform | Status |
|----------|--------|
| macOS (ARM64/x86) | Full support |
| Linux (x86/ARM) | Full support |
| Windows (WSL) | Works under WSL. Native Windows: file locking disabled (no `fcntl`), all other features work. |

## License

MIT

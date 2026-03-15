# easy-memory-claude

**Give Claude a memory that learns, connects, and self-maintains.**

Every time you close a Claude Code session, everything Claude learned disappears. The architecture decisions, the bugs you debugged together, the patterns your codebase follows, the warnings about fragile code — all gone. Next session, you start from zero.

easy-memory-claude fixes this. It gives Claude a **knowledge graph** — a persistent, structured network of entities, relationships, and observations that survives across sessions, grows smarter over time, and cleans up after itself.

```
Session 1: "Let's use LWW for conflict resolution in SyncManager"
   └─ Claude records: decision entity + rationale + relations to SyncManager

Session 2: "How does our sync system work?"
   └─ Claude finds SyncManager (score 0.87), sees the LWW decision,
      traverses relations to ProviderRegistry and OfflineQueue
      — answers with full context it never had to be re-told
```

Pure Python. Zero external dependencies. Zero configuration. Works with every project.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Why This Exists](#why-this-exists)
- [How It Works](#how-it-works)
- [Installation](#installation)
- [Usage](#usage)
- [Decision Tracking](#decision-tracking)
- [Per-Project Configuration](#per-project-configuration)
- [Architecture](#architecture)
- [Performance](#performance)
- [Resilience](#resilience)
- [Cross-Machine Sync](#cross-machine-sync)
- [Cleanup](#cleanup)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [Platform Support](#platform-support)

---

## Prerequisites

> **Both must be available in your shell's `$PATH` before running `install.sh`.**

| Requirement | Minimum | Check |
|-------------|---------|-------|
| **Python** | 3.10+ | `python3 --version` |
| **Git** | any | `git --version` |

- **macOS**: `brew install python git`
- **Ubuntu/Debian**: `sudo apt install python3 git`
- **Arch**: `sudo pacman -S python git`

No Node.js, no pip packages, no virtual environment needed. Everything runs on the standard library. The installer optionally offers `orjson` for 3-10x faster graph I/O — not required.

## Why This Exists

Claude Code starts every conversation with a blank slate. The moment a session ends, everything Claude learned evaporates — your architecture decisions, the bugs already fixed, the patterns your team prefers, the fragile files that need special handling. You end up repeating context in every single conversation.

Flat files like `CLAUDE.md` help with static rules, but they can't capture knowledge that evolves:

| | Flat files (`CLAUDE.md`) | easy-memory-claude |
|---|---|---|
| **Structure** | Linear text, no relationships | Graph of entities, relations, observations — Claude traverses connections |
| **Search** | Keyword grep only | TF-IDF semantic similarity — finds "sync conflict resolution" even when those exact words aren't stored |
| **Maintenance** | Manual — you prune and organize | Automatic — daily decay scoring, pruning, deduplication, index rebuild |
| **Growth** | Unbounded, degrades context quality | Self-regulating — stale entities pruned, duplicates merged |
| **Cross-session** | Requires manual "remember X" | Automatic — hooks inject graph summary at session start, capture activity, persist at end |
| **Decisions** | Lost in chat history | Structured journal — rationale, alternatives, outcomes tracked and linked to code |
| **Discovery** | Must know what to search for | Semantic search surfaces related knowledge you didn't think to look for |
| **Resilience** | File gets too large, context window wasted | Atomic writes, auto-backup, file locking, graceful degradation |

**Use both together:** `CLAUDE.md` for static rules and conventions, the knowledge graph for everything that evolves.

> **When flat files are still better:** Static rules that never change (code style, project constraints), short-lived projects with fewer than a few sessions, or projects with fewer than 10 facts worth remembering.

## How It Works

Four systems work together in a continuous loop:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SESSION LIFECYCLE                            │
│                                                                     │
│  ┌──────────────┐    ┌────────────────┐    ┌─────────────────────┐  │
│  │ Session Start│    │ During Session │    │ Session End         │  │
│  │              │    │                │    │                     │  │
│  │ prime-       │    │  MCP Server    │    │ capture-decisions   │  │
│  │ memory.sh    │──▶ │  (10 tools)    │──▶ │ reminds Claude to   │  │
│  │              │    │                │    │ persist decisions   │  │
│  └──────┬───────┘    └───────┬────────┘    └─────────────────────┘  │
│         │                    │                                      │
│         ▼                    ▼                                      │
│  ┌──────────────┐    ┌────────────────┐                             │
│  │ maintenance  │    │ capture-tool   │                             │
│  │ .py (1x/day) │    │ -context       │                             │
│  └──────┬───────┘    └───────┬────────┘                             │
│         │                    │                                      │
│         ▼                    ▼                                      │
│  ┌──────────────────────────────────────────────┐                   │
│  │           .memory/graph.jsonl                │                   │
│  │    entities + relations + observations       │                   │
│  │           (append-only writes)               │                   │
│  └──────────────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
```

### 1. The Knowledge Graph

All memory lives in a single `graph.jsonl` file inside each project's `.memory/` directory. Two kinds of objects:

**Entities** — things Claude knows about, with a name, type, and observations (facts):

```json
{"type":"entity","name":"SyncManager","entityType":"component","observations":["Uses LWW resolution","Custom HTTP sync","Handles offline queue"]}
```

**Relations** — directed connections between entities:

```json
{"type":"relation","from":"SyncManager","to":"ProviderRegistry","relationType":"uses"}
```

Together they form a traversable graph:

```
                    ┌─────────────────┐
         uses       │ ProviderRegistry│
    ┌──────────────▶│   (component)   │
    │               └─────────────────┘
┌───┴───────────┐
│  SyncManager  │         depends-on
│  (component)  │   ┌─────────────────┐
│               │──▶│  OfflineQueue   │
│ "Uses LWW     │   │  (component)    │
│  resolution"  │   └─────────────────┘
└───────────────┘
```

Instead of searching keywords, Claude follows relationships to discover connected knowledge it didn't know to look for.

### 2. The MCP Server

The MCP server (`semantic_server/` package) runs as a long-lived process speaking JSON-RPC 2.0 over stdio. Claude handles communication automatically.

**10 tools** in three categories:

**Read tools** — query the graph:

| Tool | What it does |
|------|-------------|
| `semantic_search_memory` | TF-IDF cosine similarity search — ranked results with observations |
| `traverse_relations` | BFS graph traversal — returns connected subgraph |
| `search_memory_by_time` | Find entities by time range, sorted by recency |
| `graph_stats` | Counts, type breakdown, pending decisions, recall rankings |

**Write tools** — mutate the graph (all locked + atomic):

| Tool | What it does |
|------|-------------|
| `create_entities` | Add entities with type + observations (merges on same name) |
| `create_relations` | Directed edges between entities (deduplicates) |
| `add_observations` | Append facts to an existing entity |
| `delete_entities` | Remove entities + cascade-remove their relations |

**Decision tools** — structured decision tracking:

| Tool | What it does |
|------|-------------|
| `create_decision` | Record decision with rationale, alternatives, outcome; auto-links related entities |
| `update_decision_outcome` | Record outcome and lesson learned to close the feedback loop |

All writes use **file locking** (`flock`) and **atomic writes** (`fsync` + `os.replace`). If a write fails, the graph stays intact. Graph size is capped at 50 MB.

#### How Semantic Search Works

Traditional search requires exact word matches. TF-IDF goes further:

1. **Term Frequency (TF)** — how often a word appears in an entity's name + observations
2. **Inverse Document Frequency (IDF)** — words appearing in fewer entities get higher weight
3. **Cosine Similarity** — the query and each entity are compared as vectors in word-weight space

Searching for "sync conflict resolution" can find an entity about "LWW merge strategy" if they share enough weighted terms — even without an exact phrase match.

A **postings index** maps each term to the entities containing it, so searches examine only candidates sharing query terms (not the full graph).

### 3. Automatic Maintenance

The maintenance script runs automatically once per day at session start. It keeps the graph healthy:

```
  graph.jsonl ──▶ backup ──▶ stamp ──▶ score ──▶ prune ──▶ consolidate
                                                                │
                  graph.jsonl ◀── index ◀── prune recall ◀── cap obs
                  (rewritten)
```

| Step | What it does |
|------|-------------|
| **Backup** | Hard-links `graph.jsonl` (O(1)) before mutation |
| **Stamp** | Tags new entities with `_branch` and `_created` metadata |
| **Score** | Calculates relevance: `obs_count * recency * recall_boost` |
| **Prune** | Removes low-score entities with zero inbound relations |
| **Consolidate** | Merges entities with overlapping names + same type (sorted-merge, O(n log n)) |
| **Cap observations** | activity-log: keep 50; others: keep 200 |
| **Prune recall** | Removes recall tracking for deleted entities |
| **Build TF-IDF** | Rebuilds search vectors, postings, magnitudes |

#### Scoring Formula

```
score = obs_count × (1 / (1 + days_stale)) × (1 + log(recall_count))
```

**Examples:**
- 5 observations, updated today, searched 10x: `5 × 1.0 × 3.3 = 16.5` — kept
- 2 observations, 60 days stale, never searched: `2 × 0.016 × 1.0 = 0.03` — pruned
- 1 observation, 30 days old, searched 3x: `1 × 0.032 × 2.1 = 0.067` — kept if it has inbound relations

Entities with inbound relations are **never pruned** regardless of score.

### 4. Lifecycle Hooks

Four global hooks fire automatically. Zero manual intervention:

| Hook | Event | What It Does |
|------|-------|-------------|
| `prime-memory.sh` | SessionStart | Runs maintenance (if due), injects scored top-N summary with 1-hop relations + pending decisions |
| `capture-tool-context.sh` | PostToolUse | Records file edits, writes, commands as graph observations |
| `capture-decisions.sh` | Stop | Reminds Claude to persist important decisions |
| `nudge-setup.sh` | SessionStart | Shows setup notice if project has no `.memory/` (1x/day) |

Hooks skip projects without `.memory/` — no noise, no errors.

**Crash-safe observation capture:** The hook writes to a sidecar buffer (`graph.jsonl.pending`). The MCP server merges pending entries every 5 seconds using rename-before-read:

```
pending ──rename──▶ .processing ──read──▶ append to graph ──▶ delete .processing
```

If the server crashes mid-merge, `.processing` survives and gets reprocessed on next startup.

## Installation

### Step 1: Get the Project

```bash
git clone https://github.com/locx/easy-memory-claude.git
cd easy-memory-claude
```

### Step 2: Install the Runtime

```bash
chmod +x install.sh
./install.sh
```

The installer will:
1. Verify prerequisites (Python 3.10+, git)
2. Deploy `maintenance.py` and the `semantic_server/` package to `~/.claude/memory/`
3. Deploy hook scripts to `~/.claude/hooks/`
4. Wire hooks into `~/.claude/settings.json` (safe JSON merge — preserves existing config)

### Step 3: Set Up a Project

```bash
./setup-project.sh /path/to/your/project
```

This creates:
- `.memory/` directory with empty `graph.jsonl` (gitignored)
- `.mcp.json` for Claude Code CLI + `.vscode/mcp.json` for VS Code
- Bootstraps graph by scanning project structure (up to 200 files)
- Builds TF-IDF index immediately
- Adds `.memory/` to `.gitignore`

Then **restart Claude Code** to activate the MCP server.

### Verify Installation

```bash
# Check runtime
ls ~/.claude/memory/maintenance.py ~/.claude/memory/semantic_server/__init__.py

# Check project
ls /path/to/project/.memory/ /path/to/project/.mcp.json

# Test maintenance
python3 ~/.claude/memory/maintenance.py /path/to/project
```

### Upgrading

Re-run `./install.sh` to deploy updated server and hooks. Then `./setup-project.sh` for each project to refresh MCP configs.

## Usage

Once installed, memory works transparently.

### What Happens Automatically

**Session start** — Claude sees a scored graph summary:
```
=== Top Memory (5 most relevant) ===
  SyncManager (component): Uses LWW resolution | uses->ProviderRegistry
  AuthService (service): JWT with 24h expiry | authenticates->UserModel

  Pending decisions (1):
    - Migration strategy for v2 schema

Memory: 42 entities | 28 relations | 3 decisions | maintained 4h ago
Tools: semantic_search_memory | traverse_relations | create_decision | graph_stats
```

**During conversation** — Claude uses MCP tools to read and write the graph. You can also ask explicitly ("remember that we decided to use LWW for conflict resolution").

**After tool calls** — Hooks automatically record what files Claude edited, created, or what commands it ran.

**Session end** — Claude gets a reminder to persist important decisions:
```
If this session involved trade-off evaluations or architectural decisions,
persist them with create_decision:

  create_decision({
    title: "what was decided",
    rationale: "why this approach",
    alternatives: ["rejected option -- reason"],
    scope: "affected code area",
    related_entities: ["ComponentName"]
  })
```

**Daily maintenance** — stale entities pruned, duplicates merged, search index rebuilt. All automatic.

> **Note:** New entities are immediately available for traversal and time-based search. Semantic (TF-IDF) search results update after the next maintenance run rebuilds the index.

### Example Session Flow

```
1. SESSION START
   prime-memory.sh → maintenance runs (if >24h) → smart_recall.py
   injects top-5 entities + pending decisions into context

2. USER: "How does our sync system handle conflicts?"
   Claude calls: semantic_search_memory(query="sync conflict handling")
   → Returns: SyncManager (score 0.87), ConflictResolver (score 0.72)
   Claude calls: traverse_relations(entity="SyncManager", max_depth=2)
   → Returns: SyncManager --uses--> ProviderRegistry --imports--> ConfigStore

3. USER: "Let's switch from LWW to CRDT for merges"
   Claude makes the code changes...
   capture-tool-context fires → records file edits as observations

4. USER: "Remember this decision"
   Claude calls: create_decision({
     title: "Switch from LWW to CRDT merge strategy",
     rationale: "CRDTs preserve concurrent edits without data loss",
     alternatives: ["LWW -- simpler but loses concurrent writes"],
     related_entities: ["SyncManager", "ConflictResolver"]
   })

5. SESSION END
   capture-decisions fires → reminder to persist any remaining decisions
   Recall counts flushed to disk
```

## Decision Tracking

Decisions are first-class citizens — not just text, but structured entities with rationale, alternatives, outcome tracking, and automatic links to related code.

### Creating a Decision

```
create_decision({
  title: "Use PostgreSQL over MongoDB for user data",
  rationale: "Need ACID transactions for billing. Relational model fits user/org hierarchy.",
  alternatives: [
    "MongoDB -- flexible schema but no multi-doc transactions",
    "CockroachDB -- distributed but operational overhead too high"
  ],
  scope: "UserService, BillingModule, data layer",
  outcome: "adopted",
  related_entities: ["UserService", "BillingModule"]
})
```

This creates:
- A `decision` entity with rationale, rejected alternatives, scope, outcome as observations
- `decided-for` relation edges to `UserService` and `BillingModule`

### Closing the Loop

When you return to code affected by a prior decision:

```
update_decision_outcome({
  title: "Use PostgreSQL over MongoDB for user data",
  outcome: "successful",
  lesson: "JSONB columns handled the few schema-flexible cases without needing MongoDB"
})
```

Valid outcomes: `pending`, `successful`, `failed`, `revised`, `adopted`, `rejected`, `deferred`.

### Why This Matters

- **`graph_stats`** surfaces pending decisions count — Claude proactively asks about unresolved decisions
- **`smart_recall.py`** shows pending decisions at session start — they stay visible until resolved
- **`traverse_relations`** connects decisions to the code they affect — full context on demand
- **Decay scoring** treats decisions like any entity — stale unresolved decisions surface as worth revisiting

## Per-Project Configuration

Edit `.memory/config.json` to override defaults:

```json
{
  "decay_threshold": 0.1,
  "max_age_days": 90,
  "throttle_hours": 24,
  "min_merge_name_len": 4
}
```

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `decay_threshold` | 0.1 | 0.0-10.0 | Entities scoring below this are pruned |
| `max_age_days` | 90 | 1-3650 | Entities older than this score poorly |
| `throttle_hours` | 24 | 0.1-720 | Minimum hours between maintenance runs |
| `min_merge_name_len` | 4 | 1-100 | Names shorter than this only merge on exact match |

Delete any key to use the default. Long-lived projects: `max_age_days: 365`. Fast prototypes: `throttle_hours: 12`.

**Tuning:**
- Too many stale entities? Raise `decay_threshold` to 0.2 or reduce `max_age_days`
- Important entities getting pruned? Lower `decay_threshold` to 0.05, or search for them more often (recall boost protects frequently-accessed entities)
- Want more frequent index rebuilds? Lower `throttle_hours`

## Architecture

### Directory Layout

```
~/.claude/                          GLOBAL (runtime only)
  settings.json                     Hook wiring
  hooks/
    prime-memory.sh                 SessionStart: maintenance + smart recall
    smart_recall.py                 SessionStart: scored top-N summary
    capture-tool-context.sh         PostToolUse: observation capture (shell)
    capture_tool_context.py         PostToolUse: observation capture (Python)
    capture-decisions.sh            Stop: decision persistence reminder
    nudge-setup.sh                  SessionStart: setup nudge
  memory/
    maintenance.py                  Decay / prune / consolidate / TF-IDF
    semantic_server/                MCP server package (12 modules)
    semantic_server.py              Backwards-compatible entry shim

easy-memory-claude/                 SOURCE (install + dev)
  install.sh                        Deploys runtime to ~/.claude/
  setup-project.sh                  Initializes any project
  cleanup.sh                        Remove artifacts
  export-memory.sh                  Export graph to portable JSON bundle
  import-memory.sh                  Import/merge bundle into project

<any-project>/                      PER-PROJECT (gitignored)
  .mcp.json                         MCP server config (Claude Code CLI)
  .memory/
    graph.jsonl                     Knowledge graph (JSONL, append-only)
    tfidf_index.json                Search index (rebuilt by maintenance)
    config.json                     Per-project overrides (optional)
    graph.jsonl.bak                 Auto-backup before maintenance
    graph.jsonl.pending             Hook sidecar buffer (transient)
    .graph.lock                     Write lock (flock)
    recall_counts.json              Hebbian recall frequency data
    pruned.log                      Maintenance event log
    .last-maintenance               Throttle marker (mtime-based)
  .vscode/mcp.json                  MCP server config (VS Code)
```

### MCP Server Package

```
semantic_server/
  __init__.py       Package entry — exposes main()
  __main__.py       python3 -m semantic_server
  _json.py          Fast JSON backend (orjson -> stdlib fallback)
  config.py         Constants, limits, regex, timestamps, event logging
  cache.py          Mtime-based caches with size-aware eviction (50 MB cap)
  recall.py         Hebbian recall tracking (OrderedDict LRU, 10K cap)
  graph.py          JSONL parsing, incremental reads, locking, atomic writes
  search.py         TF-IDF cosine similarity + time-based search
  traverse.py       BFS relation traversal with cached adjacency lists
  tools.py          Write operations + decision tracking
  protocol.py       JSON-RPC 2.0 dispatch + tool schemas (from tools_schema.json)
  server.py         Stdio event loop, signal handling, sidecar merge
  tools_schema.json Tool input schemas (data, loaded at import time)
```

**Dependency flow** (no circular imports):
```
  _json   ◀── graph, protocol, server
  config  ◀── cache, recall, graph, search, tools, protocol, server
  cache   ◀── graph, traverse, search, tools, server
  recall  ◀── search, protocol, server

  graph   ◀── search   ◀── protocol ◀── server
  graph   ◀── traverse ◀── protocol
  graph   ◀── tools    ◀── protocol
```

## Performance

Sub-second on graphs up to 100K entities. Writes are O(1) appends — no rewrite until maintenance or delete.

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| `semantic_search_memory` | O(k) | Postings index; heap-based top-k |
| `traverse_relations` | O(V+E) | Cached adjacency lists |
| `create_entities` | O(1) | Append-only JSONL with flock |
| `add_observations` | O(1) | Append-only; dedup against cache |
| `delete_entities` | O(n) | Must rewrite graph; locked + atomic |
| `maintenance` | O(n log n) | Sorted-merge consolidation |
| `load_graph_entities` (incremental) | O(delta) | Byte-offset tracking reads only new appends |

### Resource Caps

| Resource | Cap | Strategy |
|----------|-----|----------|
| Graph file | 50 MB | Write operations rejected above this |
| Parsed entities | 100,000 | Excess skipped during parse |
| Combined cache | 50 MB | Evicts: index > adjacency > entity > relation |
| Recall entries | 10,000 | LRU eviction (OrderedDict) |
| Observations/entity (cached) | 3 | Full set on disk; cache keeps newest 3 |
| Observations/entity (on disk) | 50 / 200 | activity-log: 50; others: 200 |
| BFS traversal | 10,000 nodes | Capped to prevent OOM |
| Search candidates | 1,000 | Postings intersection capped |
| Query length | 10,000 chars | Truncated silently |
| Parse time budget | 10 seconds | Aborted to prevent blocking |

## Resilience

| Scenario | Behavior |
|----------|----------|
| Branch switch | Entities stamped with `_branch`; search boosts same-branch results |
| graph.jsonl corrupt | Backup taken before every maintenance run; atomic writes prevent partial corruption |
| graph.jsonl missing | Server auto-creates on first write |
| Project not initialized | Hooks skip silently; nudge hook shows one-time notice |
| Concurrent writes | File locking (`flock`) prevents corruption |
| Power loss during write | `fsync` + temp file + `os.replace` — either old or new data survives |
| Maintenance already running | Lock-based mutual exclusion — second instance skips |
| Write fails (disk full) | Cache stays consistent — only cleared after successful write |
| Duplicate entity names | Append-only creates duplicates; server deduplicates on parse |
| SIGTERM / SIGINT | Server flushes recall counts and exits cleanly |
| Graph modified during maintenance | Detected via mtime guard — skips write, retries next run |
| Hook sidecar crash | `.processing` file survives and gets reprocessed |
| Tool errors | MCP `isError` flag set on failures — Claude can distinguish errors from results |

## Cross-Machine Sync

Export and import memory graphs between machines:

```bash
# On source machine
./export-memory.sh /path/to/project

# Transfer the .json bundle, then on target machine
./import-memory.sh bundle.json /path/to/project
```

Import merges entities (deduplicating by name+type) and relations. A backup is created before merging. Both validate structure and reject oversized graphs (>50 MB).

## Cleanup

```bash
# Dry run
./cleanup.sh project /path/to/project --dry-run

# Remove per-project data
./cleanup.sh project /path/to/project

# Remove global runtime + hooks
./cleanup.sh global

# Remove everything
./cleanup.sh all /path/to/project
```

All modes prompt before destructive steps. Use `--yes` to skip prompts.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| MCP server not starting | Check `.mcp.json` has correct `PYTHONPATH` pointing to `~/.claude/memory/`. Check stderr for startup messages. |
| Search returns no results | Index needs rebuild: `python3 ~/.claude/memory/maintenance.py /path/to/project` |
| "Graph too large" error | Run maintenance to prune. Or increase `decay_threshold` in `.memory/config.json`. |
| Hook not firing | Verify `~/.claude/settings.json` has hook wiring. Re-run `install.sh`. |
| Maintenance not running | Delete `.memory/.last-maintenance` to force re-run. Or lower `throttle_hours`. |
| "Write failed (lock timeout)" | Another process holds the lock. Check for stuck maintenance or concurrent MCP instances. |
| New entities not in search | TF-IDF search requires index rebuild (next maintenance). Use `traverse_relations` or `search_memory_by_time` for immediate access. |
| Existing projects after upgrade | Re-run `install.sh` then `setup-project.sh`. Old `.mcp.json` configs work via the backwards-compatible shim. |

## Known Limitations

- **TF-IDF, not embeddings** — finds lexically related concepts but won't understand synonyms sharing no words. Future: optional neural upgrade via `sentence-transformers`.
- **No read locking** — hooks read `graph.jsonl` without a lock. During a concurrent full rewrite, reads could see partial data. Impact: garbled session summary (self-corrects next session). Append-only writes are safe to read concurrently.
- **ASCII camelCase splitting** — `normalize_name()` handles ASCII + Latin-1 supplement. CJK and other scripts merge on exact match only.
- **Single-machine scope** — graphs are local. Cross-machine sync requires manual export/import.

## Platform Support

| Platform | Status |
|----------|--------|
| macOS (ARM64/x86) | Full support |
| Linux (x86/ARM) | Full support |
| Windows (WSL) | Works under WSL. Native Windows: file locking disabled (no `fcntl`), all other features work. |

## License

MIT

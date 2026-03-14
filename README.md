# easy-memory-claude

Persistent knowledge graph memory for Claude Code. Works across all projects.

Pure Python, zero external dependencies, zero configuration required.

**What you get:**
- Knowledge graph with entities, relations, and observations — not flat text
- Semantic search via TF-IDF cosine similarity — finds concepts by meaning
- Automatic lifecycle hooks — capture context, inject summaries, persist decisions
- Self-maintaining — daily decay scoring, pruning, deduplication, index rebuild
- Crash-safe — atomic writes, file locking, auto-backup, graceful degradation

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Why Does This Exist?](#why-does-this-exist)
- [How It Works — The Big Picture](#how-it-works--the-big-picture)
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

No Node.js, no pip packages, no virtual environment needed. Everything runs on the standard library. Installer optionally offers `orjson` for 3-10x faster graph I/O — not required.

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
| **Cross-session** | Requires manual "remember X" instructions | Automatic — hooks inject graph summary at session start, capture activity, persist at end |
| **Multi-project** | Separate files per project, no shared infra | Global infrastructure, per-project graphs, one-command setup |
| **Resilience** | File gets too large, context window wasted | Atomic writes with fsync, auto-backup, file locking, graceful degradation |
| **Discovery** | Must know what to search for | Semantic search surfaces related knowledge you didn't think to look for |
| **Decisions** | Lost in chat history | Structured journal — rationale, alternatives, outcomes tracked |

### When Flat Files Are Still Better

- **Static rules** that never change (code style, project constraints) — keep those in `CLAUDE.md`
- **Short-lived projects** where you won't have more than a few sessions
- **Projects with <10 facts** to remember — the overhead isn't worth it

easy-memory-claude **complements** `CLAUDE.md` — it doesn't replace it. Use `CLAUDE.md` for rules and conventions, use the knowledge graph for everything that evolves.

## How It Works — The Big Picture

Four systems work together in a continuous loop:

```
┌─────────────────────────────────────────────────────────────────┐
│                     SESSION LIFECYCLE                            │
│                                                                 │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────────────┐  │
│  │ Session  │    │   During     │    │    Session End         │  │
│  │  Start   │    │  Session     │    │                       │  │
│  │          │    │              │    │  capture-decisions.sh  │  │
│  │ prime-   │    │ MCP Server   │    │  reminds Claude to    │  │
│  │ memory   │──▶ │ (10 tools)  │──▶ │  persist decisions    │  │
│  │ .sh      │    │              │    │                       │  │
│  └──┬───────┘    └──────┬───────┘    └───────────────────────┘  │
│     │                   │                                       │
│     ▼                   ▼                                       │
│  ┌──────────┐    ┌──────────────┐                               │
│  │ mainte-  │    │ capture-tool │                               │
│  │ nance.py │    │ -context.sh  │                               │
│  │ (1x/day) │    │ (1x/30s)     │                               │
│  └──┬───────┘    └──────┬───────┘                               │
│     │                   │                                       │
│     ▼                   ▼                                       │
│  ┌──────────────────────────────────────────┐                   │
│  │         .memory/graph.jsonl              │                   │
│  │  entities + relations + observations     │                   │
│  │         (append-only writes)             │                   │
│  └──────────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────┘
```

### 1. The Knowledge Graph (Your Data)

All memory is stored in a single file called `graph.jsonl` inside each project's `.memory/` directory. It uses JSONL (JSON Lines) — each line is one JSON object.

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
                    ┌─────────────────┐
         uses       │ ProviderRegistry│
    ┌──────────────▶│   (component)   │
    │               ├─────────────────┤
┌───┴───────────┐   │ "Singleton      │
│  SyncManager  │   │  pattern"       │
│  (component)  │   │ "resolve() is   │
├───────────────┤   │  async"         │
│ "Uses LWW     │   └─────────────────┘
│  resolution"  │
│ "Custom HTTP  │         depends-on
│  sync"        │   ┌─────────────────┐
│ "Handles      │──▶│  OfflineQueue   │
│  offline      │   │  (component)    │
│  queue"       │   ├─────────────────┤
└───────────────┘   │ "IndexedDB      │
                    │  backed"        │
                    └─────────────────┘
```

This is much richer than flat text. Instead of searching for keywords, Claude can follow relationships to discover connected knowledge it didn't know to look for.

### 2. The MCP Server (How Claude Talks to the Graph)

The MCP server (`semantic_server/` package) is a long-running process that Claude Code communicates with. It speaks JSON-RPC 2.0 over stdio — Claude handles all of this automatically.

The server exposes **10 tools** organized in three categories:

**Read tools** — query the graph without modifying it:

| Tool | What it does | Key params |
|------|-------------|------------|
| `semantic_search_memory` | TF-IDF cosine similarity search, ranked results with observations | `query`, `top_k` (max 100) |
| `traverse_relations` | BFS graph traversal from a start entity, returns connected subgraph | `entity`, `direction`, `max_depth` (1-5) |
| `search_memory_by_time` | Find entities by time range, sorted by most recent | `since`, `until`, `limit` (max 100) |
| `graph_stats` | Counts, type breakdown, pending decisions, recall rankings, session stats | -- |

**Write tools** — mutate the graph (all locked + atomic):

| Tool | What it does | Key params |
|------|-------------|------------|
| `create_entities` | Add entities with type + observations (merges on name) | `entities` (max 50) |
| `create_relations` | Directed edges between entities (deduplicates) | `relations` (max 100) |
| `add_observations` | Append facts to an existing entity (O(1) append) | `entity`, `observations` (max 50, 5000 chars) |
| `delete_entities` | Remove entities + cascade-remove relations | `entity_names` |

**Decision tools** — structured decision tracking (see [Decision Tracking](#decision-tracking)):

| Tool | What it does | Key params |
|------|-------------|------------|
| `create_decision` | Record decision with rationale, alternatives, outcome; auto-links related entities | `title`, `rationale`, `outcome`, `related_entities` |
| `update_decision_outcome` | Record outcome and lesson learned to close the feedback loop | `title`, `outcome`, `lesson` |

All write operations use **file locking** (`flock`) and **atomic writes** (`fsync` + `os.replace`). If a write fails, the graph stays intact. Graph size is capped at 50MB.

#### How Semantic Search Works

Traditional keyword search requires exact word matches. TF-IDF (Term Frequency-Inverse Document Frequency) goes further:

1. **Term Frequency (TF)** — how often a word appears in an entity's name + observations
2. **Inverse Document Frequency (IDF)** — words that appear in fewer entities are weighted higher (rare words are more discriminating)
3. **Cosine Similarity** — the query and each entity are compared as vectors in word-weight space

The result: searching for "sync conflict resolution" can find an entity about "LWW merge strategy" if they share enough weighted terms — even without an exact phrase match.

A **postings index** maps each term to the entities containing it, so searches examine only candidates sharing query terms (not the full graph).

### 3. Automatic Maintenance (Self-Cleaning)

The maintenance script (`maintenance.py`) runs automatically once per day when you start a new Claude session. It keeps the graph healthy through an 8-step pipeline:

```
  graph.jsonl ──▶ backup ──▶ stamp ──▶ score ──▶ prune ──▶ consolidate
                                                                │
                  graph.jsonl ◀── index ◀── prune recall ◀── cap obs
                  (rewritten)
```

| Step | What it does |
|------|-------------|
| **1. Backup** | Hard-links `graph.jsonl` (O(1)) before mutation; falls back to copy |
| **2. Stamp** | Tags new entities with `_branch` and `_created` metadata |
| **3. Score** | Calculates relevance per entity (see formula below) |
| **4. Prune** | Removes low-score entities with zero inbound relations |
| **5. Consolidate** | Merges entities with overlapping names + same type (sorted-merge, avoids O(n^2)) |
| **6. Cap observations** | activity-log: keep newest 50; others: keep newest 200 |
| **7. Prune recall counts** | Removes recall tracking for deleted entities |
| **8. Build TF-IDF index** | Rebuilds search vectors, postings, and magnitudes |

#### How Scoring Works

Each entity is scored to decide whether it survives pruning:

```
score = obs_count × recency × recall_boost

where:
  obs_count    = number of observations (more facts = more valuable)
  recency      = 1 / (1 + days_since_last_update)
  recall_boost = 1 + log(recall_count)   if entity has been searched for
                 1.0                      otherwise (no boost)
```

**Examples:**
- Entity with 5 observations, updated today, searched 10x: `5 × 1.0 × 3.3 = 16.5` (kept)
- Entity with 2 observations, 60 days stale, never searched: `2 × 0.016 × 1.0 = 0.03` (pruned if score < 0.1 threshold)
- Entity with 1 observation, 30 days old, searched 3x: `1 × 0.032 × 2.1 = 0.067` (borderline — kept if it has inbound relations)

Entities with inbound relations are **never pruned** regardless of score — they're connected to something that still matters.

### 4. Lifecycle Hooks (Automatic Triggers)

Four global hooks fire automatically for every project with a `.memory/` directory. They require zero manual intervention:

| Hook | Event | When It Fires | What It Does |
|------|-------|---------------|-------------|
| `prime-memory.sh` | `SessionStart` | New session begins | Runs maintenance (if due), then calls `smart_recall.py` to inject scored top-N summary with 1-hop relations + pending decisions |
| `capture-tool-context.sh` | `PostToolUse` | After each tool use | Captures observations from file edits/writes/commands via `capture_tool_context.py` (throttled 1x/30s) |
| `capture-decisions.sh` | `Stop` | Session ends | One-time reminder to persist important decisions via `create_decision` |
| `nudge-setup.sh` | `SessionStart` | New session begins | Shows setup notice if project has no `.memory/` (1x/day per project) |

Memory hooks skip projects without `.memory/` — no noise, no errors:
```bash
[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] || exit 0   # prime, capture, decisions
[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] && exit 0   # nudge (inverse — only fires when NOT initialized)
```

**Crash safety for observation capture:** The `capture-tool-context` hook writes to a sidecar buffer (`graph.jsonl.pending`) instead of the main graph. The MCP server merges pending entries every 5 seconds using a rename-before-read pattern:

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
1. Verify prerequisites (python3 3.10+, git)
2. Deploy `maintenance.py` and the `semantic_server/` package to `~/.claude/memory/`
3. Deploy hook files to `~/.claude/hooks/`
4. Wire hooks into `~/.claude/settings.json` (safe JSON merge — preserves existing config)

### Step 3: Set Up a Project

```bash
./setup-project.sh /path/to/your/project
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

### Upgrading

Re-run `./install.sh` to deploy updated server and hooks. Then re-run `./setup-project.sh` for each project to refresh MCP configs.

## Usage

Once installed, memory works transparently — you don't need to do anything special.

### What Happens Automatically

**Session start** — Claude sees a scored graph summary injected by the `prime-memory.sh` hook:
```
=== Top Memory (5 most relevant) ===
  SyncManager (component): Uses LWW resolution | uses->ProviderRegistry
  AuthService (service): JWT with 24h expiry | authenticates->UserModel

  Pending decisions (1):
    - Migration strategy for v2 schema

Memory: 42 entities | 28 relations | 3 decisions | maintained 4h ago
Tools: semantic_search_memory | traverse_relations | create_decision | graph_stats
```

**During conversation** — Claude uses MCP tools to read and write the graph. You can also ask Claude to remember things explicitly ("remember that we decided to use LWW for conflict resolution").

**After tool calls** — The `capture-tool-context.sh` hook automatically records what files Claude edited, created, or what commands it ran. This builds a session activity log in the graph.

**Session end** — Claude gets a structured reminder to persist important decisions:
```
If this session involved trade-off evaluations, approach selections,
or architectural decisions, persist them with create_decision:

  create_decision({
    title: "what was decided",
    rationale: "why this approach",
    alternatives: ["rejected option -- reason"],
    scope: "affected code area",
    related_entities: ["ComponentName"]
  })

For file-specific warnings (gotchas, fragile areas, known issues):

  create_entities([{
    name: "filename.py",
    entityType: "file-warning",
    observations: ["[WARNING] description of the gotcha"]
  }])
```

**Daily maintenance** — stale entities are pruned, duplicates merged, search index rebuilt. All automatic.

> **Note:** Entities created during a session are immediately available for graph traversal and time-based search. Semantic (TF-IDF) search results update after the next maintenance run rebuilds the index — typically on the next session start.

### Example Session Flow

Here's what a typical session looks like from Claude's perspective:

```
1. SESSION START
   prime-memory.sh fires → maintenance runs (if >24h) → smart_recall.py
   injects top-5 entities + pending decisions into context

2. USER: "How does our sync system handle conflicts?"
   Claude calls: semantic_search_memory(query="sync conflict handling")
   → Returns: SyncManager (score 0.87), ConflictResolver (score 0.72)
   Claude calls: traverse_relations(entity="SyncManager", max_depth=2)
   → Returns: SyncManager --uses--> ProviderRegistry --imports--> ConfigStore

3. USER: "Let's switch from LWW to CRDT for merges"
   Claude makes the code changes...
   capture-tool-context.sh fires → records file edits as observations

4. USER: "Remember this decision"
   Claude calls: create_decision({
     title: "Switch from LWW to CRDT merge strategy",
     rationale: "CRDTs preserve concurrent edits without data loss",
     alternatives: ["LWW -- simpler but loses concurrent writes"],
     scope: "SyncManager, ConflictResolver",
     related_entities: ["SyncManager", "ConflictResolver"]
   })

5. SESSION END
   capture-decisions.sh fires → reminder to persist any remaining decisions
   Recall counts flushed to disk
```

## Decision Tracking

Decisions are first-class citizens in the knowledge graph — not just text comments, but structured entities with rationale, alternatives, outcome tracking, and automatic links to related code.

### Creating a Decision

When Claude makes or helps evaluate a technical decision, it can record it:

```
create_decision({
  title: "Use PostgreSQL over MongoDB for user data",
  rationale: "Need ACID transactions for billing. Relational model fits user/org hierarchy.",
  alternatives: [
    "MongoDB -- flexible schema but no multi-doc transactions",
    "CockroachDB -- distributed but operational overhead too high for team size"
  ],
  scope: "UserService, BillingModule, data layer",
  outcome: "adopted",
  related_entities: ["UserService", "BillingModule"]
})
```

This creates:
- A `decision` entity named `"decision: Use PostgreSQL over MongoDB for user data"`
- Observations: rationale, each rejected alternative with reason, scope, outcome
- Relations: `decided-for` edges to `UserService` and `BillingModule`

### Closing the Loop

When you return to code affected by a prior decision, Claude can update the outcome:

```
update_decision_outcome({
  title: "Use PostgreSQL over MongoDB for user data",
  outcome: "successful",
  lesson: "JSONB columns handled the few schema-flexible cases without needing MongoDB"
})
```

Valid outcomes: `pending`, `successful`, `failed`, `revised`, `adopted`, `rejected`, `deferred`.

### Why This Matters

- **`graph_stats`** surfaces pending decisions count — Claude can proactively ask about unresolved decisions
- **`smart_recall.py`** shows pending decisions at session start — they stay visible until resolved
- **`traverse_relations`** connects decisions to the code they affect — Claude sees the full context
- **Maintenance scoring** treats decisions like any other entity — stale unresolved decisions eventually surface as worth revisiting or pruning

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

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `decay_threshold` | 0.1 | 0.0-10.0 | Entities scoring below this are pruned |
| `max_age_days` | 90 | 1-3650 | Entities older than this (with no updates) score poorly |
| `throttle_hours` | 24 | 0.1-720 | Minimum hours between maintenance runs |
| `min_merge_name_len` | 4 | 1-100 | Names shorter than this only merge on exact match |

Delete any key to use the default. Long-lived projects may want `max_age_days: 365`; fast-moving prototypes may prefer `throttle_hours: 12`.

**Tuning tips:**
- Getting too many stale entities? Raise `decay_threshold` (e.g. 0.2) to prune more aggressively, or reduce `max_age_days`
- Important entities getting pruned? Lower `decay_threshold` (e.g. 0.05), or search for them more often (recall boost protects frequently-accessed entities)
- Want more frequent index rebuilds? Lower `throttle_hours` to 12 or even 1

## Architecture

### Directory Layout

```
~/.claude/                          GLOBAL — runtime only (lean)
  settings.json                     Hook wiring (auto-configured by install.sh)
  hooks/
    prime-memory.sh                 SessionStart: maintenance + smart recall
    smart_recall.py                 SessionStart: scored top-N summary
    capture-tool-context.sh         PostToolUse: observation capture
    capture_tool_context.py         PostToolUse: Python worker (stdin parser)
    capture-decisions.sh            Stop: decision persistence reminder
    nudge-setup.sh                  SessionStart: setup nudge (1x/day)
  memory/
    maintenance.py                  Decay / prune / consolidate / TF-IDF
    semantic_server/                MCP server package (13 modules)
    semantic_server.py              Backwards-compatible entry shim

easy-memory-claude/                 SOURCE — install + dev
  install.sh                        Deploys runtime to ~/.claude/
  setup-project.sh                  Initializes any project
  cleanup.sh                        Remove artifacts (project/global/all)
  export-memory.sh                  Export graph to portable JSON bundle
  import-memory.sh                  Import/merge bundle into project
  maintenance.py                    Source copy
  semantic_server/                  MCP server package (source)
  semantic_server.py                Backwards-compatible entry shim

<any-project>/                      PER-PROJECT — gitignored
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

### MCP Server Package Structure

```
semantic_server/
  __init__.py      Package entry point — exposes main()
  __main__.py      Allows: python3 -m semantic_server
  _json.py         Fast JSON backend (orjson -> ujson -> stdlib, error normalization)
  config.py        Constants, limits, pre-compiled regex, timestamp normalization
  cache.py         Mtime-based caches with size-aware eviction (50MB cap)
  recall.py        Hebbian recall tracking (OrderedDict LRU, 10K cap)
  graph.py         JSONL parsing, incremental reads, locking, atomic writes
  search.py        TF-IDF cosine similarity + time-based search
  traverse.py      BFS relation traversal with cached adjacency lists
  tools.py         Write operations + decision tracking tools
  protocol.py      MCP tool schemas + JSON-RPC 2.0 dispatch
  server.py        Stdio event loop, signal handling, sidecar merge
  logging.py       Structured event logging + per-session activity counters
```

| Module | Responsibility | Key Exports |
|--------|---------------|-------------|
| `_json.py` | JSON backend with error normalization (orjson/ujson raise different exceptions — normalized to `ValueError`) | `loads()`, `dumps()`, `load()`, `dump()` |
| `config.py` | All tunable constants, limits, and shared utilities | `MAX_CACHE_BYTES`, `PROTOCOL_VERSION`, `now_iso()`, `normalize_iso_ts()` |
| `cache.py` | In-memory caches for index, entities, relations, adjacency; sampling-based size estimation | `estimate_size()`, `maybe_evict_caches()` |
| `recall.py` | Tracks how often entities appear in search results (Hebbian reinforcement) | `record_recalls()`, `flush_recall_counts()` |
| `graph.py` | All disk I/O for `graph.jsonl` — parse, cache, lock, append, rewrite, incremental byte-offset reads | `load_graph_entities()`, `GraphLock`, `append_jsonl()`, `rewrite_graph()` |
| `search.py` | TF-IDF cosine similarity with postings-based candidate selection + time-range queries | `search()`, `search_by_time()` |
| `traverse.py` | BFS graph traversal with cached adjacency lists and visited-set cap | `traverse_relations()` |
| `tools.py` | CRUD operations + decision tracking + graph stats | `create_entities()`, `create_decision()`, `graph_stats()` |
| `protocol.py` | MCP tool schema definitions + JSON-RPC message routing | `TOOLS`, `handle_message()` |
| `server.py` | Stdio event loop with `select()`, signal handling, cooperative index reload, sidecar merge | `main()` |
| `logging.py` | Structured event logging to stderr + per-session activity counters | `log_event()`, `session_stats` |

**Dependency flow** (no circular imports):
```
  _json  ◀── graph, protocol, server
  config ◀── cache, recall, graph, search, tools, protocol, server
  cache  ◀── graph, traverse, search, tools, server
  recall ◀── search, protocol, server

  graph  ◀── search ◀── protocol ◀── server
  graph  ◀── traverse ◀── protocol
  graph  ◀── tools ◀──── protocol

  logging ◀── search, tools, server  (standalone, no deps)
```

## Performance

Sub-second on graphs up to 100K entities. Searches use a postings index (O(k) candidates, not full scan). Writes are O(1) appends — no rewrite until maintenance or delete.

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| `semantic_search_memory` | O(k) via postings | Only scores candidates sharing query terms; heap-based top-k |
| `traverse_relations` | O(V+E) BFS | Cached adjacency lists, invalidated on graph change |
| `create_entities` (new) | O(1) append | No graph rewrite — appends JSONL lines with flock |
| `create_entities` (merge) | O(1) append | Appends duplicate; maintenance consolidates later |
| `add_observations` | O(1) append | Append-only — deduplicates against cached entity |
| `delete_entities` | O(n) rewrite | Must rewrite to remove entries; locked + atomic |
| `maintenance` | O(n log n) | Sorted-merge consolidation avoids O(n^2) pairwise |
| `load_graph_entities` (incremental) | O(delta) | Byte-offset tracking reads only new appends since last parse |

### Resource Caps

| Resource | Cap | Eviction Strategy |
|----------|-----|-------------------|
| Graph file size | 50 MB | Write operations rejected above this |
| Entities parsed from graph | 100,000 | Excess silently skipped during parse |
| Combined cache size | 50 MB | Evicts in priority order: index -> adjacency -> entity -> relation |
| Recall count entries | 10,000 | Least-recently-used evicted (OrderedDict LRU) |
| Observations/entity (cached) | 3 | Full observations available in graph; cache keeps newest 3 |
| Observations/entity (on disk) | 50 / 200 | activity-log: 50, others: 200; oldest trimmed by maintenance |
| BFS traversal | 10,000 nodes | Traversal capped to prevent OOM on dense graphs |
| Search candidates | 1,000 | Postings intersection capped; rarest terms scored first |
| Query length | 10,000 chars | Truncated silently |
| Parse time budget | 10 seconds | Long parses aborted to prevent blocking |

## Resilience

| Scenario | Behavior |
|----------|----------|
| Branch switch | Hooks live in `~/.claude/`, not project tree. Memory persists (gitignored). Entities stamped with `_branch` |
| graph.jsonl corrupt | Backup taken before every maintenance run. Atomic writes prevent partial corruption |
| graph.jsonl missing | Server auto-creates on first write — no errors, no blocking |
| Project not initialized | Hooks skip silently. Nudge hook shows one-time notice per day |
| Concurrent writes | File locking (`flock`) prevents corruption from simultaneous MCP server + hook writes |
| Power loss during write | `fsync` + temp file + `os.replace` ensures either old or new data survives, never partial |
| Maintenance already running | Lock-based mutual exclusion — second instance skips gracefully |
| Write fails (disk full) | In-memory cache stays consistent — cache only cleared after successful write |
| Observation bloat | Activity-log entities capped at 50 observations, others at 200 — keeps newest, trims during maintenance |
| Recall count bloat | Stale recall entries pruned during maintenance. In-memory dict capped at 10,000 entries with LRU eviction |
| Duplicate entity names | Append-only writes create duplicate JSONL entries. Server merges on parse with cached dedup keys |
| SIGTERM / SIGINT | Server flushes recall counts and exits cleanly on both signals |
| Lock contention in hooks | `capture-tool-context` uses non-blocking `LOCK_NB` with retry — skips write on timeout |
| Out of memory during maintenance | `MemoryError` caught during graph load — maintenance skips gracefully |
| Unserializable entries | Skipped per-entry during writes — one bad entry never crashes a graph rewrite |
| Graph modified during maintenance | Detected via pre/post mtime comparison — maintenance skips write and retries next run |
| Hook sidecar crash | `.processing` file survives and gets reprocessed on next server merge cycle |
| JSON backend mismatch | All backends (orjson/ujson/stdlib) normalize parse errors to `ValueError` — single except clause works everywhere |

## Cross-Machine Sync

Export and import memory graphs between machines:

```bash
# On source machine — creates a portable JSON bundle
./export-memory.sh /path/to/project

# Transfer the .json bundle, then on target machine
./import-memory.sh bundle.json /path/to/project
```

Import merges entities (deduplicating by name+type) and relations. A backup is created before merging. Both export and import validate structure and reject oversized graphs (>50MB).

## Cleanup

Remove all easy-memory-claude artifacts for a fresh start:

```bash
# Dry run — see what would be removed
./cleanup.sh project /path/to/project --dry-run

# Remove per-project data (.memory/, MCP configs, .gitignore entry)
./cleanup.sh project /path/to/project

# Remove global runtime + hooks + settings entries
./cleanup.sh global

# Remove everything (global + project)
./cleanup.sh all /path/to/project
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
| "Write failed (lock timeout)" | Another process holds the graph lock. Check for stuck maintenance or concurrent MCP instances. Lock releases when the holding process exits. |
| Entities not appearing in search | New entities are searchable via `traverse_relations` and `search_memory_by_time` immediately. TF-IDF search requires an index rebuild (next maintenance run). |

## Known Limitations

- **TF-IDF, not embeddings** — semantic search uses term-frequency similarity, not deep embeddings. It finds lexically related concepts but won't understand synonyms or paraphrases that share no words. Future: optional neural upgrade via `sentence-transformers` (see `requirements.txt`).
- **No read locking** — the `prime-memory.sh` hook reads `graph.jsonl` without a lock. During a concurrent full rewrite, it could read partial data. Impact: garbled session summary (self-corrects on next session). Append-only writes are safe to read concurrently.
- **ASCII camelCase splitting** — `normalize_name()` in maintenance handles ASCII + Latin-1 supplement only. CJK or other scripts won't be split on case boundaries (they still merge on exact match).
- **Single-machine scope** — the knowledge graph is local to one machine per project. Cross-machine sync requires manual export/import (see [Cross-Machine Sync](#cross-machine-sync)).

## Platform Support

| Platform | Status |
|----------|--------|
| macOS (ARM64/x86) | Full support |
| Linux (x86/ARM) | Full support |
| Windows (WSL) | Works under WSL. Native Windows: file locking disabled (no `fcntl`), all other features work. |

## License

MIT

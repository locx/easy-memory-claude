# easy-memory-claude

**Persistent, self-maintaining memory for Claude Code.**

Every session ends. Every decision, bug fix, and pattern vanishes. Next session: blank slate.

This plugin gives Claude a **knowledge graph** — entities, relationships, and observations that survive sessions, strengthen with use, and prune themselves when stale.

```
Session 1: "Use LWW for conflict resolution in SyncManager"
   └─ Claude records decision + rationale + links to SyncManager

Session 2: "How does our sync work?"                         ┌─ ProviderRegistry
   └─ Finds SyncManager (score 0.87) ─── traverses to ──────┤
      sees the LWW decision, full context                    └─ OfflineQueue
```

Pure Python. Zero dependencies. One install, every project.

---

## Contents

| # | Section | Covers |
|--:|---------|--------|
| **1** | [**What Makes This Different**](#1-what-makes-this-different) | Branch-aware · Hebbian · Self-regulating · Decisions · Incremental · Atomic |
| **2** | [**How It Works**](#2-how-it-works) | Knowledge Graph · Tool Reference · Search · Self-Cleaning · Lifecycle Hooks |
| **3** | [**Getting Started**](#3-getting-started) | First Run · Day-Two Operations |
| **4** | [**Usage**](#4-usage) | Zero Effort · A Session in Action |
| **5** | [**Decision Tracking**](#5-decision-tracking) | Record a Decision · Update the Outcome |
| **6** | [**Configuration**](#6-configuration) | Decay · Age · Throttle · Merge |
| **7** | [**Architecture**](#7-architecture) | Layout · Under the Hood |
| **8** | [**Performance**](#8-performance) | Complexity · Hard Limits |
| **9** | [**Resilience**](#9-resilience) | Branch · Crash · Lock · Power Loss |
| **10** | [**Troubleshooting**](#10-troubleshooting) | Common Fixes |
| **11** | [**Known Limitations**](#11-known-limitations) | TF-IDF · Locking · CJK · VSCode |
| **12** | [**Platform & License**](#12-platform--license) | macOS · Linux · WSL · MIT |

---

## 1. What Makes This Different

Not another flat-file memory. Six things set this apart:

| Feature | What it means |
|---------|--------------|
| **Branch-aware scoring** | `feature/auth` entities rank higher on that branch. Switch to `main`? Automatic rebalance. Cross-branch at 85–95%, never lost. |
| **Hebbian recall** | Search something 10× → prune-proof. Never touch it → fades. `log(recall_count)` boost baked into every score. |
| **Self-regulating growth** | Daily maintenance scores every entity. Below 0.1 → pruned. Above → kept. Zero intervention, zero bloat. |
| **Structured decisions** | Not comments. Rationale, alternatives, outcomes. Auto-linked to code. Pending ones surface every session until resolved. |
| **Incremental reads** | Byte-offset tracking. 3 new lines in a 50 MB graph? Reads only those 3 lines. |
| **Atomic everything** | `flock` → temp → `fsync` → `os.replace`. Power loss mid-write? Old or new survives. Never corrupt. |

> **CLAUDE.md** for static rules. **Knowledge graph** for everything that evolves. Use both.

---

## 2. How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SESSION LIFECYCLE                            │
│                                                                     │
│  ┌──────────────┐    ┌────────────────┐    ┌─────────────────────┐  │
│  │ Session Start│    │ During Session │    │ Session End         │  │
│  │              │    │                │    │                     │  │
│  │ prime-       │    │  CLI Bridge    │    │ capture-decisions   │  │
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

### a. Knowledge Graph

One `graph.jsonl` per project. Two object types:

```json
{"type":"entity","name":"SyncManager","entityType":"component",
 "observations":["Uses LWW resolution","Custom HTTP sync"],
 "_branch":"feature/sync","_created":"2026-03-10T14:00:00Z"}

{"type":"relation","from":"SyncManager","to":"ProviderRegistry","relationType":"uses"}
```

Entities carry facts, branch tags, and timestamps. Relations form directed edges. Claude traverses connections to find knowledge it wasn't told to look for.

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

### b. Tool Reference

All via `memory-cli.py` — direct Python import, no server, zero startup. Works in CLI and VSCode.

```bash
MEMORY_DIR=$PWD/.memory PYTHONPATH=~/.claude/memory \
  python3 ~/.claude/memory/memory-cli.py <tool> '<json_args>'
```

`setup-project.sh` injects this into `CLAUDE.md`. Claude uses them autonomously.

| Category | Tool | Does |
|----------|------|------|
| **Read** | `semantic_search_memory` | TF-IDF ranked search across names + observations |
| | `traverse_relations` | BFS graph walk — connected subgraph |
| | `search_memory_by_time` | Entities by time range |
| | `graph_stats` | Counts, types, pending decisions, recall rankings |
| **Write** | `create_entities` | Add/merge entities with observations |
| | `create_relations` | Directed edges, auto-deduplicated |
| | `add_observations` | Append facts to existing entities |
| | `delete_entities` | Remove + cascade relation cleanup |
| **Decide** | `create_decision` | Rationale, alternatives, scope, auto-linked |
| | `update_decision_outcome` | Outcome + lesson learned |

All writes: `flock` → temp → `fsync` → `os.replace`. Graph capped at 50 MB.

### c. Semantic Search - How Search Works

TF-IDF cosine similarity over an inverted postings index. Not grep.

1. **Term Frequency** — how often a word appears in an entity's name + observations
2. **Inverse Document Frequency** — rare words weighted higher
3. **Cosine Similarity** — query and entity compared as vectors

Searching for "sync conflict resolution" finds "LWW merge strategy" if they share weighted terms. Only candidates sharing query terms are scored — not the full graph.

### d. Maintenance - Self-Cleaning Pipeline

Once daily, automatic, zero intervention. Runs at session start if >24h since last run.

```
graph.jsonl ──▶ backup ──▶ stamp ──▶ score ──▶ prune ──▶ consolidate
                                                              │
                graph.jsonl ◀── index ◀── prune recall ◀── cap obs
```

| Step | What it does |
|------|-------------|
| **Backup** | Hard-links `graph.jsonl` (O(1)) before mutation |
| **Stamp** | Tags new entities with `_branch` and `_created` metadata |
| **Score** | `obs_count × (1 / (1 + days_stale)) × (1 + log(recall_count))` |
| **Prune** | Removes entities scoring < 0.1 with zero inbound relations |
| **Consolidate** | Merges entities with overlapping names + same type |
| **Cap obs** | activity-log: keep 50; others: keep 200 |
| **Build TF-IDF** | Rebuilds search vectors, postings, magnitudes |

> **Scoring in practice:**
> - 5 obs, today, searched 10× → **16.5** — kept
> - 2 obs, 60 days stale, never searched → **0.03** — pruned
> - Any score + inbound relations → **never pruned**

### e. Lifecycle Hooks

Four hooks, CLI only. VSCode detected and skipped in <1ms — `CLAUDE.md` handles it.

| Hook | Event | Action |
|------|-------|--------|
| `prime-memory.sh` | SessionStart | Maintenance + scored recall with 1-hop relations + pending decisions |
| `capture-tool-context.sh` | PostToolUse | File edits → graph observations (throttled 1x/30s) |
| `capture-decisions.sh` | Stop | Persist-decisions reminder |
| `nudge-setup.sh` | SessionStart | One-time setup notice (no `.memory/`) |

---

## 3. Getting Started

Requires **Python 3.10+** and **git** in `$PATH`. No pip packages, no Node.js.

### a. First Run

```bash
git clone https://github.com/locx/easy-memory-claude.git
cd easy-memory-claude
./install.sh                              # → ~/.claude/memory/ + hooks/ + settings.json
./setup-project.sh /path/to/project       # → .memory/ + CLAUDE.md bridge + TF-IDF index

# Verify
MEMORY_DIR=/path/to/project/.memory PYTHONPATH=~/.claude/memory \
  python3 ~/.claude/memory/memory-cli.py graph_stats
```

> | Script | What it does |
> |--------|-------------|
> | **`install.sh`** | Deploys runtime, CLI bridge, hooks, wires `settings.json` |
> | **`setup-project.sh`** | Creates `.memory/`, injects bridge into `CLAUDE.md`, bootstraps graph (up to 200 files), removes legacy MCP configs |
>
> Both are safe to re-run. `setup-project.sh` upgrades the `CLAUDE.md` section in place.

### b. Day-Two Operations

```bash
# Upgrade — re-run both, preserves graph data
./install.sh && ./setup-project.sh /path/to/project

# Cross-machine sync
./export-memory.sh /path/to/project            # portable .json bundle
./import-memory.sh bundle.json /path/to/project # merge with dedup + backup

# Cleanup — prompts before destructive steps (--yes to skip)
./cleanup.sh project /path --dry-run   # preview
./cleanup.sh project /path             # one project
./cleanup.sh global                    # runtime + hooks
./cleanup.sh all /path                 # everything
```

---

## 4. Usage

### a. Zero Effort - You Do Nothing

The hooks and `CLAUDE.md` handle everything. No commands to memorize, no workflow changes.

**Session start** (CLI) — maintenance runs if due, top-5 entities with 1-hop relations injected:

```
Memory: 42e 28r 3d 0w branch:main
  SyncManager (component): Uses LWW | uses→ProviderRegistry
  AuthService (service): JWT 24h | authenticates→UserModel
  [pending] Migration strategy v2
```

**During work** — Claude silently searches before editing unfamiliar code, records decisions, flags fragile code, links related entities.

**Session end** (CLI) — reminder to persist remaining decisions.

**VSCode** — same tools, same behavior. `CLAUDE.md` bridge drives it. No hooks needed.

> New entities are immediately traversable. TF-IDF search updates at next maintenance.

### b. A Session in Action

```
1. USER: "How does our sync handle conflicts?"
   Claude → semantic_search_memory("sync conflict") → SyncManager (0.87)
   Claude → traverse_relations("SyncManager") → ProviderRegistry, ConfigStore

2. USER: "Switch from LWW to CRDT"
   Claude edits code → hooks capture observations
   Claude silently: create_decision({
     title: "Switch from LWW to CRDT",
     rationale: "Preserve concurrent edits",
     alternatives: ["LWW — simpler but loses writes"]
   })
```

---

## 5. Decision Tracking

Decisions are **structured graph entities** — not notes, not comments.

### a. Record a Decision

```
create_decision({
  title: "Use PostgreSQL over MongoDB for user data",
  rationale: "ACID for billing. Relational model fits user/org hierarchy.",
  alternatives: ["MongoDB — no multi-doc txns", "CockroachDB — too much ops"],
  scope: "UserService, BillingModule",
  related_entities: ["UserService", "BillingModule"]
})
```

Creates a `decision` entity with observations + `decided-for` edges to related entities.

### b. Update the Outcome

```
update_decision_outcome({
  title: "Use PostgreSQL over MongoDB for user data",
  outcome: "successful",
  lesson: "JSONB covered schema-flex cases"
})
```

Outcomes: `pending` · `successful` · `failed` · `revised` · `adopted` · `rejected` · `deferred`

Pending decisions surface **every session start** until resolved. Stale unresolved ones bubble up through decay scoring.

---

## 6. Configuration

`.memory/config.json` — delete any key for default:

| Key | Default | Effect |
|-----|---------|--------|
| `decay_threshold` | 0.1 | Score floor for pruning |
| `max_age_days` | 90 | Age penalty ceiling |
| `throttle_hours` | 24 | Maintenance frequency |
| `min_merge_name_len` | 4 | Exact-match threshold for short names |

Long-lived projects → `max_age_days: 365`. Fast prototypes → `throttle_hours: 12`.

---

## 7. Architecture

```
~/.claude/                            GLOBAL RUNTIME
  hooks/
    prime-memory.sh                   SessionStart → maintenance + recall
    smart_recall.py                   Scored top-N + 1-hop relations
    capture-tool-context.sh/.py       PostToolUse → observations
    capture-decisions.sh              Stop → decision reminder
    nudge-setup.sh                    SessionStart → setup nudge
  memory/
    maintenance.py                    Decay / prune / merge / TF-IDF
    memory-cli.py                     CLI bridge (primary access)
    semantic_server/                  12-module tool package

<project>/                            PER-PROJECT (gitignored)
  CLAUDE.md                           Bridge instructions
  .memory/
    graph.jsonl                       The graph (append-only)
    tfidf_index.json                  Search index
    recall_counts.json                Hebbian frequencies
    config.json                       Overrides
    graph.jsonl.bak                   Pre-maintenance backup
```

### a. Under the Hood - Package Internals

`semantic_server/` — 12 modules in 3 layers:

| Layer | Modules | Role |
|-------|---------|------|
| **Storage** | `graph.py`, `_json.py` | Incremental byte-offset reads, flock + atomic writes, orjson fallback |
| **Intelligence** | `search.py`, `traverse.py`, `recall.py` | TF-IDF + postings, BFS + cached adjacency, Hebbian LRU |
| **Operations** | `tools.py`, `cache.py` | Write ops + decisions, mtime caches + tiered eviction |

Eviction order: index (largest, cheapest) → adjacency → entities → relations.

---

## 8. Performance

Sub-second up to 100K entities. Writes are O(1) appends.

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| `semantic_search_memory` | O(k) | Postings index; heap-based top-k |
| `traverse_relations` | O(V+E) | Cached adjacency lists |
| `create_entities` | O(1) | Append-only JSONL with flock |
| `delete_entities` | O(n) | Must rewrite graph; locked + atomic |
| `maintenance` | O(n log n) | Sorted-merge consolidation |

### a. Hard Limits

| Resource | Cap |
|----------|-----|
| Graph file | 50 MB |
| Entities | 100K |
| Combined cache | 50 MB |
| Recall entries | 10K LRU |
| Obs/entity | 50 activity-log / 200 others |
| BFS depth | 10K nodes |
| Parse budget | 10s |

---

## 9. Resilience

| Scenario | Behavior |
|----------|----------|
| Branch switch | `_branch` tags rebalance scores — cross-branch at 85-95%, never lost |
| graph.jsonl corrupt | Backup before maintenance; atomic writes prevent partial corruption |
| graph.jsonl missing | Auto-creates on first write |
| Concurrent writes | `flock` mutual exclusion |
| Power loss mid-write | `fsync` + `os.replace` — old or new survives, never partial |
| Duplicate entities | Deduplicated on parse, merged by maintenance |
| Graph edited during maintenance | mtime guard → skip, retry next run |

---

## 10. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tools missing | Re-run `install.sh` |
| Search empty | Rebuild index: `python3 ~/.claude/memory/maintenance.py /path` |
| Graph too large | Raise `decay_threshold` or run maintenance |
| Hooks silent in VSCode | [Expected](https://github.com/anthropics/claude-code/issues/21736) — CLI bridge in `CLAUDE.md` handles it |
| Agent asks permission | Re-run `setup-project.sh` — needs "Mandatory behavior" in `CLAUDE.md` |
| Maintenance stuck | Delete `.memory/.last-maintenance` |

---

## 11. Known Limitations

- **TF-IDF, not embeddings** — lexical similarity only. "automobile" won't match "car". Future: `sentence-transformers`.
- **No read locking** — reads during maintenance may see partial data. Self-corrects next session.
- **ASCII name splitting** — CJK/non-Latin merges on exact match only.
- **Single machine** — export/import for cross-machine sync.
- **VSCode hooks** — silently discarded ([#21736](https://github.com/anthropics/claude-code/issues/21736), [#6305](https://github.com/anthropics/claude-code/issues/6305)). CLI bridge works identically.

---

## 12. Platform & License

### a. Platforms

macOS (ARM64/x86) · Linux (x86/ARM) · Windows via WSL (native: no `fcntl`, everything else works)

### b. License

MIT

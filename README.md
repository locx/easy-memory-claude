# easy-memory-claude 🧠

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10+-yellow.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20WSL-lightgrey.svg)](https://github.com/locx/easy-memory-claude)

**Hardened, high-performance, self-governing memory for Claude Code agents.**

Stop starting every session from scratch. **easy-memory-claude** provides a persistent knowledge graph that captures decisions, remembers buggy patterns, and strengthens with every turn.

### 🚀 Zero Dependency · Zero Latency · Pure Python

```
Session 1: "Use LWW for SyncManager" ──▶ Capture Decision + Context
Session 2: "How does sync work?"     ──▶ Recall Decision + Graph Neighbors (0.87)
```

---

## Contents

|      # | Section | Key Focus |
| -----: | --------------------------------------------------- | --------------------------------------------------------------------------- |
|  **1** | [**Memory Advantage**](#1-memory-advantage)         | Branch-aware · Hebbian · Atomic Resilience |
|  **2** | [**How It Works**](#2-how-it-works)                 | Graph · Tool Reference · Search · Maintenance |
|  **3** | [**Getting Started**](#3-getting-started)           | First Run · Installation · Script Summary |
|  **4** | [**Operations & Usage**](#4-operations--usage)       | Zero Effort · Session Walkthrough · Export · Cleanup |
|  **5** | [**Decision Tracking**](#5-decision-tracking)       | Record · List · Update Outcome |
|  **6** | [**Configuration**](#6-configuration)               | Limits · Tuning · Default Values |
|  **7** | [**Architecture & Design**](#7-architecture--design)| Design Philosophy · Layout · Package Internals |
|  **8** | [**Performance & Scale**](#8-performance--scale)    | Complexity · Hard Limits |
|  **9** | [**Resilience & Safety**](#9-resilience--safety)     | Branching · Safety · Recovery Logic |
| **10** | [**Project Info**](#10-project-info)                 | Troubleshooting · Limitations · Platform · License |

---

## 1. Memory Advantage

Architecture-aware memory, not just flat files.

| Feature                  | Impact                                                                                    |
| ------------------------ | ----------------------------------------------------------------------------------------- |
| **Branch-aware**         | 🌿 Scores rebalance automatically as you switch git branches. `main` is always preserved. |
| **Hebbian Recall**       | 🧠 Frequently searched knowledge is reinforced; untouched data fades out.                 |
| **Self-Regulating**      | 🔄 Daily maintenance scores and prunes the graph. Zero intervention required.             |
| **Structured Decisions** | 📝 Captures rationale, chosen approach, and alternatives. Linked directly to components.  |
| **Atomic Resilience**    | 🛡️ `flock` + `fsync` + `os.replace`. Old or new survives; the graph never corrupts.       |
| **Incremental I/O**      | ⚡ Only scales with changes, not graph size. 50MB graph? Reads are still sub-second.      |

> **CLAUDE.md** defines your static rules; the **Knowledge Graph** tracks your architectural evolution.

---

## 2. How It Works

<div align="center">
<pre>
┌─────────────────────────────────────────────────────────────────────┐
│                        SESSION LIFECYCLE                            │
│                                                                     │
│  ┌──────────────┐    ┌────────────────┐    ┌─────────────────────┐  │
│  │ Session Start│    │ During Session │    │ Session End         │  │
│  │              │    │                │    │                     │  │
│  │ prime-       │    │  CLI Bridge    │    │ capture-decisions   │  │
│  │ memory.sh    │──▶ │  (9 commands)  │──▶ │ reminds Claude to   │  │
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
</pre>
</div>

### a. Knowledge Graph

One `graph.jsonl` per project. Two object types: **Entities** (nodes) and **Relations** (edges).

```json
{"type":"entity","name":"SyncManager","entityType":"component",
 "observations":["Uses LWW resolution","Custom HTTP sync"],
 "_branch":"feature/sync","_created":"2026-03-10T14:00:00Z"}

{"type":"relation","from":"SyncManager","to":"ProviderRegistry","relationType":"uses"}
```

Entities carry facts, branch tags, and timestamps. Relations form directed edges. Claude traverses connections to find knowledge it wasn't told to look for.

```
                                                    ┌─────────────────┐
                                          uses      │ ProviderRegistry│
                    ┌──────────────────────────────▶│   (component)   │
                    │                               └─────────────────┘
                    │
                ┌───┴───────────┐
                │  SyncManager  │
                │  (component)  │
                │               │                   ┌─────────────────┐
                │ "Uses LWW     │    depends-on     │  OfflineQueue   │
                │  resolution"  │──────────────────▶│  (component)    │
                └───────────────┘                   └─────────────────┘
```

### b. Tool Reference

All access via the `mem` CLI wrapper (backed by `memory-cli.py`).

| Command       | Role                                               |
| ------------- | -------------------------------------------------- |
| `mem search`  | Ranked lexical lookup (TF-IDF + Porter Stemming)   |
| `mem recall`  | Smart context: Search + 1-hop graph neighbors      |
| `mem write`   | Create/merge entities, relations, and observations |
| `mem decide`  | Track architectural trade-offs & rationales        |
| `mem remove`  | Atomic deletion or renaming                        |
| `mem status`  | Real-time health, stats, and pending nudges        |
| `mem diff`    | Changes since last session                         |
| `mem doctor`  | Locate orphans, technical debt, and stale indices  |
| `mem rebuild` | Force a global project re-scan & index update      |

### c. How Search Works

High-speed lexical retrieval via **TF-IDF cosine similarity** over a positional **inverted index**.

- 🧩 **Stemming**: Integrated Porter Stemmer matches variants (e.g., `running` -> `run`).
- 📂 **Inverted Index**: Only entities with query terms are scored. O(1) fetch, O(k) ranking.
- 📐 **Vector Scoring**: Query and documents are compared as vectors. Length-normalization (cosine) ensures short, dense matches aren't buried.
- ⚖️ **Distinctive Weights**: Uses BM25-style scaling to prioritize rare terms over common boilerplate.

### d. Maintenance - Self-Cleaning Pipeline

Once daily, automatic, zero intervention. Runs at session start if >24h since last run.

```
      graph.jsonl ──▶ backup ──▶ stamp ──▶ score ──▶ prune ──▶ consolidate
                                                                    │
                      graph.jsonl ◀── index ◀── prune recall ◀── cap obs
```

| Phase        | Logic                                            |
| ------------ | ------------------------------------------------ |
| **Backup**   | O(1) hard-link before any mutation               |
| **Stamping** | Automatic `_branch` and `_created` tagging       |
| **Scoring**  | `obs_count × recency_weight × log(recall_count)` |
| **Pruning**  | Removes score < 0.1 unless bound by relations    |
| **Indexing** | Full TF-IDF vector rebuild (positional postings) |

### e. Lifecycle Hooks

Four hooks, CLI only. VSCode detected and skipped in <1ms — `CLAUDE.md` handles it.

| Hook                      | Event        | Action                                                               |
| ------------------------- | ------------ | -------------------------------------------------------------------- |
| `prime-memory.sh`         | SessionStart | Maintenance + scored recall with 1-hop relations + pending decisions |
| `capture-tool-context.sh` | PostToolUse  | Surface file warnings from graph (throttled 1x/30s)                  |
| `capture-decisions.sh`    | Stop         | Persist-decisions reminder                                           |
| `nudge-setup.sh`          | SessionStart | One-time setup notice (no `.memory/`)                                |

---

## 3. Getting Started

Requires **Python 3.10+** and **git** in `$PATH`. No pip packages, no Node.js.

### a. First Run

```bash
git clone https://github.com/locx/easy-memory-claude.git
cd easy-memory-claude
./install.sh                # Deploys runtime + hooks + global bridge
./setup-project.sh /path    # Injects CLAUDE.md bridge + bootstraps graph

# Use the 'mem' wrapper globally
export PATH="$HOME/.claude/memory:$PATH"
mem status
```

| Script                 | What it does                                                                                    |
| ---------------------- | ----------------------------------------------------------------------------------------------- |
| **`install.sh`**       | Deploys runtime, CLI bridge, hooks, wires `settings.json`                                       |
| **`setup-project.sh`** | Creates `.memory/`, injects bridge into `CLAUDE.md`, migrates auto-memory, removes legacy MCP   |

---

## 4. Operations & Usage

### a. Zero Effort Usage

The hooks and `CLAUDE.md` handle everything. No commands to memorize, no workflow changes.

**Session start** (CLI) — maintenance runs if due, top-5 entities with 1-hop relations injected.

**During work** — Claude silently searches before editing unfamiliar code, records decisions, flags fragile code, links related entities.

**Session end** (CLI) — reminder to persist remaining decisions.

### b. A Session in Action

```
1. USER: "How does our sync handle conflicts?"
   Claude → mem search "sync conflict" → SyncManager (0.87)
   Claude → mem recall "SyncManager" → ProviderRegistry, ConfigStore

2. USER: "Switch from LWW to CRDT"
   Claude edits code
   Claude silently: mem decide '{"title":"Switch from LWW to CRDT",
     "chosen":"CRDT merge","rationale":"Preserve concurrent edits",
     "alternatives":["LWW — simpler but loses writes"]}'
```

### c. Day-Two - Export & Cleanup

```bash
# Update — re-run both to refresh graph/bridge
./install.sh && ./setup-project.sh /path/to/project

# Export — portable JSON bundles for cross-machine memory
./export-memory.sh /path/to/project            # → portable bundle
./import-memory.sh bundle.json /path/to/project # → merge with dedup

# Cleanup — prompts before destructive steps
./cleanup.sh project /path      # Remove one project graph
./cleanup.sh global             # Remove runtime + hooks
```

---

## 5. Decision Tracking

Decisions are **structured graph entities** — not notes, not comments.

### a. Record a Decision

```bash
mem decide '{
  "title": "Use PostgreSQL over MongoDB for user data",
  "rationale": "ACID for billing. Relational model fits user/org hierarchy.",
  "chosen": "PostgreSQL",
  "alternatives": ["MongoDB — no multi-doc txns", "CockroachDB — too much ops"]
}'
```

### b. List Decisions

```bash
mem status
```

Returns all decisions sorted by recency with their current outcome status. Pending decisions older than 2 days are flagged.

### c. Update the Outcome

```bash
mem decide '{"action":"resolve","title":"Use PostgreSQL over MongoDB","outcome":"successful","lesson":"ACID saved us during billing migration"}'
```

Outcomes: `pending` · `successful` · `failed` · `revised` · `adopted` · `rejected` · `deferred`

---

## 6. Configuration

`.memory/config.json` — delete any key for default:

| Key                  | Default | Effect                                |
| -------------------- | ------- | ------------------------------------- |
| `decay_threshold`    | 0.1     | Score floor for pruning               |
| `max_age_days`       | 90      | Age penalty ceiling                   |
| `throttle_hours`     | 24      | Maintenance frequency                 |
| `min_merge_name_len` | 4       | Exact-match threshold for short names |

---

## 7. Architecture & Design

### a. Design Philosophy

1. **Agent-Centric**: Built for LLMs, not just humans. Terse outputs and graph guidance.
2. **Lean & Native**: Standard Python + Shell. No external DBs, no `pip install`.
3. **Implicit Growth**: Memory is a side-effect of your work, not a burden on your workflow.
4. **Hardened IO**: Every write is a promise. Atomic replace ensures 100% data integrity.

### b. Layout

```
~/.claude/                            GLOBAL RUNTIME
  hooks/
    prime-memory.sh                   SessionStart → maintenance + recall
    capture-tool-context.sh/.py       PostToolUse → file warnings
    capture-decisions.sh              Stop → decision reminder
  memory/
    maintenance.py                    Decay / prune / merge / TF-IDF
    memory-cli.py                     CLI bridge (primary access)
    semantic_server/                  Tool package (16 modules)

<project>/                            PER-PROJECT
  CLAUDE.md                           Bridge instructions
  .memory/
    graph.jsonl                       The graph (append-only)
    tfidf_index.json                  Search index
    recall_counts.json                Hebbian frequencies
```

### c. Under the Hood - Package Internals

Modular, low-latency engine core in `semantic_server/`:

| Component        | Support Modules              | Role                                           |
| ---------------- | ---------------------------- | ---------------------------------------------- |
| **Persistence**  | `graph`, `_json`, `io_utils` | Atomic I/O, byte-offset reads, orjson recovery |
| **Intelligence** | `search`, `recall`, `stem`   | TF-IDF, Hebbian LRU, English Porter Stemmer    |
| **Mechanics**    | `text`, `traverse`           | Tokenization, synonyms, BFS-cached adjacency   |
| **Maintenance**  | `maintenance_utils`          | Strategic pruning & consolidation merging      |
| **Interface**    | `tools`, `cache`             | Command logic & tiered mtime eviction          |

---

## 8. Performance & Scale

Sub-second up to 100K entities. Writes are O(1) appends.

| Operation      | Complexity | Notes                               |
| -------------- | ---------- | ----------------------------------- |
| `mem search`   | O(k)       | Postings index; heap-based top-k    |
| `mem recall`   | O(V+E)     | Cached adjacency lists              |
| `mem write`    | O(1)       | Append-only JSONL with flock        |
| `mem remove`   | O(n)       | Must rewrite graph; locked + atomic |
| `maintenance`  | O(n log n) | Sorted-merge consolidation          |

### a. Hard Limits

| Resource       | Cap                          |
| -------------- | ---------------------------- |
| Graph file     | 50 MB                        |
| Entities       | 100K                         |
| Combined cache | 50 MB                        |
| Recall entries | 10K LRU                      |
| Obs/entity     | 20 cached                    |
| BFS depth      | 10K nodes                    |

---

## 9. Resilience & Safety

| Scenario             | Recovery Logic                                                            |
| -------------------- | ------------------------------------------------------------------------- |
| **Branch Switch**    | Rebalance scores—favor current work while preserving cross-branch links.  |
| **Power Loss**       | `fsync` ensure no partial writes. Old or new survives.                    |
| **Concurrent Write** | `flock` mutual exclusion across CLI instances.                            |
| **Graph Drift**      | Maintenance consolidation merges duplicate entities and stabilizes names. |

---

## 10. Project Info

### a. Troubleshooting

| Symptom               | Fix                                                                   |
| --------------------- | --------------------------------------------------------------------- |
| Tools missing         | Re-run `install.sh`                                                   |
| Search empty          | Rebuild index: `mem rebuild`                                          |
| Graph too large       | Raise `decay_threshold` or run maintenance                            |
| Agent asks permission | Re-run `setup-project.sh` — checks CLAUDE.md bridge instructions      |
| Maintenance stuck     | Delete `.memory/.last-maintenance`                                    |

### b. Known Limitations

- **TF-IDF, not embeddings** — lexical similarity only. Porter stemming handles variants.
- **No read locking** — reads during maintenance may see partial data. Self-corrects.
- **ASCII name splitting** — CJK/non-Latin merges on exact match only.
- **Single machine** — export/import for cross-machine sync.

### c. Platform & License

macOS (ARM64/x86) · Linux (x86/ARM) · Windows via WSL (native: no `fcntl`).

**License**: [MIT](LICENSE)

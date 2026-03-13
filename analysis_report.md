# Analysis Report — easy-memory-claude

## Architecture and Execution Model

### System Components

| Component | File | Role | Runtime |
|-----------|------|------|---------|
| MCP Server | `semantic_server.py` | Long-running stdio JSON-RPC server; 7 tools (search, traverse, time-range, CRUD) | Per-project process, spawned by Claude Code |
| Maintenance | `maintenance.py` | Batch job: decay-score, prune, consolidate, TF-IDF index rebuild | Invoked 1x/day by `prime-memory.sh` hook |
| Capture Hook | `hooks/capture_tool_context.py` | Appends activity-log observations on PostToolUse events | Short-lived process per tool call (throttled 1x/30s) |
| Prime Hook | `hooks/prime-memory.sh` | SessionStart: triggers maintenance, injects graph summary | Short-lived, blocks session start (10s timeout) |
| Nudge Hook | `hooks/nudge-setup.sh` | SessionStart: one-time per-project-per-day setup reminder | Short-lived (3s timeout) |
| Decision Hook | `hooks/capture-decisions.sh` | Stop: one-time-per-session reminder to persist decisions | Short-lived (3s timeout) |

### Entry Points

1. **`semantic_server.py:main()`** — Long-running MCP server on stdio. Reads JSON-RPC lines from stdin, dispatches to tool handlers, writes responses to stdout. Single-threaded, synchronous, blocking readline loop.
2. **`maintenance.py:run(project_dir)`** — One-shot batch processor. Called by prime-memory.sh or directly from CLI. Throttled to 1x/day via `.last-maintenance` marker file.
3. **`capture_tool_context.py:main()`** — One-shot appender. Reads JSON from temp file, appends single JSONL line to graph.

### Request/Data Flow

```
Claude Code ──stdin──▶ semantic_server.py ──JSON-RPC──▶ handle_message()
                                                           │
                              ┌─────────────────────────────┤
                              ▼                             ▼
                         Read Path                    Write Path
                    ┌──────────────┐            ┌──────────────────┐
                    │ load_index() │            │ _append_jsonl()  │
                    │ load_graph_  │            │ _rewrite_graph() │
                    │  entities()  │            │ (lock + fsync)   │
                    └──────┬───────┘            └──────────────────┘
                           │
                    mtime-based cache
                    (in-process dicts)
```

### Concurrency Model

- **Single-threaded, synchronous** — no asyncio, no threads, no multiprocessing.
- **Inter-process coordination** via `fcntl.flock()` on `.graph.lock` file.
- Three concurrent writers possible: MCP server, capture-tool-context hook, maintenance.py.
- No read locking — reads can see partial data during full rewrites (documented as known limitation).

### I/O Operations

| Operation | Frequency | Blocking? | Notes |
|-----------|-----------|-----------|-------|
| `sys.stdin.readline()` | Every request | Yes — blocks main loop | Single-threaded, expected behavior for MCP stdio |
| `graph.jsonl` read | Per search/traverse/write (cached) | Yes | mtime-based cache avoids repeat reads |
| `tfidf_index.json` read | Per search (cached) | Yes | Full JSON parse on cache miss; up to 50MB |
| `recall_counts.json` read/write | Periodic (60s interval) | Yes | Small file, throttled stat() checks |
| `graph.jsonl` append | Per write operation | Yes | Locked via flock, single fsync |
| `graph.jsonl` full rewrite | delete_entities only | Yes | Locked, atomic temp+replace |

### Production Workload Behavior

Under typical usage (1 MCP server per project, <10,000 entities):
- **Search latency** dominated by index cache hit/miss. Cache hit: <1ms (dict lookups). Cache miss: 10–100ms (JSON parse of index file).
- **Write latency** dominated by fsync (~1–5ms on SSD). Lock contention rare in single-user scenarios.
- **Memory footprint** bounded by MAX_CACHE_BYTES (50MB) across index + entity + relation caches. Eviction prioritizes index cache.
- **Maintenance** runs in <1s for graphs under 5,000 entities. Consolidation is the bottleneck for larger graphs due to token-indexed pairwise comparison.

---

## Performance Bottlenecks

### PB-1: Full JSON Parse of TF-IDF Index on Cache Miss

**Location:** `semantic_server.py:276-303` (`load_index()`)

The TF-IDF index is stored as a single monolithic JSON file containing vectors, IDF values, magnitudes, postings, and metadata. On cache miss (first search, or after maintenance rebuilds the index), the entire file is parsed with `json.load()`. For large graphs (50K+ entities), this file can be 10–30MB, causing 200–500ms parse latency.

**Complexity:** O(n) where n = index file size in bytes.

**Impact:** First search after session start or index rebuild experiences noticeable latency.

### PB-2: _parse_graph_file O(n) Full Scan on Cache Miss

**Location:** `semantic_server.py:325-475` (`_parse_graph_file()`)

On entity cache miss, the entire graph.jsonl is read line-by-line, each line JSON-parsed, entities merged by name with observation dedup. For a 50K-line graph, this involves 50K `json.loads()` calls plus set operations for dedup.

**Complexity:** O(n × m) where n = lines, m = average observations per entity (for dedup set operations).

**Impact:** Moderate — incremental read (TASK-3.2) mitigates for append-only changes, but full reparse triggers after maintenance rewrites.

### PB-3: Consolidation Token-Indexed Comparison

**Location:** `maintenance.py:338-515` (`consolidate()`)

Although token indexing avoids full O(n²), the candidate set per entity can grow up to 50 before being capped. High-frequency tokens are filtered at `sqrt(n)` threshold, but for entity counts >10K with common domain terms, the inner loop still processes significant candidate sets. `_safe_obs_set()` is called for every merge pair.

**Complexity:** O(n × k) where k = avg candidates per entity (capped at 50). Worst case approaches O(n × 50) ≈ O(n).

**Impact:** For graphs >10K entities, consolidation can take several seconds.

### PB-4: build_tfidf_index Two-Pass O(n) with Full Tokenization

**Location:** `maintenance.py:572-694` (`build_tfidf_index()`)

Pass 1 tokenizes every entity's name + type + observations (full regex scan). Pass 2 computes TF-IDF vectors for all docs. Both passes iterate all entities. The `_filter_token()` function is called per-token (regex match + frozenset lookup + length check).

**Complexity:** O(n × t) where t = avg tokens per entity document.

**Impact:** Moderate — runs once/day during maintenance. Acceptable for current graph sizes but becomes the dominant cost above 50K entities.

### PB-5: Observation Dedup via String Comparison in create_entities

**Location:** `semantic_server.py:1097-1185` (`create_entities()`)

When merging with existing entities (cache warm), `_obs_dedup_key()` is called for every existing observation to build a set, then again for each new observation. For entities with 200 observations (the cap), this involves 200+ `json.dumps(sort_keys=True)` calls for non-string observations.

**Complexity:** O(e × o) where e = entities being created, o = existing observations per entity.

**Impact:** Low for typical usage (small observation lists), but pathological for entities near the 200-obs cap being frequently updated.

### PB-6: _estimate_size Sampling Overhead

**Location:** `semantic_server.py:199-255` (`_estimate_size()`)

Called after every cache population to estimate memory usage. Iterates up to 50 dict entries, calling `sys.getsizeof()` on nested structures. Not expensive per call but called 3 times per full parse (entities, relations, index).

**Complexity:** O(1) per call (fixed 50-sample cap), but constant factor involves recursive getsizeof.

**Impact:** Low — negligible compared to JSON parse time.

### PB-7: search() Candidate Scoring Linear Scan

**Location:** `semantic_server.py:649-795` (`search()`)

After postings-based candidate filtering, every candidate is scored via dot product over query vector. For multi-term queries matching many entities, candidate set can reach MAX_CANDIDATES (1000). Each candidate requires a dict.get() per query term.

**Complexity:** O(c × q) where c = candidate count (up to 1000), q = query term count.

**Impact:** Low — 1000 candidates × 10 terms = 10K dict lookups, well within acceptable latency.

---

## Memory Inefficiencies

### MI-1: Full Index Held in Memory

**Location:** `semantic_server.py:48-57` (module-level caches)

The entire TF-IDF index (vectors, IDF, magnitudes, postings, metadata) is cached in `_index_cache["data"]` as a nested Python dict. For 50K entities, this can consume 30–80MB of Python heap (2–3× the raw JSON size due to Python object overhead).

**Mitigation:** MAX_CACHE_BYTES (50MB) cap triggers eviction, but the estimate is based on sampling and may undercount. The `_estimate_size()` function only samples 50 entries and extrapolates.

### MI-2: Entity Cache Stores Truncated Observations but Retains _obs_keys Sets

**Location:** `semantic_server.py:385-474` (`_parse_graph_file()`)

During entity merging, `_obs_keys` sets are built for dedup. These are stripped after parsing (line 473-474), but during the parse phase they temporarily double the memory for observation data. For 100K entities with 3 obs each, this means 300K extra string objects in sets.

**Mitigation:** Already stripped post-parse. Impact is on peak memory during parse only.

### MI-3: Maintenance consolidate() Intermediate Structures

**Location:** `maintenance.py:361-464`

`norm_data` list (normalized names + token sets for each entity in a type group) and `token_idx` dict are allocated per entity-type group. For a single type with 30K entities, `norm_data` holds 30K tuples of (string, set). TASK-3.4 already frees these per-group, which is correct.

**Impact:** Bounded per type-group, not cumulative.

### MI-4: import-memory.sh Loads Entire Graph + Bundle into Memory

**Location:** `import-memory.sh` (embedded Python)

Both the existing graph (as a list of dicts) and the import bundle are fully loaded into memory simultaneously. For two 50MB graphs, peak memory could reach 200MB+ (Python dict overhead).

**Impact:** Medium — import is a rare one-shot operation, but could OOM on constrained machines with large graphs.

### MI-5: export-memory.sh Loads All Entries into List

**Location:** `export-memory.sh` (embedded Python)

All graph entries are loaded into a Python list before JSON serialization. For a 50MB graph, this doubles memory (raw list + JSON output buffer).

**Impact:** Low — one-shot operation with 50MB guard.

### MI-6: Adjacency Cache Unbounded

**Location:** `semantic_server.py:59-61` (`_adjacency_cache`)

The adjacency cache (outbound + inbound dicts) is not included in `_estimate_size()` or `_maybe_evict_caches()` calculations. For graphs with 100K relations, this adds significant untracked memory.

**Impact:** Medium — adjacency is typically smaller than entity/index caches, but is invisible to the eviction policy.

---

## I/O and External Resource Efficiency

### IO-1: fsync on Every Append Write

**Location:** `semantic_server.py:1029-1054` (`_append_jsonl()`)

Every `create_entities`, `create_relations`, and `add_observations` call triggers an fsync. While this guarantees durability, it adds 1–5ms latency per write. Rapid successive writes (e.g., creating 10 entities in 10 separate calls) pay this cost 10 times.

**Mitigation:** `_append_jsonl` already batches entries within a single call. The per-call fsync is acceptable for data integrity.

### IO-2: capture_tool_context.py Spawns Python Process Per Tool Call

**Location:** `hooks/capture-tool-context.sh:44-45`

Every PostToolUse event spawns a new `python3` process. Python startup time is 30–80ms. With 30s throttling, this is acceptable, but the overhead is non-trivial.

**Mitigation:** Throttled to 1x/30s. Bytecode caching (`.pyc`) reduces parse time on repeat calls.

### IO-3: prime-memory.sh Inline Python for Graph Summary

**Location:** `hooks/prime-memory.sh:37-74`

An inline Python script reads the entire graph.jsonl to produce a summary (count entities/relations, list top 30). This is a separate read from the MCP server's cache — duplicated I/O.

**Impact:** Low — runs once per session start, graph is small, and summary is useful.

### IO-4: Redundant Graph Stat Checks

**Location:** `semantic_server.py:1783-1794` (main loop cooperative index reload)

Every iteration of the main loop calls `os.path.getmtime()` on the TF-IDF index file before reading stdin. This adds one syscall per request cycle, even when no reload is needed.

**Impact:** Negligible — `getmtime()` is a fast stat() call.

### IO-5: recall_counts.json Full Rewrite on Flush

**Location:** `semantic_server.py:145-175` (`_flush_recall_counts()`)

The entire recall dict is serialized and rewritten on every flush (every 60s if dirty). For 10K entries, this is a ~100KB write.

**Impact:** Low — 60s interval, no fsync (explicitly skipped for non-critical data).

---

## Data Flow Efficiency

### DF-1: Entity Merge on Parse Reconstructs Dedup Sets

**Location:** `semantic_server.py:387-417`

When duplicate entity names are encountered in graph.jsonl (common from append-only writes), `_obs_dedup_key()` is called for every observation to build a dedup set. If the observation list is then truncated to `_MAX_CACHED_OBS` (3), the dedup set is rebuilt from scratch for just 3 items. This is already optimized (TASK-2.5) but the initial set construction for the "losing" observations is wasted work.

### DF-2: Double Serialization in JSON-RPC Response

**Location:** `semantic_server.py:1709-1720`

Tool results are first serialized to JSON string (`json.dumps(result)`), then embedded in the JSON-RPC response wrapper, which is also serialized (`json.dumps(response)`). The inner result is double-serialized (first to string, then escaped within the outer JSON).

**Impact:** Low — MCP protocol requires text content type. This is inherent to the protocol.

### DF-3: Maintenance Reloads Graph After Writing

**Location:** `maintenance.py:949-966`

After writing the pruned/consolidated graph, `build_tfidf_index()` is called with the in-memory entity list. This is efficient — no re-read. However, `_print_graph_stats()` iterates the entity list again for statistics. Minor redundancy.

---

## Risk and Scalability Assessment

### High-Risk Hotspots

| Component | Risk | Trigger | Impact |
|-----------|------|---------|--------|
| `_parse_graph_file()` | **High** | Graph >50K lines with many duplicate entity names | Parse time exceeds `_PARSE_TIME_BUDGET` (10s), truncated results |
| `consolidate()` | **High** | Entity count >10K with common domain vocabulary | Consolidation dominates maintenance time, may hit `_MAX_CONSOLIDATE_ENTITIES` cap |
| `load_index()` JSON parse | **Medium** | Index file >10MB (>20K entities with large observation lists) | 200–500ms first-search latency |
| `_append_jsonl()` under contention | **Medium** | Rapid writes while maintenance is rewriting | Lock timeout (5s) causes write rejection |
| Memory pressure from caches | **Medium** | Large graph + index + adjacency exceeding 50MB cap | Cache thrashing — evict index, reload on next search |

### Scaling Limits

| Resource | Current Cap | Scaling Concern |
|----------|-------------|-----------------|
| Entity count | 100,000 | Consolidation skipped above 50K; parse time budget may truncate |
| Graph file size | 50 MB | Write operations rejected; maintenance required to prune |
| Cache memory | 50 MB (tracked) + untracked adjacency | May exceed available memory on constrained hosts |
| Recall entries | 10,000 | LRU eviction works well; not a concern |
| Observations/entity | 200 (50 for activity-log) | Adequate cap |
| TF-IDF index file | Unbounded (but proportional to entity count) | At 100K entities, index could reach 50MB+ |

### Components Executed Most Frequently

1. **`search()` + `load_index()`** — every semantic search request (most common MCP call)
2. **`load_graph_entities()`** — every traverse, time-search, and write operation
3. **`_obs_dedup_key()`** — called per-observation during parse, create, and add operations
4. **`handle_message()`** — every JSON-RPC request (dispatcher)
5. **`capture_tool_context.py`** — every qualifying tool use (throttled to 1x/30s)

---

## Summary of Findings

The system is well-engineered for its target workload (single-user, <10K entities). Key strengths:
- Mtime-based caching avoids redundant I/O
- Append-only writes with deferred consolidation avoid O(n) rewrites
- Incremental graph reads (TASK-3.2) reduce reparse after appends
- Proper bounds on all resource dimensions (entities, cache, observations, recall)

Primary optimization opportunities:
1. **Index format** — binary or split format to avoid monolithic JSON parse
2. **Adjacency cache tracking** — include in eviction policy
3. **Consolidation algorithmic improvement** — tighter candidate filtering for large graphs
4. **Import/export streaming** — avoid full in-memory loading for large graphs

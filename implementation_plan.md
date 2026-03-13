# Implementation Plan — easy-memory-claude

## Optimization Roadmap

### Phase 1 — Critical Correctness and Stability Fixes

| File | Change | Why |
|------|--------|-----|
| `semantic_server.py` | Include adjacency cache in `_maybe_evict_caches()` size tracking | Adjacency cache is unbounded and invisible to eviction policy (MI-6). Under high relation counts, total memory exceeds MAX_CACHE_BYTES without triggering eviction |
| `semantic_server.py` | Add adjacency cache size to `_estimate_size()` total in `_maybe_evict_caches()` | Prevents silent memory growth that could OOM on constrained hosts |
| `capture_tool_context.py` | Write without fsync when lock not acquired (current: writes without lock) | Currently writes to graph even when lock acquisition fails. Data integrity risk under concurrent full rewrite |

### Phase 2 — Algorithmic Performance Improvements

| File | Change | Why |
|------|--------|-----|
| `maintenance.py` | Pre-filter consolidation candidates by normalized name length similarity | Reduces false candidates — names of vastly different lengths cannot be substrings of each other. Cuts inner-loop iterations |
| `maintenance.py` | Short-circuit `_safe_obs_set()` for all-string observation lists | Common case (>95% of entities) — avoids `isinstance()` + `json.dumps()` overhead on every observation |
| `semantic_server.py` | Cache `_obs_dedup_key()` results during entity merge in `_parse_graph_file()` | Same observations are re-keyed on truncation (TASK-2.5). Caching avoids redundant `json.dumps(sort_keys=True)` calls |
| `semantic_server.py` | Use postings intersection for multi-term queries instead of Counter | Current approach counts hits per entity across all terms, then filters by count ≥ 2. Set intersection of the two rarest terms' postings is faster for high-cardinality posting lists |

### Phase 3 — Memory Optimization

| File | Change | Why |
|------|--------|-----|
| `semantic_server.py` | Track adjacency cache size and include in eviction calculations | MI-6: untracked memory can silently exceed bounds |
| `maintenance.py` | Stream entities in `build_tfidf_index()` Pass 1 to avoid holding `docs` and `meta` dicts simultaneously with entity list | Peak memory = entities list + docs dict + meta dict. Streaming reduces to entities + running accumulators |
| `import-memory.sh` | Stream JSONL merge instead of loading both graph and bundle fully into memory | MI-4: Two 50MB graphs → 200MB+ peak. Streaming merge (sorted by name) reduces to O(max_entity_obs) |
| `export-memory.sh` | Stream entries to output JSON instead of collecting into list | MI-5: Avoid doubling memory for large graphs |

### Phase 4 — I/O Efficiency Improvements

| File | Change | Why |
|------|--------|-----|
| `semantic_server.py` | Add configurable `fsync` parameter to `_append_jsonl()` for non-critical writes | IO-1: recall count updates and activity-log appends don't need fsync durability. Saves 1–5ms per write |
| `semantic_server.py` | Throttle cooperative index reload stat() check to every 5s instead of every request | IO-4: Reduces syscall overhead on rapid request bursts. Index changes are rare (1x/day) |
| `hooks/prime-memory.sh` | Cache graph summary output alongside `.last-maintenance` marker | IO-3: Avoid re-parsing graph.jsonl on every session start when maintenance hasn't run. Summary only changes after maintenance |

### Phase 5 — Concurrency Improvements

| File | Change | Why |
|------|--------|-----|
| `semantic_server.py` | Document and validate that `_GraphLock` retry loop uses non-blocking flock correctly | Current retry: 50 attempts × 100ms sleep = 5s timeout. Correct but the sleep between retries is CPU-idle time. Consider `select()`-based timeout or `LOCK_EX` with `alarm()` for cleaner timeout |
| `capture_tool_context.py` | Skip write entirely (instead of writing without lock) when lock not acquired | Current behavior writes to graph without lock on contention, risking interleaved partial lines during concurrent full rewrite |
| `maintenance.py` | Release entity/relation lists before building TF-IDF index to reduce peak concurrent memory | Already partially done (gc.collect at line 910). Could be more aggressive by passing only the entity list to build_tfidf_index and freeing relations/others explicitly first |

---

## Existing Implementation Phases (Retained)

The above optimization phases complement the project's existing task-based improvements (TASK-1.x through TASK-5.x) which addressed:

- TASK-1: Input bounds and safety guards (lock timeout, cache size estimation, parse time budget, consolidation cap)
- TASK-2: Algorithmic improvements (fast age check, obs dedup, stopword filtering, singleton term exclusion, truncation without rebuild)
- TASK-3: Memory optimizations (cache byte tracking, incremental reads, size estimates, per-group intermediate cleanup, LRU recall eviction)
- TASK-4: I/O optimizations (fsync skip for non-critical data, cold-cache append skip, lock scope reduction)
- TASK-5: Server lifecycle (recall flush between requests, atexit handler, cooperative index reload)

The new optimization phases (1–5 above) target remaining opportunities identified through deep algorithmic and scalability analysis.

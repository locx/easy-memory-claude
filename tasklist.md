# Task List — easy-memory-claude Optimization

## Phase 1 — Critical Correctness and Stability

### OPT-1.1: Track Adjacency Cache in Eviction Policy [DONE]

- **Description:** Include `_adjacency_cache` size in `_maybe_evict_caches()` calculation. Add size tracking field to `_adjacency_cache` dict. Evict adjacency when total exceeds MAX_CACHE_BYTES.
- **Rationale:** Adjacency cache grows proportionally to relation count but is invisible to the 50MB eviction cap (MI-6). On graphs with 100K relations, this can add 10–20MB of untracked memory.
- **Affected files:** `semantic_server.py`
- **Implementation steps:**
  1. Add `"size": 0` to `_adjacency_cache` initialization (line 60)
  2. In `_get_adjacency()`, after building outbound/inbound dicts, call `_estimate_size()` on each and store sum in `_adjacency_cache["size"]`
  3. Add `_adjacency_cache["size"]` to the `total` calculation in `_maybe_evict_caches()`
  4. Add adjacency eviction as a third tier (after index, before entity)
- **Expected outcome:** Memory usage accurately tracked; eviction triggers before OOM on large graphs
- **Priority:** high
- **Complexity:** small
- **Dependencies:** none

### OPT-1.2: Fix Lockless Write in capture_tool_context.py [DONE]

- **Description:** When lock acquisition fails after 250ms, the hook currently writes to graph.jsonl without holding the lock. Change to skip the write entirely and exit with a non-zero code.
- **Rationale:** Writing without lock during a concurrent full rewrite (`_rewrite_graph`) can interleave partial lines, corrupting the JSONL. The 30s throttle means losing one observation is acceptable; data corruption is not.
- **Affected files:** `hooks/capture_tool_context.py`
- **Implementation steps:**
  1. In the `if not acquired:` branch (line 75), add `sys.exit(2)` after the stderr warning
  2. Remove the unlocked write path (lines 80-83 inside the `try/finally`)
  3. Keep the `except (ImportError, OSError)` fallback for Windows only (no fcntl)
- **Expected outcome:** No writes without lock protection; at most one lost observation per 30s interval under contention
- **Priority:** high
- **Complexity:** small
- **Dependencies:** none

---

## Phase 2 — Algorithmic Performance

### OPT-2.1: Pre-filter Consolidation Candidates by Name Length [DONE]

- **Description:** In `consolidate()`, skip candidate pairs where the shorter normalized name cannot be a substring of the longer (i.e., `len(shorter) > len(longer)` is impossible, but `len(shorter) < MIN_MERGE_NAME_LEN` and names differ is already checked). Add length-ratio filter: skip if `len(name_i) / len(name_j) < 0.3` or `> 3.3` (substring containment impossible at extreme ratios).
- **Rationale:** PB-3: Reduces inner loop iterations by ~30–50% for diverse entity name sets. Substring check (`name_i in name_j`) will always fail if the candidate name is less than 1/3 the length of the other.
- **Affected files:** `maintenance.py`
- **Implementation steps:**
  1. After `name_j, _ = norm_data[gj]` (line 420), add: `if len(name_i) > 3 * len(name_j) or len(name_j) > 3 * len(name_i): continue`
  2. This is a constant-time check before the O(n) substring check
- **Expected outcome:** 30–50% fewer substring checks in consolidation inner loop
- **Priority:** medium
- **Complexity:** small
- **Dependencies:** none

### OPT-2.2: Fast-Path _safe_obs_set for All-String Lists [DONE]

- **Description:** Add an early check in `_safe_obs_set()` that detects all-string observation lists (the common case) and uses a simpler dict comprehension without `isinstance()` per element.
- **Rationale:** PB-5: >95% of observation lists are all-strings. The `isinstance()` + `json.dumps()` fallback path is unnecessary overhead for the common case.
- **Affected files:** `maintenance.py`
- **Implementation steps:**
  1. At the top of `_safe_obs_set()`, check if all elements are strings: `if all(isinstance(o, str) for o in observations): return {o: o for o in observations}`
  2. This avoids the per-element isinstance+branch in the main loop
- **Expected outcome:** ~20% faster observation dedup for typical entities
- **Priority:** medium
- **Complexity:** small
- **Dependencies:** none

### OPT-2.3: Postings Intersection for Multi-Term Queries [DONE]

- **Description:** In `search()`, when the query has ≥2 terms, use set intersection of the two rarest terms' posting lists as the initial candidate set, then expand with remaining terms if the intersection is too small.
- **Rationale:** PB-7: Current Counter-based approach iterates all posting entries for all query terms, then filters. Intersection of two smallest posting lists is O(min(|p1|, |p2|)) and typically produces a much smaller candidate set.
- **Affected files:** `semantic_server.py`
- **Implementation steps:**
  1. In the `if len(sorted_terms) >= 2:` branch (line 713), get the two rarest terms' posting lists
  2. Compute their intersection as the initial candidate set
  3. If intersection is empty, fall back to union of the rarest term's postings
  4. Cap at MAX_CANDIDATES as before
- **Expected outcome:** Faster candidate selection for multi-term queries on large indexes
- **Priority:** medium
- **Complexity:** medium
- **Dependencies:** none

---

## Phase 3 — Memory Optimization

### OPT-3.1: Stream TF-IDF Index Build Pass 1

- **Description:** In `build_tfidf_index()`, avoid holding both `docs` dict and the entity list simultaneously. Process entities in a streaming fashion, building DF counts and metadata in a single pass without storing all tokenized documents.
- **Rationale:** PB-4/MI-1: Peak memory during index build = entity list + docs dict + meta dict + DF counter. For 50K entities, docs alone can be 20MB.
- **Affected files:** `maintenance.py`
- **Implementation steps:**
  1. In Pass 1, write tokenized documents to a temporary JSONL file instead of holding in `docs` dict
  2. In Pass 2, stream from the temp file to compute TF-IDF vectors
  3. Delete temp file after index is written
  4. Alternative: accept the current approach since it only runs 1x/day during maintenance (not in the hot path)
- **Expected outcome:** ~30% reduction in peak memory during maintenance
- **Priority:** low
- **Complexity:** medium
- **Dependencies:** none

### OPT-3.2: Stream Import/Export for Large Graphs [DONE]

- **Description:** Rewrite import-memory.sh and export-memory.sh embedded Python to use streaming JSONL processing instead of loading all entries into a list.
- **Rationale:** MI-4/MI-5: Both scripts load the entire graph into memory. For near-cap (50MB) graphs, peak memory reaches 200MB+.
- **Affected files:** `import-memory.sh`, `export-memory.sh`
- **Implementation steps:**
  1. Export: stream entries directly from graph.jsonl to output JSON, writing the `entries` array incrementally
  2. Import: use two-pass approach — first pass builds entity name index, second pass merges and writes output
  3. Both: maintain atomic write pattern (temp file + fsync + os.replace)
- **Expected outcome:** Import/export peak memory reduced from O(graph_size × 3) to O(max_entity_size)
- **Priority:** low
- **Complexity:** medium
- **Dependencies:** none

---

## Phase 4 — I/O Efficiency

### OPT-4.1: Throttle Cooperative Index Reload Stat Check [DONE]

- **Description:** In `main()` loop, only check TF-IDF index mtime every 5 seconds instead of every request.
- **Rationale:** IO-4: Index changes at most 1x/day (during maintenance). Checking every request adds unnecessary stat() syscalls during rapid request bursts.
- **Affected files:** `semantic_server.py`
- **Implementation steps:**
  1. Add a module-level `_last_index_check = 0.0` variable
  2. In the main loop, wrap the index mtime check with `if time.monotonic() - _last_index_check > 5.0:`
  3. Update `_last_index_check` after the check
- **Expected outcome:** Eliminates stat() syscall on every request; reload latency increases by at most 5s (acceptable since index changes 1x/day)
- **Priority:** low
- **Complexity:** small
- **Dependencies:** none

### OPT-4.2: Cache Graph Summary in prime-memory.sh [DONE]

- **Description:** Store the graph summary output alongside the `.last-maintenance` marker. On session start, if maintenance didn't run (throttled), use the cached summary instead of re-parsing graph.jsonl.
- **Rationale:** IO-3: Every session start re-reads the entire graph to produce a 30-line summary, even when nothing has changed since the last session.
- **Affected files:** `hooks/prime-memory.sh`
- **Implementation steps:**
  1. After the Python summary script runs, write its output to `$MEMORY_DIR/.graph-summary.txt`
  2. On session start, if `.last-maintenance` is newer than `.graph-summary.txt`, regenerate; otherwise cat the cached file
  3. Fall back to live generation if cache file is missing
- **Expected outcome:** Session start avoids full graph parse when maintenance hasn't run (the common case)
- **Priority:** low
- **Complexity:** small
- **Dependencies:** none

---

## Phase 5 — Concurrency

### OPT-5.1: Skip Write on Lock Failure in capture_tool_context [DONE]

- **Description:** Same as OPT-1.2 — listed here for cross-reference. When the graph lock cannot be acquired within 250ms, skip the write entirely instead of writing without lock.
- **Rationale:** Concurrent unlocked writes risk JSONL corruption. The 30s throttle makes data loss from one skipped observation negligible.
- **Affected files:** `hooks/capture_tool_context.py`
- **Implementation steps:** See OPT-1.2
- **Expected outcome:** Eliminates data corruption risk under concurrent write contention
- **Priority:** high
- **Complexity:** small
- **Dependencies:** OPT-1.2 (same task)

### OPT-5.2: Explicit Memory Release Before TF-IDF Build [DONE]

- **Description:** In `maintenance.py:run()`, explicitly `del relations, others` before calling `build_tfidf_index()`. Move `gc.collect()` after these deletions.
- **Rationale:** Relations and others lists are not needed for index building but remain in scope, preventing garbage collection. For large graphs, this can hold 5–10MB of dead objects.
- **Affected files:** `maintenance.py`
- **Implementation steps:**
  1. After `write_jsonl()` (line 901-904), add `del others` (relations is still needed for recall pruning)
  2. After recall pruning block (line 948), add `del recall_counts`
  3. Move `gc.collect()` to just before `build_tfidf_index()` call
- **Expected outcome:** 5–10MB lower peak memory during maintenance for large graphs
- **Priority:** low
- **Complexity:** small
- **Dependencies:** none

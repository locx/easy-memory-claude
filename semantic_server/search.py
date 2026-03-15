"""TF-IDF cosine similarity search and time-based search."""
import heapq
import math
import time as _time
from collections import Counter

from .config import (
    MAIN_BRANCHES,
    MAX_CACHED_OBS,
    MAX_CANDIDATES,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    RE_WORDS,
    get_current_branch,
    log_event,
    normalize_iso_ts as _normalize_iso_ts,
    session_stats,
)
from .cache import entity_cache as _ec
from .graph import load_index, load_graph_entities
from .recall import (
    maybe_reload_recall_counts, record_recalls,
)


def _branch_boost(entity_branch, current_branch, sim):
    """Smooth branch relevance factor.

    Returns 1.0 for same-branch or unknown. For other
    branches, penalty increases as sim decreases —
    no hard cliff.
    """
    if (not entity_branch or not current_branch
            or entity_branch == current_branch):
        return 1.0
    # Base penalty: 0.05 for main, 0.20 for other
    if entity_branch in MAIN_BRANCHES:
        max_penalty = 0.05
    else:
        max_penalty = 0.20
    # Smooth: penalty scales with (1 - sim)
    penalty = max_penalty * (1.0 - min(sim, 1.0))
    return 1.0 - penalty


def _enrich_results(results, source,
                    max_obs=MAX_CACHED_OBS):
    """Attach entityType, observations, _branch."""
    for r in results:
        info = source.get(r["entity"], {})
        if info:
            r["entityType"] = info.get(
                "entityType", ""
            )
            obs = info.get("observations")
            r["observations"] = (
                obs[:max_obs]
                if isinstance(obs, list) else []
            )
            r["_branch"] = info.get("_branch", "")


def search(query, memory_dir, top_k=5, branch=None):
    """Search memory graph using TF-IDF cosine similarity.

    branch: override branch for boost (default:
    auto-detected current branch).
    """
    _t0 = _time.monotonic()
    if not isinstance(query, str):
        query = str(query) if query is not None else ""
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS]
    if (isinstance(top_k, bool)
            or not isinstance(top_k, int) or top_k < 1):
        top_k = 5
    top_k = min(top_k, MAX_TOP_K)

    current_branch = branch or get_current_branch()

    # Reload recall counts if changed externally
    maybe_reload_recall_counts()

    idx = load_index(memory_dir)
    if idx is None:
        return {
            "error": (
                "No TF-IDF index found. "
                "Index is built automatically by the "
                "SessionStart maintenance hook (1x/day). "
                "To force rebuild: python3 "
                "maintenance.py <project_dir>"
            ),
            "results": [],
            "total_indexed": 0,
        }

    vectors = idx.get("vectors", {})
    idf = idx.get("idf", {})
    magnitudes = idx.get("magnitudes", {})
    postings = idx.get("postings", {})
    metadata = idx.get("metadata", {})

    words = RE_WORDS.findall(query.lower())
    if not words:
        return {
            "error": "Empty query",
            "results": [],
            "total_indexed": len(vectors),
        }

    tf = Counter(words)
    total = len(words)
    query_vec = {}
    for w, count in tf.items():
        weight = (count / total) * idf.get(w, 0)
        if weight > 0:
            query_vec[w] = weight

    mag_q = math.sqrt(
        sum(v * v for v in query_vec.values())
    )
    if mag_q == 0:
        return {
            "results": [],
            "total_indexed": len(vectors),
            "note": "All query terms are too common "
                    "or not in index",
        }

    query_keys = query_vec.keys()

    if postings:
        sorted_terms = sorted(
            query_keys, key=lambda w: idf.get(w, 0),
            reverse=True,
        )
        p_lists = [
            set(postings[w]) for w in sorted_terms
            if w in postings
        ]
        if len(p_lists) >= 2:
            candidates = p_lists[0] & p_lists[1]
            if not candidates:
                for pl in p_lists:
                    candidates |= pl
                    if len(candidates) >= MAX_CANDIDATES:
                        break
        elif p_lists:
            candidates = p_lists[0]
        else:
            candidates = set()
    else:
        candidates = set(vectors.keys())

    # Heap: (adj_sim, raw_sim, boost, name)
    heap = []
    for name in candidates:
        vec = vectors.get(name)
        if not vec or not isinstance(vec, dict):
            continue
        dot = 0.0
        for k, qw in query_vec.items():
            vw = vec.get(k)
            if vw is not None:
                dot += qw * vw
        if dot == 0.0:
            continue
        mag_b = magnitudes.get(name)
        if mag_b is None:
            mag_b = math.sqrt(
                sum(v * v for v in vec.values())
            )
        if mag_b == 0:
            continue
        sim = dot / (mag_q * mag_b)
        if sim > 0.001 and math.isfinite(sim):
            # Branch boost — index metadata first,
            # then entity cache
            entity_branch = ""
            meta_entry = metadata.get(name)
            if meta_entry:
                entity_branch = meta_entry.get(
                    "_branch", ""
                )
            elif (_ec["data"]
                    and name in _ec["data"]):
                entity_branch = _ec["data"][name].get(
                    "_branch", ""
                )
            boost = _branch_boost(
                entity_branch, current_branch, sim
            )
            adj_sim = sim * boost
            if len(heap) < top_k:
                heapq.heappush(
                    heap,
                    (adj_sim, sim, boost, name),
                )
            elif adj_sim > heap[0][0]:
                heapq.heapreplace(
                    heap,
                    (adj_sim, sim, boost, name),
                )

    results = [
        {"entity": name,
         "score": round(adj, 4),
         "raw_score": round(raw, 4),
         "branch_boost": round(boost, 4)}
        for adj, raw, boost, name
        in sorted(heap, reverse=True)
    ]

    if results:
        # Prefer entity cache → index metadata → graph
        if _ec["data"] is not None:
            source = _ec["data"]
        elif isinstance(metadata, dict) and metadata:
            source = metadata
        else:
            source = load_graph_entities(memory_dir)
        _enrich_results(results, source, MAX_CACHED_OBS)

    if results:
        record_recalls([r["entity"] for r in results])

    session_stats["searches"] += 1
    _elapsed = int((_time.monotonic() - _t0) * 1000)
    log_event(
        "SEARCH",
        f'query="{query[:60]}" results='
        f'{len(results)} latency={_elapsed}ms',
    )
    return {
        "results": results,
        "total_indexed": len(vectors),
        "current_branch": current_branch,
    }


def search_by_time(memory_dir, since=None, until=None,
                   limit=20, branch_filter=None):
    """Return entities within a time window, sorted by
    recency. Optionally filter to a specific branch."""
    try:
        limit = min(max(int(limit), 1), MAX_TOP_K)
    except (ValueError, TypeError):
        limit = 20
    entities = load_graph_entities(memory_dir)

    # Normalize only the query bounds
    since_n = _normalize_iso_ts(since) if since else None
    until_n = _normalize_iso_ts(until) if until else None

    candidates = []
    for name, info in entities.items():
        ts = info.get("_updated") or info.get(
            "_created", ""
        )
        if not ts:
            continue
        if since_n and ts < since_n:
            continue
        if until_n and ts > until_n:
            continue
        if (branch_filter
                and info.get("_branch", "")
                != branch_filter):
            continue
        candidates.append((ts, name))

    total_matched = len(candidates)
    top = heapq.nlargest(limit, candidates)

    results = []
    for ts, name in top:
        info = entities.get(name, {})
        obs = info.get("observations")
        results.append({
            "entity": name,
            "entityType": info.get("entityType", ""),
            "updated": ts,
            "created": info.get("_created", ""),
            "_branch": info.get("_branch", ""),
            "observations": (
                obs[:MAX_CACHED_OBS]
                if isinstance(obs, list) else []
            ),
        })

    session_stats["searches"] += 1
    log_event(
        "TIME_SEARCH",
        f"since={since} until={until} "
        f"matched={total_matched}",
    )
    return {
        "results": results,
        "total_matched": total_matched,
    }

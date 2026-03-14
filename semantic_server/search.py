"""TF-IDF cosine similarity search and time-based search."""
import heapq
import math
import time as _time
from collections import Counter

from .config import (
    MAX_CANDIDATES,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    RE_WORDS,
    normalize_iso_ts as _normalize_iso_ts,
)
from .cache import entity_cache as _ec
from .graph import load_index, load_graph_entities
from .recall import maybe_reload_recall_counts, record_recalls
from .logging import log_event, session_stats


def _enrich_results(results, source, max_obs=5):
    """Attach entityType + observations from source dict."""
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


def search(query, memory_dir, top_k=5):
    """Search memory graph using TF-IDF cosine similarity."""
    _t0 = _time.monotonic()
    if not isinstance(query, str):
        query = str(query) if query is not None else ""
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS]
    if (isinstance(top_k, bool)
            or not isinstance(top_k, int) or top_k < 1):
        top_k = 5
    top_k = min(top_k, MAX_TOP_K)

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
                "~/.claude/memory/maintenance.py"
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
        # Sort query terms by IDF desc — rarest first
        sorted_terms = sorted(
            query_keys, key=lambda w: idf.get(w, 0),
            reverse=True,
        )
        if len(sorted_terms) >= 2:
            # Intersect two rarest posting lists first,
            # then expand if needed. Faster than Counter
            # over all terms for large indexes.
            p_lists = [
                set(postings[w]) for w in sorted_terms
                if w in postings
            ]
            if len(p_lists) >= 2:
                candidates = p_lists[0] & p_lists[1]
                # Expand with remaining terms if too few
                if not candidates:
                    candidates = p_lists[0] | p_lists[1]
                    for pl in p_lists[2:]:
                        candidates |= pl
                        if len(candidates) >= MAX_CANDIDATES:
                            break
            elif p_lists:
                candidates = p_lists[0]
            else:
                candidates = set()
        else:
            candidates = set()
            for w in sorted_terms:
                if w in postings:
                    candidates.update(postings[w])
                    if len(candidates) >= MAX_CANDIDATES:
                        break
    else:
        candidates = set(vectors.keys())

    heap = []
    for name in candidates:
        vec = vectors.get(name)
        if not vec or not isinstance(vec, dict):
            continue
        # Explicit loop over query terms — avoids creating
        # an intermediate set from query_keys & vec.keys()
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
            if len(heap) < top_k:
                heapq.heappush(heap, (sim, name))
            elif sim > heap[0][0]:
                heapq.heapreplace(heap, (sim, name))

    results = [
        {"entity": name, "score": round(sim, 4)}
        for sim, name in sorted(heap, reverse=True)
    ]

    if results:
        # Prefer entity cache → index metadata → graph load
        if _ec["data"] is not None:
            source = _ec["data"]
        elif isinstance(metadata, dict) and metadata:
            source = metadata
        else:
            source = load_graph_entities(memory_dir)
        _enrich_results(results, source)

    if results:
        record_recalls([r["entity"] for r in results])

    session_stats["searches"] += 1
    _elapsed = int((_time.monotonic() - _t0) * 1000)
    log_event(
        "SEARCH",
        f'query="{query[:60]}" results='
        f'{len(results)} latency={_elapsed}ms',
    )
    return {"results": results, "total_indexed": len(vectors)}


def search_by_time(memory_dir, since=None, until=None,
                   limit=20):
    """Return entities within a time window, sorted by
    recency. Uses heapq for O(n + k log n) instead of
    full sort. Two-pass: collect keys, then build dicts."""
    try:
        limit = min(max(int(limit), 1), MAX_TOP_K)
    except (ValueError, TypeError):
        limit = 20
    entities = load_graph_entities(memory_dir)

    # Normalize only the query bounds — entity timestamps
    # are pre-normalized during graph parse (_norm_ts).
    since_n = _normalize_iso_ts(since) if since else None
    until_n = _normalize_iso_ts(until) if until else None

    # Pass 1: collect (ts, name) tuples — timestamps
    # are already normalized from parse, skip per-entity call.
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
        candidates.append((ts, name))

    total_matched = len(candidates)

    # Pass 2: top-k via heapq (avoids full sort)
    top = heapq.nlargest(limit, candidates)

    # Pass 3: build result dicts only for winners
    results = []
    for ts, name in top:
        info = entities.get(name, {})
        obs = info.get("observations")
        results.append({
            "entity": name,
            "entityType": info.get("entityType", ""),
            "updated": ts,
            "created": info.get("_created", ""),
            "observations": (
                obs[:3] if isinstance(obs, list) else []
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

"""TF-IDF cosine similarity search and time-based search."""
import heapq
import math
from collections import Counter

from .config import (
    MAX_CANDIDATES,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    RE_WORDS,
)
from .graph import load_index, load_graph_entities
from .recall import maybe_reload_recall_counts, record_recalls


def _normalize_iso_ts(ts):
    """Normalize ISO timestamp to fixed-width for safe
    lexicographic comparison."""
    if not ts or not isinstance(ts, str):
        return ""
    if (len(ts) >= 10 and ts[4] == '-' and ts[7] == '-'
            and ts[:4].isdigit() and ts[5:7].isdigit()
            and ts[8:10].isdigit()):
        return ts
    try:
        parts = ts.split('T', 1)
        date_parts = parts[0].split('-')
        if len(date_parts) == 3:
            fixed = (
                f"{int(date_parts[0]):04d}-"
                f"{int(date_parts[1]):02d}-"
                f"{int(date_parts[2]):02d}"
            )
            if len(parts) > 1:
                return fixed + 'T' + parts[1]
            return fixed
    except (ValueError, IndexError):
        pass
    return ts


def search(query, memory_dir, top_k=5):
    """Search memory graph using TF-IDF cosine similarity."""
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
        return {"results": [], "total_indexed": len(vectors)}

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
        if metadata:
            for r in results:
                info = metadata.get(r["entity"], {})
                if info:
                    r["entityType"] = info.get(
                        "entityType", ""
                    )
                    r["observations"] = info.get(
                        "observations", []
                    )[:5]
        else:
            entities = load_graph_entities(memory_dir)
            for r in results:
                info = entities.get(r["entity"], {})
                if info:
                    r["entityType"] = info.get(
                        "entityType", ""
                    )
                    r["observations"] = info.get(
                        "observations", []
                    )[:5]

    if results:
        record_recalls([r["entity"] for r in results])

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

    since_n = _normalize_iso_ts(since) if since else None
    until_n = _normalize_iso_ts(until) if until else None

    # Pass 1: collect (ts_normalized, name, raw_ts) tuples
    candidates = []
    for name, info in entities.items():
        ts = info.get("_updated") or info.get(
            "_created", ""
        )
        if not ts:
            continue
        ts_n = _normalize_iso_ts(ts)
        if since_n and ts_n < since_n:
            continue
        if until_n and ts_n > until_n:
            continue
        candidates.append((ts_n, name, ts))

    total_matched = len(candidates)

    # Pass 2: top-k via heapq (avoids full sort)
    top = heapq.nlargest(limit, candidates)

    # Pass 3: build result dicts only for winners
    results = []
    for _ts_n, name, ts in top:
        info = entities.get(name, {})
        results.append({
            "entity": name,
            "entityType": info.get("entityType", ""),
            "updated": ts,
            "created": info.get("_created", ""),
            "observations": info.get(
                "observations", []
            )[:3],
        })

    return {
        "results": results,
        "total_matched": total_matched,
    }

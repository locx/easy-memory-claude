"""TF-IDF cosine similarity search and time-based search."""
import heapq
import math
import os as _os
import sys
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
    maybe_reload_recall_counts, record_recalls, recall_counts,
)

# Import search-quality enhancers from modular components
try:
    from .stem import stem_word as _stem
    from .text import (
        expand_synonyms as _expand_synonyms,
        make_bigrams as _make_bigrams,
        STOPWORDS as _STOPWORDS,
        filter_token as _filter_token,
        load_aliases,
        normalize_type,
    )
    _HAS_STEMMING = True
except ImportError as _import_err:
    raise ImportError(
        f"[memory] stem/text modules required but unavailable: {_import_err}"
    )

MIN_SIM_THRESHOLD = 0.001
_HEBBIAN_ALPHA = 0.1

# Alias cache: reload when aliases.json mtime changes
_alias_cache = {"map": None, "mtime": 0.0, "dir": ""}


def _get_alias_map(memory_dir):
    """Return alias-merged synonym map, cached by mtime."""
    aliases_path = _os.path.join(memory_dir, "aliases.json")
    try:
        mt = _os.path.getmtime(aliases_path)
    except OSError:
        mt = 0.0
    if (_alias_cache["map"] is not None
            and _alias_cache["mtime"] == mt
            and _alias_cache["dir"] == memory_dir):
        return _alias_cache["map"]
    merged = load_aliases(memory_dir)
    _alias_cache["map"] = merged
    _alias_cache["mtime"] = mt
    _alias_cache["dir"] = memory_dir
    return merged


def _tokenize_query(query_str, alias_map=None):
    """Tokenize query with stemming, synonyms, bigrams."""
    raw = [w for w in RE_WORDS.findall(query_str.lower()) if _filter_token(w)]
    if alias_map:
        expand = lambda w: alias_map.get(w, w)
    else:
        expand = _expand_synonyms
    # stem then expand — matches indexer order (maintenance.py:148)
    stemmed = [expand(_stem(w)) for w in raw]
    # Include bigrams for compound-term matching
    bigrams = _make_bigrams(stemmed)
    return stemmed + bigrams


def _branch_boost(entity_branch, current_branch, sim):
    """Smooth branch relevance factor (no hard cliff).

    Empty entity_branch is treated as 'main' (the default branch).
    """
    eff_entity = entity_branch if entity_branch else "main"
    eff_current = current_branch if current_branch else "main"
    if eff_entity == eff_current:
        return 1.0
    max_penalty = 0.05 if eff_entity in MAIN_BRANCHES else 0.20
    penalty = max_penalty * (1.0 - min(sim, 1.0))
    return 1.0 - penalty


def _enrich_results(results, source, max_obs=MAX_CACHED_OBS):
    """Attach entityType, observations, _branch."""
    for r in results:
        info = source.get(r["entity"], {})
        if info:
            obs = info.get("observations")
            r["entityType"] = normalize_type(info.get("entityType", ""))
            r["_branch"] = info.get("_branch", "")
            r["observations"] = obs[:max_obs] if isinstance(obs, list) else []


def _build_query_vector(query, idf, alias_map=None):
    words = _tokenize_query(query, alias_map)
    if not words:
        return None, 0.0
    tf = Counter(words)
    total = len(words)
    query_vec = {}
    for w, count in tf.items():
        weight = (count / total) * idf.get(w, 0)
        if weight > 0:
            query_vec[w] = weight
    mag_q = math.sqrt(sum(v * v for v in query_vec.values()))
    return query_vec, mag_q


def _get_candidates(query_keys, postings, vectors):
    if not postings:
        return set(vectors.keys())
    # Sort postings smallest-first and intersect incrementally
    # for early short-circuit on broad queries
    relevant = [postings[w] for w in query_keys if w in postings]
    relevant.sort(key=len)
    if not relevant:
        return set()
    if len(relevant) == 1:
        return set(relevant[0])
    candidates = set(relevant[0]) & set(relevant[1])
    if not candidates:
        # Fallback to union up to MAX_CANDIDATES
        for pl in relevant:
            candidates |= set(pl)
            if len(candidates) >= MAX_CANDIDATES:
                break
    return candidates


def _score_candidates(query_vec, mag_q, candidates, vectors, magnitudes, metadata, current_branch, top_k):
    heap = []
    for name in candidates:
        vec = vectors.get(name)
        if not vec or not isinstance(vec, dict):
            continue
        dot = sum(qw * vec.get(k, 0) for k, qw in query_vec.items() if k in vec)
        if dot == 0.0:
            continue
        mag_b = magnitudes.get(name)
        if mag_b is None:
            mag_b = math.sqrt(sum(v * v for v in vec.values()))
        if mag_b == 0:
            continue
        sim = dot / (mag_q * mag_b)
        if sim > MIN_SIM_THRESHOLD and math.isfinite(sim):
            entity_branch = metadata.get(name, {}).get("_branch", "")
            if not entity_branch and _ec["data"] and name in _ec["data"]:
                entity_branch = _ec["data"][name].get("_branch", "")
            boost = _branch_boost(entity_branch, current_branch, sim)
            rc = recall_counts.get(name, 0)
            adj_sim = sim * boost * (1.0 + _HEBBIAN_ALPHA * math.log1p(rc))
            if len(heap) < top_k:
                heapq.heappush(heap, (adj_sim, sim, boost, name))
            elif adj_sim > heap[0][0]:
                heapq.heapreplace(heap, (adj_sim, sim, boost, name))

    return [
        {"entity": name, "score": round(adj, 4), "raw_score": round(raw, 4), "branch_boost": round(boost, 4)}
        for adj, raw, boost, name in sorted(heap, reverse=True)
    ]


def _format_results(results, compact, metadata, memory_dir):
    if not results:
        return
    if not compact:
        if _ec["data"] is not None:
            source = _ec["data"]
        elif isinstance(metadata, dict) and metadata:
            source = metadata
        else:
            source = load_graph_entities(memory_dir)
        _enrich_results(results, source, min(5, MAX_CACHED_OBS))
    else:
        for r in results:
            r["entityType"] = normalize_type(
                metadata.get(r["entity"], {}).get("entityType", "")
            )


def search(query, memory_dir, top_k=5, branch=None, compact=False):
    """Search memory graph using TF-IDF cosine similarity."""
    _t0 = _time.monotonic()
    query = str(query)[:MAX_QUERY_CHARS] if query else ""
    try:
        top_k = min(max(int(top_k), 1), MAX_TOP_K)
    except (ValueError, TypeError):
        top_k = 5
    current_branch = branch or get_current_branch()

    maybe_reload_recall_counts()

    idx = load_index(memory_dir)
    if idx is None:
        return {"error": "No TF-IDF index found. Ensure SessionStart ran.", "results": [], "total_indexed": 0}

    vectors = idx.get("vectors", {})
    idf = idx.get("idf", {})

    # Load project-specific aliases for query expansion
    alias_map = _get_alias_map(memory_dir)

    query_vec, mag_q = _build_query_vector(query, idf, alias_map or None)
    if not query_vec:
        return {"error": "Empty query", "results": [], "total_indexed": len(vectors)}
    if mag_q == 0:
        return {"results": [], "total_indexed": len(vectors), "note": "Terms too common or unindexed"}

    candidates = _get_candidates(query_vec.keys(), idx.get("postings", {}), vectors)
    results = _score_candidates(
        query_vec, mag_q, candidates, vectors, idx.get("magnitudes", {}),
        idx.get("metadata", {}), current_branch, top_k
    )

    _format_results(results, compact, idx.get("metadata", {}), memory_dir)

    if results:
        record_recalls([r["entity"] for r in results])

    session_stats["searches"] += 1
    log_event("SEARCH", f'query="{query[:60]}" results={len(results)} latency={int((_time.monotonic() - _t0)*1000)}ms')

    return {"results": results, "total_indexed": len(vectors), "current_branch": current_branch}


def search_by_time(memory_dir, since=None, until=None, limit=20, branch_filter=None, entity_type=None):
    """Return entities within a time window, sorted by recency."""
    try: limit = min(max(int(limit), 1), MAX_TOP_K)
    except (ValueError, TypeError): limit = 20
    entities = load_graph_entities(memory_dir)

    since_n = _normalize_iso_ts(since) if since else None
    until_n = _normalize_iso_ts(until) if until else None

    target_type = normalize_type(entity_type) if entity_type else None

    candidates = []
    for name, info in entities.items():
        ts = info.get("_updated") or info.get("_created", "")
        if not ts: continue
        if since_n and ts < since_n: continue
        if until_n and ts > until_n: continue
        if branch_filter and info.get("_branch", "") != branch_filter: continue
        if target_type and normalize_type(info.get("entityType", "")) != target_type: continue
        candidates.append((ts, name))

    total_matched = len(candidates)
    top = heapq.nlargest(limit, candidates)

    results = []
    for ts, name in top:
        info = entities.get(name, {})
        obs = info.get("observations")
        results.append({
            "entity": name,
            "entityType": normalize_type(info.get("entityType", "")),
            "updated": ts,
            "created": info.get("_created", ""),
            "_branch": info.get("_branch", ""),
            "observations": obs[:min(5, MAX_CACHED_OBS)] if isinstance(obs, list) else [],
        })

    session_stats["searches"] += 1
    log_event("TIME_SEARCH", f"since={since} until={until} matched={total_matched}")
    return {"results": results, "total_matched": total_matched}

#!/usr/bin/env python3
"""Minimal MCP server for semantic memory search.

Pure Python — zero external dependencies.
Uses TF-IDF cosine similarity over the knowledge graph.
Communicates via JSON-RPC 2.0 over stdio (MCP stdio transport).

Usage:
    MEMORY_DIR=/path/to/.memory python3 semantic_server.py
"""
import heapq
import json
import math
import os
import re
import signal
import sys
from collections import Counter

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memory-semantic-search"
SERVER_VERSION = "1.6.0"

MAX_INPUT_CHARS = 10_000_000  # 10 MB — reject oversized lines
MAX_TOP_K = 100               # cap result count
MAX_QUERY_CHARS = 10_000      # cap query length
RECALL_FLUSH_INTERVAL = 10    # flush recall counts every N searches

# Pre-compiled regex
_RE_WORDS = re.compile(r'\w+')

# --- Module-level caches ---
_index_cache = {
    "data": None, "mtime": 0.0, "path": "",
}
_entity_cache = {
    "data": None, "mtime": 0.0, "path": "",
}
_relation_cache = {
    "data": None, "mtime": 0.0, "path": "",
}
# P2-1: cached adjacency (invalidated with relation cache)
_adjacency_cache = {
    "outbound": None, "inbound": None, "mtime": 0.0,
}


# --- Recall tracking (Hebbian reinforcement) ---
_recall_counts = {}        # entity_name -> count (in-memory)
_recall_search_count = 0   # searches since last flush
_recall_path = ""          # set on first search
_recall_mtime = 0.0        # P1-2: track file mtime


def _load_recall_counts(memory_dir):
    """Load recall counts from sidecar file."""
    global _recall_counts, _recall_path, _recall_mtime
    _recall_path = os.path.join(
        memory_dir, "recall_counts.json"
    )
    try:
        _recall_mtime = os.path.getmtime(_recall_path)
        with open(_recall_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _recall_counts = {
                k: v for k, v in data.items()
                if isinstance(k, str)
                and isinstance(v, (int, float))
            }
    except (OSError, json.JSONDecodeError, ValueError):
        _recall_counts = {}
        _recall_mtime = 0.0


def _maybe_reload_recall_counts():
    """P1-2: Reload recall counts if file changed on disk."""
    global _recall_mtime
    if not _recall_path:
        return
    try:
        mtime = os.path.getmtime(_recall_path)
    except OSError:
        return
    if mtime == _recall_mtime:
        return
    _recall_mtime = mtime
    try:
        with open(_recall_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                cur = _recall_counts.get(k, 0)
                if v > cur:
                    _recall_counts[k] = v
    except (OSError, json.JSONDecodeError, ValueError):
        pass


def _record_recalls(entity_names):
    """Increment recall counts and flush periodically."""
    global _recall_search_count
    for name in entity_names:
        _recall_counts[name] = (
            _recall_counts.get(name, 0) + 1
        )
    _recall_search_count += 1
    if _recall_search_count >= RECALL_FLUSH_INTERVAL:
        _flush_recall_counts()


def _flush_recall_counts():
    """Atomic write of recall counts to disk."""
    global _recall_search_count, _recall_mtime
    if not _recall_path or not _recall_counts:
        return
    _recall_search_count = 0
    tmp = _recall_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                _recall_counts, f, separators=(",", ":")
            )
        os.replace(tmp, _recall_path)
        try:
            _recall_mtime = os.path.getmtime(_recall_path)
        except OSError:
            pass
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _clear_index_cache():
    _index_cache.update(data=None, mtime=0.0, path="")


def _clear_entity_cache():
    _entity_cache.update(data=None, mtime=0.0, path="")


def _clear_relation_cache():
    _relation_cache.update(
        data=None, mtime=0.0, path=""
    )
    _adjacency_cache.update(
        outbound=None, inbound=None, mtime=0.0
    )


def load_index(memory_dir):
    """Load TF-IDF index with mtime-based caching."""
    index_path = os.path.join(memory_dir, "tfidf_index.json")

    try:
        mtime = os.path.getmtime(index_path)
    except OSError:
        _clear_index_cache()
        return None

    if (_index_cache["data"] is not None
            and _index_cache["path"] == index_path
            and _index_cache["mtime"] == mtime):
        return _index_cache["data"]

    try:
        with open(index_path, encoding="utf-8") as f:
            data = json.load(f)
        _index_cache["data"] = data
        _index_cache["mtime"] = mtime
        _index_cache["path"] = index_path
        return data
    except (json.JSONDecodeError, OSError):
        _clear_index_cache()
        return None


def _get_graph_mtime(memory_dir):
    """Get graph.jsonl mtime, or None if missing."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    try:
        return graph_path, os.path.getmtime(graph_path)
    except OSError:
        return graph_path, None


def _parse_graph_file(graph_path):
    """Parse graph.jsonl into (entities_dict, relations_list).
    P3-9: Skips entities with empty name.
    """
    entities = {}
    relations = []
    try:
        with open(graph_path, encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if len(line) > MAX_INPUT_CHARS:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "entity":
                        name = obj.get("name", "")
                        if not name:
                            continue
                        entities[name] = {
                            "entityType": obj.get(
                                "entityType", ""
                            ),
                            "observations": obj.get(
                                "observations", []
                            ),
                            "_created": obj.get(
                                "_created", ""
                            ),
                            "_updated": obj.get(
                                "_updated", ""
                            ),
                        }
                    elif t == "relation":
                        r_from = obj.get("from", "")
                        r_to = obj.get("to", "")
                        if not r_from or not r_to:
                            continue
                        relations.append({
                            "from": r_from,
                            "to": r_to,
                            "relationType": obj.get(
                                "relationType", ""
                            ),
                        })
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None, None
    return entities, relations


def load_graph_entities(memory_dir):
    """Load entity details with mtime-based caching."""
    graph_path, mtime = _get_graph_mtime(memory_dir)
    if mtime is None:
        _clear_entity_cache()
        _clear_relation_cache()
        return {}

    if (_entity_cache["data"] is not None
            and _entity_cache["path"] == graph_path
            and _entity_cache["mtime"] == mtime):
        return _entity_cache["data"]

    entities, relations = _parse_graph_file(graph_path)
    if entities is None:
        _clear_entity_cache()
        _clear_relation_cache()
        return {}

    _entity_cache["data"] = entities
    _entity_cache["mtime"] = mtime
    _entity_cache["path"] = graph_path
    _relation_cache["data"] = relations
    _relation_cache["mtime"] = mtime
    _relation_cache["path"] = graph_path
    return entities


def load_graph_relations(memory_dir):
    """Load relations with mtime-based caching."""
    graph_path, mtime = _get_graph_mtime(memory_dir)
    if mtime is None:
        _clear_entity_cache()
        _clear_relation_cache()
        return []

    if (_relation_cache["data"] is not None
            and _relation_cache["path"] == graph_path
            and _relation_cache["mtime"] == mtime):
        return _relation_cache["data"]

    entities, relations = _parse_graph_file(graph_path)
    if relations is None:
        _clear_entity_cache()
        _clear_relation_cache()
        return []

    _entity_cache["data"] = entities
    _entity_cache["mtime"] = mtime
    _entity_cache["path"] = graph_path
    _relation_cache["data"] = relations
    _relation_cache["mtime"] = mtime
    _relation_cache["path"] = graph_path
    return relations


def _get_adjacency(memory_dir):
    """P2-1: Build or return cached adjacency dicts."""
    relations = load_graph_relations(memory_dir)
    mtime = _relation_cache.get("mtime", 0.0)

    if (_adjacency_cache["outbound"] is not None
            and _adjacency_cache["mtime"] == mtime):
        return (
            _adjacency_cache["outbound"],
            _adjacency_cache["inbound"],
        )

    outbound = {}
    inbound = {}
    for r in relations:
        fr, to = r["from"], r["to"]
        rt = r["relationType"]
        outbound.setdefault(fr, []).append((to, rt))
        inbound.setdefault(to, []).append((fr, rt))

    _adjacency_cache["outbound"] = outbound
    _adjacency_cache["inbound"] = inbound
    _adjacency_cache["mtime"] = mtime
    return outbound, inbound


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

    # P1-2: reload recall counts if changed externally
    _maybe_reload_recall_counts()

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

    words = _RE_WORDS.findall(query.lower())
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
        candidates = set()
        for w in query_keys:
            if w in postings:
                candidates.update(postings[w])
    else:
        candidates = set(vectors.keys())

    heap = []
    for name in candidates:
        vec = vectors.get(name)
        if not vec or not isinstance(vec, dict):
            continue
        common = query_keys & vec.keys()
        if not common:
            continue
        dot = sum(query_vec[k] * vec[k] for k in common)
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
        _record_recalls([r["entity"] for r in results])

    return {"results": results, "total_indexed": len(vectors)}


def traverse_relations(entity, memory_dir, direction="both",
                       max_depth=2):
    """BFS traversal over relations from a start entity."""
    try:
        max_depth = min(max(int(max_depth), 1), 5)
    except (ValueError, TypeError):
        max_depth = 2
    if direction not in ("outbound", "inbound", "both"):
        direction = "both"

    entities = load_graph_entities(memory_dir)
    # P2-1: use cached adjacency
    outbound, inbound = _get_adjacency(memory_dir)

    if entity not in entities:
        return {"error": f"Entity '{entity}' not found",
                "nodes": [], "edges": []}

    visited = {entity}
    frontier = [entity]
    seen_edges = set()
    edges = []

    for _depth in range(max_depth):
        next_frontier = []
        for node in frontier:
            neighbors = []
            if direction in ("outbound", "both"):
                for to, rt in outbound.get(node, []):
                    neighbors.append((node, to, rt))
            if direction in ("inbound", "both"):
                for fr, rt in inbound.get(node, []):
                    neighbors.append((fr, node, rt))
            for fr, to, rt in neighbors:
                edge_key = (fr, to, rt)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                target = to if fr == node else fr
                edges.append({
                    "from": fr, "to": to,
                    "relationType": rt,
                })
                if target not in visited:
                    visited.add(target)
                    next_frontier.append(target)
        frontier = next_frontier
        if not frontier:
            break

    nodes = []
    for name in visited:
        info = entities.get(name, {})
        nodes.append({
            "name": name,
            "entityType": info.get("entityType", ""),
            "observations": info.get(
                "observations", []
            )[:3],
        })

    return {"nodes": nodes, "edges": edges}


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


# --- MCP Protocol ---

TOOLS = [
    {
        "name": "semantic_search_memory",
        "description": (
            "Search the memory knowledge graph using "
            "TF-IDF semantic similarity. Returns entities "
            "ranked by relevance to the query, with their "
            "type and top observations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language search query"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": (
                        "Number of results to return "
                        "(default 5)"
                    ),
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "traverse_relations",
        "description": (
            "Traverse the memory knowledge graph from a "
            "start entity, following relations up to "
            "max_depth hops. Returns connected subgraph "
            "with nodes and edges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Start entity name",
                },
                "direction": {
                    "type": "string",
                    "enum": [
                        "outbound", "inbound", "both"
                    ],
                    "description": (
                        "Traversal direction (default both)"
                    ),
                    "default": "both",
                },
                "max_depth": {
                    "type": "integer",
                    "description": (
                        "Max hops to traverse (1-5, "
                        "default 2)"
                    ),
                    "default": 2,
                },
            },
            "required": ["entity"],
        },
    },
    {
        "name": "search_memory_by_time",
        "description": (
            "Search memory entities by time range. "
            "Returns entities updated/created within the "
            "window, sorted by most recent first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "ISO date start (e.g. "
                        "2026-03-01T00:00:00Z)"
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "ISO date end (e.g. "
                        "2026-03-13T23:59:59Z)"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max results (default 20)"
                    ),
                    "default": 20,
                },
            },
        },
    },
]


def handle_message(msg, memory_dir):
    """Handle a single JSON-RPC 2.0 message.
    P3-6: Wraps tool calls in try/except.
    """
    if not isinstance(msg, dict):
        return None

    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        load_index(memory_dir)
        _load_recall_counts(memory_dir)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        # P3-6: guard against unexpected exceptions
        try:
            if tool_name == "semantic_search_memory":
                result = search(
                    args.get("query", ""),
                    memory_dir,
                    args.get("top_k", 5),
                )
            elif tool_name == "traverse_relations":
                result = traverse_relations(
                    args.get("entity", ""),
                    memory_dir,
                    args.get("direction", "both"),
                    args.get("max_depth", 2),
                )
            elif tool_name == "search_memory_by_time":
                result = search_by_time(
                    memory_dir,
                    args.get("since"),
                    args.get("until"),
                    args.get("limit", 20),
                )
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": (
                            f"Unknown tool: {tool_name}"
                        ),
                    },
                }
        except Exception as exc:
            sys.stderr.write(
                f"error: {tool_name}: {exc}\n"
            )
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "error": str(exc),
                            "results": [],
                        }),
                    }],
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result),
                    }
                ]
            },
        }

    if method.startswith("notifications/"):
        return None

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }
    return None


def _shutdown_handler(signum, frame):
    """Graceful SIGTERM shutdown."""
    _flush_recall_counts()
    try:
        sys.stdout.flush()
    except OSError:
        pass
    try:
        sys.stderr.write("semantic_server: shutting down\n")
        sys.stderr.flush()
    except OSError:
        pass
    os._exit(0)


def main():
    """Run MCP server on stdio."""
    signal.signal(signal.SIGTERM, _shutdown_handler)
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    memory_dir = os.environ.get(
        "MEMORY_DIR",
        os.path.join(os.getcwd(), ".memory"),
    )

    if not os.path.isdir(memory_dir):
        sys.stderr.write(
            f"{SERVER_NAME}: warning: MEMORY_DIR "
            f"'{memory_dir}' does not exist\n"
        )
        sys.stderr.flush()

    sys.stderr.write(
        f"{SERVER_NAME} v{SERVER_VERSION} "
        f"ready (memory_dir={memory_dir})\n"
    )
    sys.stderr.flush()

    try:
        while True:
            try:
                line = sys.stdin.readline()
            except (EOFError, UnicodeDecodeError):
                break
            if not line:
                break

            if len(line) > MAX_INPUT_CHARS:
                sys.stderr.write(
                    "warn: oversized input dropped "
                    f"({len(line)} chars)\n"
                )
                continue

            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                sys.stderr.write(
                    f"warn: malformed input: "
                    f"{line[:100]}\n"
                )
                continue

            response = handle_message(msg, memory_dir)
            if response is not None:
                try:
                    sys.stdout.write(
                        json.dumps(response) + "\n"
                    )
                    sys.stdout.flush()
                except BrokenPipeError:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        _flush_recall_counts()


if __name__ == "__main__":
    main()

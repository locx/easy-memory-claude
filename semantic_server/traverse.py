"""BFS relation traversal over the knowledge graph."""
from .cache import (
    adjacency_cache,
    estimate_size,
    relation_cache,
)
from .graph import (
    get_graph_mtime,
    load_graph_entities,
    load_graph_relations,
)

_MAX_VISITED = 10_000


def _get_adjacency(memory_dir):
    """Build or return cached adjacency dicts."""
    relations = load_graph_relations(memory_dir)
    mtime = relation_cache.get("mtime", 0.0)

    if (adjacency_cache["outbound"] is not None
            and adjacency_cache["mtime"] == mtime):
        return (
            adjacency_cache["outbound"],
            adjacency_cache["inbound"],
        )

    outbound = {}
    inbound = {}
    for r in relations:
        fr = r.get("from", "")
        to = r.get("to", "")
        rt = r.get("relationType", "")
        if not fr or not to:
            continue
        outbound.setdefault(fr, []).append((to, rt))
        inbound.setdefault(to, []).append((fr, rt))

    adjacency_cache["outbound"] = outbound
    adjacency_cache["inbound"] = inbound
    adjacency_cache["mtime"] = mtime
    adjacency_cache["size"] = (
        estimate_size(outbound) + estimate_size(inbound)
    )
    return outbound, inbound


def _expand_frontier(frontier, direction, outbound, inbound, visited, seen_edges, edges):
    next_frontier = []
    capped = False
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
            edges.append({"from": fr, "to": to, "relationType": rt})
            if target not in visited:
                if len(visited) >= _MAX_VISITED:
                    capped = True
                    continue
                visited.add(target)
                next_frontier.append(target)
    return next_frontier, capped


def traverse_relations(entity, memory_dir,
                       direction="both",
                       max_depth=2):
    """BFS traversal with visited-set cap."""
    try:
        max_depth = min(max(int(max_depth), 1), 5)
    except (ValueError, TypeError):
        max_depth = 2
    if direction not in (
        "outbound", "inbound", "both"
    ):
        direction = "both"

    _, mtime = get_graph_mtime(memory_dir)
    if mtime is None:
        return {
            "error": "Graph file not found",
            "nodes": [],
            "edges": [],
        }

    entities = load_graph_entities(memory_dir)
    if not entities:
        return {
            "error": "Graph is empty or unreadable",
            "nodes": [],
            "edges": [],
        }

    outbound, inbound = _get_adjacency(memory_dir)

    if entity not in entities:
        return {
            "error": f"Entity '{entity}' not found",
            "nodes": [],
            "edges": [],
        }

    visited = {entity}
    frontier = [entity]
    seen_edges = set()
    edges = []
    capped = False

    for _depth in range(max_depth):
        frontier, capped = _expand_frontier(frontier, direction, outbound, inbound, visited, seen_edges, edges)
        if not frontier or capped:
            break

    nodes = []
    for name in visited:
        info = entities.get(name, {})
        nodes.append({
            "name": name,
            "entityType": info.get("entityType", ""),
            "observations": info.get("observations", [])[:3],
        })

    result = {"nodes": nodes, "edges": edges}
    if capped:
        result["truncated"] = True
        result["max_visited"] = _MAX_VISITED
    return result

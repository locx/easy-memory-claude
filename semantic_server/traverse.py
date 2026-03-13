"""BFS relation traversal over the knowledge graph."""
from .cache import (
    adjacency_cache,
    estimate_size,
    maybe_evict_caches,
)
from .graph import load_graph_entities, load_graph_relations


def _get_adjacency(memory_dir):
    """Build or return cached adjacency dicts."""
    relations = load_graph_relations(memory_dir)
    from .cache import relation_cache
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
        fr, to = r["from"], r["to"]
        rt = r["relationType"]
        outbound.setdefault(fr, []).append((to, rt))
        inbound.setdefault(to, []).append((fr, rt))

    adjacency_cache["outbound"] = outbound
    adjacency_cache["inbound"] = inbound
    adjacency_cache["mtime"] = mtime
    # Track adjacency size for eviction
    adjacency_cache["size"] = (
        estimate_size(outbound) + estimate_size(inbound)
    )
    maybe_evict_caches()
    return outbound, inbound


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

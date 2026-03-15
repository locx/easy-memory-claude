"""Cache infrastructure: mtime-based caches with size-aware eviction."""

from .config import MAX_CACHE_BYTES

# --- Module-level caches ---
index_cache = {
    "data": None, "mtime": 0.0, "path": "", "size": 0,
}
entity_cache = {
    "data": None, "mtime": 0.0, "path": "", "size": 0,
    "offset": 0, "append_only": False,
}
relation_cache = {
    "data": None, "mtime": 0.0, "path": "", "size": 0,
}
# Cached adjacency lists (invalidated with relation cache)
adjacency_cache = {
    "outbound": None, "inbound": None,
    "mtime": 0.0, "size": 0,
}

# Throttle cooperative index reload stat check
last_index_check = 0.0


def clear_index_cache():
    index_cache.update(
        data=None, mtime=0.0, path="", size=0
    )


def clear_entity_cache():
    entity_cache.update(
        data=None, mtime=0.0, path="", size=0,
        offset=0, append_only=False,
    )


def clear_relation_cache():
    relation_cache.update(
        data=None, mtime=0.0, path="", size=0
    )
    adjacency_cache.update(
        outbound=None, inbound=None, mtime=0.0, size=0,
    )


def estimate_size(obj):
    """Estimate byte size for eviction decisions.

    Uses entry count * avg overhead. Avoids json.dumps
    which creates a transient copy of the full dataset.
    """
    if obj is None:
        return 0
    if isinstance(obj, dict):
        return len(obj) * 500
    if isinstance(obj, list):
        return len(obj) * 200
    return 64


def maybe_evict_caches():
    """Evict caches in priority order until under cap.

    Priority: index (largest, rebuilt by maint) ->
    adjacency -> entity -> relation.
    """
    total = (
        index_cache["size"]
        + entity_cache["size"]
        + relation_cache["size"]
        + adjacency_cache["size"]
    )
    if total <= MAX_CACHE_BYTES:
        return
    for cache, clear_fn in (
        (index_cache, clear_index_cache),
        (adjacency_cache,
         lambda: adjacency_cache.update(
             outbound=None, inbound=None,
             mtime=0.0, size=0)),
        (entity_cache, clear_entity_cache),
        (relation_cache, clear_relation_cache),
    ):
        if cache["size"] > 0:
            clear_fn()
            total = (
                index_cache["size"]
                + entity_cache["size"]
                + relation_cache["size"]
                + adjacency_cache["size"]
            )
            if total <= MAX_CACHE_BYTES:
                return

"""Cache infrastructure: mtime-based caches with size-aware eviction."""

from .config import MAX_CACHE_BYTES

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
adjacency_cache = {
    "outbound": None, "inbound": None,
    "mtime": 0.0, "size": 0,
}

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
    """Estimate byte size via entry count * avg overhead."""
    if obj is None:
        return 0
    if isinstance(obj, dict):
        return len(obj) * 2000
    if isinstance(obj, list):
        return len(obj) * 200
    return 64


def _cache_total():
    return (index_cache["size"] + entity_cache["size"]
            + relation_cache["size"] + adjacency_cache["size"])


def maybe_evict_caches():
    """Evict caches in priority order until under cap."""
    if _cache_total() <= MAX_CACHE_BYTES:
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
            if _cache_total() <= MAX_CACHE_BYTES:
                return

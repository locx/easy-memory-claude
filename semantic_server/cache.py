"""Cache infrastructure: mtime-based caches with size-aware eviction."""

import sys
from sys import getsizeof

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

_DEPTH_CAP = 3
_STRING_OVERHEAD = sys.getsizeof("")


def clear_index_cache():
    index_cache.update(
        data=None, mtime=0.0, path="", size=0
    )


def clear_entity_cache():
    entity_cache.update(
        data=None, mtime=0.0, path="", size=0,
        offset=0, append_only=False,
    )
    entity_cache.pop("_pre_invalidate_mtime", None)


def clear_relation_cache():
    relation_cache.update(
        data=None, mtime=0.0, path="", size=0
    )
    adjacency_cache.update(
        outbound=None, inbound=None, mtime=0.0, size=0,
    )


def estimate_size(obj, _depth=0):
    """Estimate byte size via shallow walk of strings/containers.

    Traverses up to _DEPTH_CAP levels deep to price string payloads
    accurately. Past the cap, falls back to sys.getsizeof for speed.
    """
    if obj is None:
        return 0
    if _depth >= _DEPTH_CAP:
        return getsizeof(obj)
    if isinstance(obj, str):
        return _STRING_OVERHEAD + len(obj)
    if isinstance(obj, (int, float, bool)):
        return getsizeof(obj)
    if isinstance(obj, dict):
        total = getsizeof(obj)
        for k, v in obj.items():
            total += estimate_size(k, _depth + 1)
            total += estimate_size(v, _depth + 1)
        return total
    if isinstance(obj, (list, tuple, set, frozenset)):
        total = getsizeof(obj)
        for item in obj:
            total += estimate_size(item, _depth + 1)
        return total
    return getsizeof(obj)


def _cache_total():
    return (index_cache["size"] + entity_cache["size"]
            + relation_cache["size"] + adjacency_cache["size"])


def maybe_evict_caches():
    """Evict caches by size (largest first) until under cap."""
    if _cache_total() <= MAX_CACHE_BYTES:
        return
    evictable = [
        (index_cache, clear_index_cache),
        (adjacency_cache,
         lambda: adjacency_cache.update(
             outbound=None, inbound=None,
             mtime=0.0, size=0)),
        (entity_cache, clear_entity_cache),
        (relation_cache, clear_relation_cache),
    ]
    evictable.sort(key=lambda x: x[0]["size"], reverse=True)
    for cache, clear_fn in evictable:
        if cache["size"] > 0:
            clear_fn()
            if _cache_total() <= MAX_CACHE_BYTES:
                return

"""Cache infrastructure: mtime-based caches with size-aware eviction."""
import sys

from .config import MAX_CACHE_BYTES

# --- Module-level caches ---
index_cache = {
    "data": None, "mtime": 0.0, "path": "", "size": 0,
}
entity_cache = {
    "data": None, "mtime": 0.0, "path": "", "size": 0,
}
relation_cache = {
    "data": None, "mtime": 0.0, "path": "", "size": 0,
}
# Cached adjacency lists (invalidated with relation cache)
# Size tracked for eviction policy
adjacency_cache = {
    "outbound": None, "inbound": None, "mtime": 0.0,
    "size": 0,
}

# Throttle cooperative index reload stat check
last_index_check = 0.0


def clear_index_cache():
    index_cache.update(
        data=None, mtime=0.0, path="", size=0
    )


def clear_entity_cache():
    entity_cache.update(
        data=None, mtime=0.0, path="", size=0
    )


def clear_relation_cache():
    relation_cache.update(
        data=None, mtime=0.0, path="", size=0
    )
    adjacency_cache.update(
        outbound=None, inbound=None, mtime=0.0, size=0
    )


def estimate_size(obj):
    """Sampling-based byte-size estimate.

    Samples up to 50 entries and extrapolates. Accounts for
    Python object overhead (~2-3x raw data).
    """
    if obj is None:
        return 0
    if isinstance(obj, dict):
        n = len(obj)
        if n == 0:
            return sys.getsizeof(obj)
        sample_n = min(n, 50)
        it = iter(obj.items())
        total_sample = 0
        for _ in range(sample_n):
            k, v = next(it)
            total_sample += sys.getsizeof(k)
            if isinstance(v, dict):
                total_sample += sys.getsizeof(v)
                for vv in v.values():
                    if isinstance(vv, (str, int, float)):
                        total_sample += sys.getsizeof(vv)
                    elif isinstance(vv, list):
                        total_sample += sys.getsizeof(vv)
                        total_sample += sum(
                            sys.getsizeof(el)
                            for el in vv[:5]
                        )
            elif isinstance(v, list):
                total_sample += sys.getsizeof(v)
                for el in v[:5]:
                    if isinstance(el, dict):
                        total_sample += sys.getsizeof(el)
                        total_sample += sum(
                            sys.getsizeof(dv)
                            for dv in el.values()
                        )
                    else:
                        total_sample += sys.getsizeof(el)
            else:
                total_sample += sys.getsizeof(v)
        avg = total_sample / sample_n
        return int(avg * n) + sys.getsizeof(obj)
    if isinstance(obj, list):
        n = len(obj)
        if n == 0:
            return sys.getsizeof(obj)
        sample_n = min(n, 50)
        total_sample = sum(
            sys.getsizeof(obj[i]) for i in range(sample_n)
        )
        avg = total_sample / sample_n
        return int(avg * n) + sys.getsizeof(obj)
    return sys.getsizeof(obj)


def maybe_evict_caches():
    """Evict largest cache if combined size exceeds cap.

    Includes adjacency cache in size tracking.
    """
    total = (
        index_cache["size"]
        + entity_cache["size"]
        + relation_cache["size"]
        + adjacency_cache["size"]
    )
    if total <= MAX_CACHE_BYTES:
        return
    # Evict index cache first (largest, rebuilt by maint)
    if index_cache["size"] > 0:
        clear_index_cache()
        return
    # Then adjacency (rebuilt on next traverse)
    if adjacency_cache["size"] > 0:
        adjacency_cache.update(
            outbound=None, inbound=None,
            mtime=0.0, size=0,
        )
        return
    # Then entity cache
    if entity_cache["size"] > 0:
        clear_entity_cache()

"""Graph I/O: JSONL parsing, loading, locking, appending, rewriting.

Handles all disk interaction with graph.jsonl including:
- Mtime-based cache loading for entities and relations
- Incremental reads via byte-offset tracking
- Exclusive file locking for write safety
- Atomic writes (temp file + fsync + os.replace)
"""
import json
import os
import sys
import time

from ._json import loads as _fast_loads
from ._json import dumps as _fast_dumps

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows — locking disabled

from .config import (
    MAX_ENTITY_COUNT,
    MAX_GRAPH_BYTES,
    MAX_INPUT_CHARS,
    MAX_CACHED_OBS,
    GRAPH_LOCK_TIMEOUT,
    PARSE_TIME_BUDGET,
    normalize_iso_ts as _norm_ts,
)
from .cache import (
    index_cache,
    entity_cache,
    relation_cache,
    adjacency_cache,
    clear_index_cache,
    clear_entity_cache,
    clear_relation_cache,
    estimate_size,
    maybe_evict_caches,
)


def _obs_dedup_key(obs):
    """Normalize an observation to a hashable dedup key."""
    if isinstance(obs, str):
        return obs
    return json.dumps(obs, sort_keys=True)


def get_graph_mtime(memory_dir):
    """Get graph.jsonl mtime, or None if missing."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    try:
        return graph_path, os.path.getmtime(graph_path)
    except OSError:
        return graph_path, None


def _parse_graph_file(graph_path, start_offset=0):
    """Parse graph.jsonl into (entities_dict, relations_list).

    Merges duplicate entity names (common from append-only
    writes by capture-tool-context hook). Observations are
    deduplicated, earliest _created and latest _updated kept.
    Skips entities with empty name.

    Observations are truncated to MAX_CACHED_OBS during
    parse (not after) to reduce peak memory.

    Supports start_offset for incremental reads. Aborts
    after PARSE_TIME_BUDGET seconds.

    Returns (entities, relations, end_offset).
    """
    entities = {}
    relations = []
    deadline = time.monotonic() + PARSE_TIME_BUDGET
    line_count = 0
    end_offset = start_offset
    # Byte guard for incremental reads — cap at 2x
    # typical append batch to catch corruption/runaway.
    max_incr_bytes = MAX_GRAPH_BYTES if start_offset == 0 \
        else min(MAX_GRAPH_BYTES, 10_000_000)
    try:
        # Binary mode for correct byte-offset tracking
        # (text-mode seek is undefined for non-tell values)
        with open(graph_path, "rb") as f:
            if start_offset > 0:
                f.seek(start_offset)
            for raw in f:
                end_offset = f.tell()
                # Byte budget guard for incremental reads
                if (end_offset - start_offset
                        > max_incr_bytes):
                    sys.stderr.write(
                        "warn: incremental read byte "
                        "budget exceeded\n"
                    )
                    break
                line = raw.decode("utf-8", errors="replace")
                if len(line) > MAX_INPUT_CHARS:
                    continue
                line = line.strip()
                if not line:
                    continue
                # Time budget check every 1000 lines
                line_count += 1
                if line_count % 1000 == 0:
                    if time.monotonic() > deadline:
                        sys.stderr.write(
                            "warn: parse time budget "
                            f"exceeded after {line_count}"
                            " lines\n"
                        )
                        break
                try:
                    obj = _fast_loads(line)
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "entity":
                        name = obj.get("name", "")
                        if isinstance(name, str):
                            name = name.strip()
                        if not name:
                            continue
                        # Cap entity count to bound memory
                        if (name not in entities
                                and len(entities)
                                >= MAX_ENTITY_COUNT):
                            continue
                        obs = obj.get("observations", [])
                        if not isinstance(obs, list):
                            obs = []
                        if name in entities:
                            prev = entities[name]
                            prev_obs = prev.get(
                                "observations", []
                            )
                            seen = prev.get("_obs_keys")
                            if seen is None:
                                seen = set()
                                for o in prev_obs:
                                    seen.add(
                                        _obs_dedup_key(o)
                                    )
                                prev["_obs_keys"] = seen
                            for o in obs:
                                k = _obs_dedup_key(o)
                                if k not in seen:
                                    prev_obs.append(o)
                                    seen.add(k)
                            # Truncate without rebuilding
                            # the full set — keep last N
                            # obs, rebuild only their keys.
                            if len(prev_obs) > MAX_CACHED_OBS:
                                kept = prev_obs[
                                    -MAX_CACHED_OBS:
                                ]
                                prev["observations"] = kept
                                # Rebuild only for kept
                                # (N=3, so O(1) work)
                                prev["_obs_keys"] = {
                                    _obs_dedup_key(o)
                                    for o in kept
                                }
                            new_c = _norm_ts(
                                obj.get("_created", "")
                            )
                            if new_c and (
                                not prev["_created"]
                                or new_c < prev["_created"]
                            ):
                                prev["_created"] = new_c
                            new_u = _norm_ts(
                                obj.get("_updated", "")
                            )
                            if new_u and (
                                not prev["_updated"]
                                or new_u > prev["_updated"]
                            ):
                                prev["_updated"] = new_u
                            branch = obj.get("_branch")
                            if branch:
                                prev["_branch"] = branch
                        else:
                            # Truncate immediately on insert
                            obs_list = list(obs)
                            if len(obs_list) > MAX_CACHED_OBS:
                                obs_list = obs_list[
                                    -MAX_CACHED_OBS:
                                ]
                            info = {
                                "entityType": obj.get(
                                    "entityType", ""
                                ),
                                "observations": obs_list,
                                "_created": _norm_ts(
                                    obj.get("_created", "")
                                ),
                                "_updated": _norm_ts(
                                    obj.get("_updated", "")
                                ),
                            }
                            branch = obj.get("_branch")
                            if branch:
                                info["_branch"] = branch
                            entities[name] = info
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
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return None, None, 0
    # Strip internal dedup keys (obs already truncated)
    for info in entities.values():
        info.pop("_obs_keys", None)
    return entities, relations, end_offset


def _do_full_parse(graph_path, mtime):
    """Full graph parse — populates both entity+relation caches.

    Returns (entities, relations) or ({}, []) on failure.
    """
    entities, relations, offset = _parse_graph_file(graph_path)
    if entities is None:
        clear_entity_cache()
        clear_relation_cache()
        return {}, []

    entity_cache["data"] = entities
    entity_cache["mtime"] = mtime
    entity_cache["path"] = graph_path
    entity_cache["size"] = estimate_size(entities)
    entity_cache["offset"] = offset
    entity_cache["append_only"] = False
    relation_cache["data"] = relations
    relation_cache["mtime"] = mtime
    relation_cache["path"] = graph_path
    relation_cache["size"] = estimate_size(relations)
    maybe_evict_caches()
    return entities, relations


def load_index(memory_dir):
    """Load TF-IDF index with mtime-based caching."""
    index_path = os.path.join(memory_dir, "tfidf_index.json")

    try:
        mtime = os.path.getmtime(index_path)
    except OSError:
        clear_index_cache()
        return None

    if (index_cache["data"] is not None
            and index_cache["path"] == index_path
            and index_cache["mtime"] == mtime):
        return index_cache["data"]

    try:
        from ._json import load as _fast_load
        with open(index_path, encoding="utf-8") as f:
            data = _fast_load(f)
        size = estimate_size(data.get("vectors", {}))
        index_cache["data"] = data
        index_cache["mtime"] = mtime
        index_cache["path"] = index_path
        index_cache["size"] = size
        maybe_evict_caches()
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        clear_index_cache()
        return None


def load_graph_entities(memory_dir):
    """Load entity details with mtime-based caching.

    Supports incremental reads — when only appends have
    occurred since last full parse, reads only new bytes
    from the tracked file offset.
    """
    graph_path, mtime = get_graph_mtime(memory_dir)
    if mtime is None:
        clear_entity_cache()
        clear_relation_cache()
        return {}

    if (entity_cache["data"] is not None
            and entity_cache["path"] == graph_path
            and entity_cache["mtime"] == mtime):
        return entity_cache["data"]

    # Incremental read if append-only flag is set
    prev_offset = entity_cache.get("offset", 0)
    if (entity_cache.get("append_only")
            and entity_cache["data"] is not None
            and entity_cache["path"] == graph_path
            and prev_offset > 0):
        new_ents, new_rels, offset = _parse_graph_file(
            graph_path, start_offset=prev_offset
        )
        if new_ents is None:
            # Incremental read failed — force full parse
            entity_cache["append_only"] = False
        else:
            # Merge new into existing cache
            existing = entity_cache["data"]
            for name, info in new_ents.items():
                if name in existing:
                    # Merge observations
                    prev = existing[name]
                    seen = {
                        _obs_dedup_key(o)
                        for o in prev["observations"]
                    }
                    for o in info.get("observations", []):
                        k = _obs_dedup_key(o)
                        if k not in seen:
                            prev["observations"].append(o)
                            seen.add(k)
                    if len(prev["observations"]) > \
                            MAX_CACHED_OBS:
                        prev["observations"] = \
                            prev["observations"][
                                -MAX_CACHED_OBS:]
                    new_u = info.get("_updated", "")
                    if new_u and (
                        not prev["_updated"]
                        or new_u > prev["_updated"]
                    ):
                        prev["_updated"] = new_u
                else:
                    existing[name] = info
            # Merge new relations
            if relation_cache["data"] is not None:
                relation_cache["data"].extend(new_rels)
                # Incremental size: add delta, not re-scan
                relation_cache["size"] += (
                    estimate_size(new_rels)
                    if new_rels else 0
                )
            entity_cache["mtime"] = mtime
            entity_cache["offset"] = offset
            # Incremental size: add delta for new entities
            entity_cache["size"] += (
                estimate_size(new_ents)
                if new_ents else 0
            )
            entity_cache["append_only"] = False
            # Invalidate adjacency (relations changed)
            adjacency_cache.update(
                outbound=None, inbound=None,
                mtime=0.0, size=0,
            )
            maybe_evict_caches()
            return existing

    entities, _ = _do_full_parse(graph_path, mtime)
    return entities


def load_graph_relations(memory_dir):
    """Load relations with mtime-based caching."""
    graph_path, mtime = get_graph_mtime(memory_dir)
    if mtime is None:
        clear_entity_cache()
        clear_relation_cache()
        return []

    if (relation_cache["data"] is not None
            and relation_cache["path"] == graph_path
            and relation_cache["mtime"] == mtime):
        return relation_cache["data"]

    _, relations = _do_full_parse(graph_path, mtime)
    return relations


# --- Write infrastructure ---


class GraphLock:
    """Context manager for exclusive graph file locking.

    Uses non-blocking flock with retry loop and timeout
    (GRAPH_LOCK_TIMEOUT) instead of indefinite block.
    Callers can check self.acquired to handle lock failure.
    """
    __slots__ = ("_fd", "_path", "acquired")

    def __init__(self, memory_dir):
        self._path = os.path.join(
            memory_dir, ".graph.lock"
        )
        self._fd = None
        self.acquired = False

    def __enter__(self):
        if fcntl is None:
            self.acquired = True
            return self
        try:
            self._fd = open(self._path, "a")
        except OSError:
            return self
        try:
            # Exponential backoff with monotonic deadline
            # to avoid float accumulation drift.
            delay = 0.01
            deadline = time.monotonic() + GRAPH_LOCK_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(
                        self._fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    self.acquired = True
                    return self
                except (IOError, OSError):
                    time.sleep(delay)
                    delay = min(delay * 2, 0.5)
            # Timeout — lock not acquired
            sys.stderr.write(
                "warn: graph lock timeout after "
                f"{GRAPH_LOCK_TIMEOUT}s\n"
            )
        except Exception:
            # Ensure fd cleanup on unexpected exception
            self._fd.close()
            self._fd = None
            raise
        # Timeout path — close fd, no lock acquired
        self._fd.close()
        self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                if self.acquired:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            self._fd.close()
            self._fd = None
            self.acquired = False
        return False


def invalidate_caches():
    """Invalidate all in-process caches after a write."""
    clear_entity_cache()
    clear_relation_cache()
    clear_index_cache()


def invalidate_entity_cache_only():
    """Mark entity cache for incremental reload.

    Sets append_only flag so next load_graph_entities() can
    do an incremental read from the tracked offset instead
    of a full reparse.
    """
    if entity_cache["data"] is not None:
        entity_cache["append_only"] = True
        entity_cache["mtime"] = 0.0  # force reload
    else:
        clear_entity_cache()


def invalidate_relation_cache_only():
    """Invalidate relation + adjacency caches only."""
    clear_relation_cache()


def check_graph_size(memory_dir):
    """Guard against writes to oversized graphs."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    try:
        size = os.path.getsize(graph_path)
        if size > MAX_GRAPH_BYTES:
            return {
                "error": f"Graph too large ({size} bytes, "
                         f"max {MAX_GRAPH_BYTES}). Run "
                         f"maintenance to prune first."
            }
    except OSError:
        pass
    return None


def append_jsonl(memory_dir, entries, do_fsync=True):
    """Append JSONL lines directly — O(1) for new entries.

    Uses file append mode instead of copying the entire graph.
    Falls back to creating the file if it doesn't exist.
    Locked to prevent concurrent writes from hooks/maintenance.

    Returns False on lock timeout instead of writing without
    lock.
    """
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    with GraphLock(memory_dir) as lock:
        if not lock.acquired:
            return False
        # Pre-serialize outside file write to catch
        # serialization errors before touching the file.
        # Avoids partial writes from generator failures.
        lines = []
        for e in entries:
            try:
                lines.append(_fast_dumps(e) + "\n")
            except (TypeError, ValueError, OverflowError):
                continue  # skip unserializable entries
        if not lines:
            return True
        with open(graph_path, "a", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            if do_fsync:
                os.fsync(f.fileno())
    return True


def rewrite_graph(memory_dir, entities_dict, relations):
    """Rewrite entire graph from entities dict + relations.

    entities_dict: {name: {entityType, observations, ...}}
    relations: list of relation dicts
    Preserves all metadata fields (_branch, _created, etc).
    Locked + atomic (temp file + fsync + os.replace).
    Uses writelines() with a generator for batched I/O.
    """
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    tmp = graph_path + ".new"

    def _lines():
        for name, info in entities_dict.items():
            if not name or not isinstance(name, str):
                continue
            entry = {"type": "entity", "name": name}
            entry.update(info)
            try:
                yield _fast_dumps(entry) + "\n"
            except (TypeError, ValueError, OverflowError):
                continue
        for r in relations:
            if not r.get("from") or not r.get("to"):
                continue
            entry = {"type": "relation"}
            entry.update(r)
            try:
                yield _fast_dumps(entry) + "\n"
            except (TypeError, ValueError, OverflowError):
                continue

    # Write temp file OUTSIDE lock — reduces lock hold
    # time from seconds to milliseconds (just the rename).
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(_lines())
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # Lock only for the atomic rename
    with GraphLock(memory_dir) as lock:
        if not lock.acquired:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise OSError("Graph lock timeout")
        try:
            os.replace(tmp, graph_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

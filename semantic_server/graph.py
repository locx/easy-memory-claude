"""Graph I/O: JSONL parsing, loading, locking, appending, rewriting."""
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
from .text import normalize_type


def _obs_dedup_key(obs):
    """Normalize an observation to a hashable dedup key."""
    if isinstance(obs, str):
        return ("s", obs)
    return ("d", json.dumps(obs, sort_keys=True))


def _merge_obs(prev_obs, new_obs, seen=None):
    """Merge new_obs into prev_obs with dedup + truncate.

    Returns the updated seen-set; caller may cache for reuse.
    """
    if seen is None:
        seen = {_obs_dedup_key(o) for o in prev_obs}
    for o in new_obs:
        k = _obs_dedup_key(o)
        if k not in seen:
            prev_obs.append(o)
            seen.add(k)
    if len(prev_obs) > MAX_CACHED_OBS:
        prev_obs[:] = prev_obs[-MAX_CACHED_OBS:]
        seen = {_obs_dedup_key(o) for o in prev_obs}
    return seen


def _merge_ts(prev, created, updated):
    """Keep earliest _created, latest _updated."""
    if created and (
        not prev.get("_created")
        or created < prev["_created"]
    ):
        prev["_created"] = created
    if updated and (
        not prev.get("_updated")
        or updated > prev["_updated"]
    ):
        prev["_updated"] = updated


def get_graph_mtime(memory_dir):
    """Get graph.jsonl (mtime, size) tuple, or None if missing."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    try:
        st = os.stat(graph_path)
        return graph_path, (st.st_mtime, st.st_size)
    except OSError:
        return graph_path, None


_MAX_RAW_LINE_BYTES = MAX_INPUT_CHARS * 4


def _iter_graph_lines(f, start_offset, max_incr_bytes, deadline):
    line_count = 0
    for raw in f:
        end_offset = f.tell()
        if (end_offset - start_offset > max_incr_bytes):
            sys.stderr.write("warn: incremental read byte budget exceeded\n")
            break
        if len(raw) > _MAX_RAW_LINE_BYTES:
            continue
        line = raw.decode("utf-8", errors="replace")
        if "\ufffd" in line:
            sys.stderr.write(
                f"warn: skipped invalid UTF-8 at offset {f.tell()}\n"
            )
            continue
        line = line.strip()
        if not line:
            continue
        line_count += 1
        if line_count % 1000 == 0 and time.monotonic() > deadline:
            sys.stderr.write(f"warn: parse time budget exceeded after {line_count} lines\n")
            break
        yield line, end_offset


def _handle_entity_entry(entities, obs_keys, obj):
    """Parse one entity JSONL entry.

    obs_keys is a sidecar {name: set(dedup_keys)} used for fast
    dedup-merge across appends. Kept out of the entity dict so the
    data model stays clean.
    """
    name = obj.get("name", "")
    if isinstance(name, str):
        name = name.strip()
    if not name:
        return
    if name not in entities and len(entities) >= MAX_ENTITY_COUNT:
        return
    obs = obj.get("observations", [])
    if not isinstance(obs, list):
        obs = []
    if name in entities:
        prev = entities[name]
        obs_keys[name] = _merge_obs(
            prev.get("observations", []),
            obs,
            obs_keys.get(name),
        )
        _merge_ts(
            prev,
            _norm_ts(obj.get("_created", "")),
            _norm_ts(obj.get("_updated", "")),
        )
        branch = obj.get("_branch")
        if branch and not prev.get("_branch"):
            prev["_branch"] = branch
    else:
        obs_list = list(obs)
        if len(obs_list) > MAX_CACHED_OBS:
            obs_list = obs_list[-MAX_CACHED_OBS:]
        info = {
            "entityType": normalize_type(obj.get("entityType", "")),
            "observations": obs_list,
            "_created": _norm_ts(obj.get("_created", "")),
            "_updated": _norm_ts(obj.get("_updated", "")),
        }
        branch = obj.get("_branch")
        if branch:
            info["_branch"] = branch
        entities[name] = info
        obs_keys[name] = {_obs_dedup_key(o) for o in obs_list}


def _handle_relation_entry(relations, rel_seen, obj):
    r_from = obj.get("from", "")
    r_to = obj.get("to", "")
    if not isinstance(r_from, str) or not isinstance(r_to, str):
        return
    if not r_from or not r_to:
        return
    r_type = normalize_type(obj.get("relationType", ""))
    rel_key = (r_from, r_to, r_type)
    if rel_key not in rel_seen:
        rel_seen.add(rel_key)
        relations.append({
            "from": r_from,
            "to": r_to,
            "relationType": r_type,
        })

def _parse_graph_file(graph_path, start_offset=0, seed_obs_keys=None):
    """Parse graph.jsonl into (entities_dict, relations_list, obs_keys, end_offset).

    seed_obs_keys lets the incremental path reuse a prior dedup set,
    avoiding O(M) rebuilds per entity.
    """
    entities = {}
    relations = []
    rel_seen = set()
    obs_keys = dict(seed_obs_keys) if seed_obs_keys else {}
    deadline = time.monotonic() + PARSE_TIME_BUDGET
    end_offset = start_offset
    max_incr_bytes = MAX_GRAPH_BYTES if start_offset == 0 \
        else min(MAX_GRAPH_BYTES, 10_000_000)
    try:
        with open(graph_path, "rb") as f:
            if start_offset > 0:
                f.seek(start_offset)
            for line, offset in _iter_graph_lines(f, start_offset, max_incr_bytes, deadline):
                end_offset = offset
                try:
                    obj = _fast_loads(line)
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "entity":
                        _handle_entity_entry(entities, obs_keys, obj)
                    elif t == "relation":
                        _handle_relation_entry(relations, rel_seen, obj)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return None, None, None, 0
    # Trim obs_keys to entities actually present
    for name in list(obs_keys):
        if name not in entities:
            obs_keys.pop(name, None)
    return entities, relations, obs_keys, end_offset


def _do_full_parse(graph_path, mtime):
    """Full graph parse — populates both caches."""
    entities, relations, obs_keys, offset = _parse_graph_file(graph_path)
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
    entity_cache["obs_keys"] = obs_keys
    entity_cache.pop("_pre_invalidate_mtime", None)
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


def _merge_incremental_data(existing_ents, existing_obs_keys,
                            new_ents, new_obs_keys, new_rels):
    """Merge incremental parse results into cache, reusing dedup sets."""
    for name, info in new_ents.items():
        if name in existing_ents:
            prev = existing_ents[name]
            seen = existing_obs_keys.get(name)
            existing_obs_keys[name] = _merge_obs(
                prev["observations"],
                info.get("observations", []),
                seen,
            )
            _merge_ts(prev, info.get("_created", ""), info.get("_updated", ""))
        else:
            existing_ents[name] = info
            # Reuse obs_keys produced by parser when available
            existing_obs_keys[name] = (
                new_obs_keys.get(name)
                or {_obs_dedup_key(o) for o in info.get("observations", [])}
            )

    if relation_cache["data"] is not None and new_rels:
        existing_keys = {
            (r["from"], r["to"], r.get("relationType", ""))
            for r in relation_cache["data"]
        }
        added = [
            r for r in new_rels
            if (r["from"], r["to"], r.get("relationType", "")) not in existing_keys
        ]
        if added:
            relation_cache["data"].extend(added)
            relation_cache["size"] += estimate_size(added)


def _try_incremental_load(graph_path, mtime, prev_offset):
    """Attempt incremental load from prev_offset. Returns entities or None on failure."""
    # Size regression guard: if file shrank, it was rewritten — force full load
    current_size = mtime[1] if isinstance(mtime, tuple) else None
    if current_size is not None and current_size < prev_offset:
        return None

    if mtime == entity_cache.get("_pre_invalidate_mtime", 0.0):
        entity_cache["append_only"] = False
        return None

    existing_obs_keys = entity_cache.get("obs_keys") or {}
    new_ents, new_rels, new_obs_keys, offset = _parse_graph_file(
        graph_path, start_offset=prev_offset,
    )
    if new_ents is None:
        entity_cache["append_only"] = False
        entity_cache.pop("_pre_invalidate_mtime", None)
        return None

    existing = entity_cache["data"]
    _merge_incremental_data(
        existing, existing_obs_keys,
        new_ents, new_obs_keys or {}, new_rels,
    )
    entity_cache["mtime"] = mtime
    entity_cache["offset"] = offset
    entity_cache["size"] = estimate_size(existing)
    entity_cache["append_only"] = False
    entity_cache["obs_keys"] = existing_obs_keys
    entity_cache.pop("_pre_invalidate_mtime", None)
    if relation_cache["data"] is not None:
        relation_cache["mtime"] = mtime
    if new_rels:
        adjacency_cache.update(outbound=None, inbound=None, mtime=0.0)
    maybe_evict_caches()
    return existing


def _full_load(graph_path, mtime):
    """Full parse path."""
    entities, _ = _do_full_parse(graph_path, mtime)
    return entities


def load_graph_entities(memory_dir):
    """Load entities with mtime cache + incremental reads."""
    graph_path, mtime = get_graph_mtime(memory_dir)
    if mtime is None:
        clear_entity_cache()
        clear_relation_cache()
        return {}

    if (entity_cache["data"] is not None
            and entity_cache["path"] == graph_path
            and entity_cache["mtime"] == mtime):
        return entity_cache["data"]

    prev_offset = entity_cache.get("offset", 0)
    if (entity_cache.get("append_only")
            and entity_cache["data"] is not None
            and entity_cache["path"] == graph_path
            and prev_offset > 0):
        result = _try_incremental_load(graph_path, mtime, prev_offset)
        if result is not None:
            return result

    return _full_load(graph_path, mtime)


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
    """Exclusive graph file lock with timeout."""
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
            sys.stderr.write(
                "warn: graph lock timeout after "
                f"{GRAPH_LOCK_TIMEOUT}s\n"
            )
        except Exception:
            self._fd.close()
            self._fd = None
            raise
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
    """Mark entity cache for incremental reload."""
    if entity_cache["data"] is not None:
        # Record mtime before invalidation for coherence guard
        entity_cache["_pre_invalidate_mtime"] = \
            entity_cache.get("mtime", 0.0)
        entity_cache["append_only"] = True
        entity_cache["mtime"] = 0.0
    else:
        clear_entity_cache()


def invalidate_relation_cache_only():
    """Invalidate relation cache (adjacency handled separately)."""
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
    """Append JSONL lines under lock; fsync after lock release."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    lines = []
    with GraphLock(memory_dir) as lock:
        if not lock.acquired:
            return False
        for e in entries:
            try:
                lines.append(_fast_dumps(e) + "\n")
            except (TypeError, ValueError, OverflowError):
                continue
        if not lines:
            return True
        with open(graph_path, "a", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
    if do_fsync and lines:
        try:
            with open(graph_path, "a", encoding="utf-8") as f:
                os.fsync(f.fileno())
        except OSError:
            pass
    return True


def _build_rewrite_line(entry_type, name_or_r, info_or_none):
    """Build one JSONL line for rewrite_graph."""
    if entry_type == "entity":
        if not name_or_r or not isinstance(name_or_r, str):
            return None
        entry = {"type": "entity", "name": name_or_r}
        entry.update(info_or_none)
    else:
        if not info_or_none.get("from") or not info_or_none.get("to"):
            return None
        entry = {"type": "relation"}
        entry.update(info_or_none)
    try:
        return _fast_dumps(entry) + "\n"
    except (TypeError, ValueError, OverflowError):
        return None


def rewrite_graph(memory_dir, entities_dict, relations):
    """Atomic rewrite: temp file + fsync + os.replace."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    tmp = graph_path + ".new"
    dropped = 0

    def _lines():
        nonlocal dropped
        for name, info in entities_dict.items():
            line = _build_rewrite_line("entity", name, info)
            if line is None:
                dropped += 1
                continue
            yield line
        for r in relations:
            line = _build_rewrite_line("relation", None, r)
            if line is None:
                dropped += 1
                continue
            yield line

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

    if dropped > 0:
        sys.stderr.write(f"warn: rewrite_graph dropped {dropped} invalid lines\n")

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
        invalidate_caches()

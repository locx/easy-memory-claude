#!/usr/bin/env python3
"""Memory graph maintenance: decay, prune, consolidate, TF-IDF index.

Pure Python — zero external dependencies. Throttled to 1x/day.
Usage: python3 maintenance.py [project_dir]
"""
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows — locking disabled
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path

# Fast JSON backend
try:
    from semantic_server._json import loads as _loads, dumps as _dumps, dump as _dump
except ImportError:
    import json as _json
    _loads = _json.loads
    def _dumps(obj, **kw):
        sep = kw.get("separators", (",", ":"))
        return _json.dumps(obj, separators=sep)
    def _dump(obj, f, **kw):
        sep = kw.get("separators", (",", ":"))
        _json.dump(obj, f, separators=sep)

from semantic_server.io_utils import iter_jsonl, partition_graph, write_jsonl, merge_pending
from semantic_server.stem import stem_word as _stem
from semantic_server.text import (
    STOPWORDS as _STOPWORDS,
    SYNONYM_MAP as _SYNONYM_MAP,
    normalize_name,
    make_bigrams as _make_bigrams,
    filter_token as _filter_token,
    expand_synonyms as _expand_synonyms,
    load_aliases,
)
from semantic_server.maintenance_utils import (
    prune_entities,
    consolidate,
    stamp_metadata,
    read_recall_counts,
    parse_iso_date,
)

# Configuration defaults (overridable via .memory/config.json)
_DEFAULTS = {
    "DECAY_THRESHOLD": 0.1,
    "MAX_AGE_DAYS": 90,
    "THROTTLE_HOURS": 24,
    "MIN_MERGE_NAME_LEN": 4,
    "MAX_LOG_BYTES": 100_000,
}

_cfg = dict(_DEFAULTS)


def _valid(cfg, key, types, lo, hi):
    """Return cfg[key] if valid type + in bounds, else None."""
    v = cfg.get(key)
    if (v is not None and isinstance(v, types)
            and not isinstance(v, bool)
            and lo <= v <= hi):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    return None


def _load_config(memory_dir):
    """Load per-project config with inline validation."""
    cfg_path = os.path.join(memory_dir, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(cfg, dict):
        return
    overrides = {}
    for json_key, cfg_key, types, lo, hi in (
        ("decay_threshold", "DECAY_THRESHOLD",
         (int, float), 0.0, 10.0),
        ("max_age_days", "MAX_AGE_DAYS",
         int, 1, 3650),
        ("throttle_hours", "THROTTLE_HOURS",
         (int, float), 0.1, 720),
        ("min_merge_name_len", "MIN_MERGE_NAME_LEN",
         int, 1, 100),
        ("max_log_bytes", "MAX_LOG_BYTES",
         int, 1000, 100_000_000),
    ):
        v = _valid(cfg, json_key, types, lo, hi)
        if v is not None:
            overrides[cfg_key] = v
    _cfg.update(overrides)


# Pre-compiled regexes (Unicode-aware)
_RE_WORDS = re.compile(r'\w+', re.UNICODE)


def get_branch(cwd=None):
    """Get current git branch, or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _tokenize_docs(entities, alias_map):
    docs = {}
    meta = {}
    df = Counter()
    def _expand(w):
        return alias_map.get(w, w)

    for ent in entities:
        name = ent.get("name", "")
        obs = ent.get("observations", [])
        obs_strs = []
        for o in obs:
            if isinstance(o, str):
                obs_strs.append(o)
            else:
                obs_strs.append(str(o))
        etype = ent.get("entityType", "")
        words = []
        for piece in chain((name, etype), obs_strs):
            raw = [w for w in _RE_WORDS.findall(piece.lower()) if _filter_token(w)]
            stemmed = [_expand(_stem(w)) for w in raw]
            words.extend(stemmed)
            words.extend(_make_bigrams(stemmed))
        if words:
            docs[name] = words
            meta[name] = {
                "entityType": etype,
                "observations": obs_strs[:5],
                "_branch": ent.get("_branch", ""),
            }
            for w in set(words):
                df[w] += 1
    return docs, meta, df

def build_tfidf_index(entities, memory_dir):
    """Build TF-IDF index with magnitudes, postings, metadata.

    Two-pass: tokenize + DF, then TF-IDF vectors. Filters
    stopwords and singleton terms (DF < 2 when corpus > 50).
    Uses stemming, bigrams, and synonym expansion.
    """
    if not entities:
        # Remove stale index so search doesn't return
        # results for pruned/nonexistent entities
        _stale = os.path.join(memory_dir, "tfidf_index.json")
        try:
            os.unlink(_stale)
        except OSError:
            pass
        return 0

    # Load project-specific aliases for synonym expansion
    alias_map = load_aliases(memory_dir)
    docs, meta, df = _tokenize_docs(entities, alias_map)

    if not docs:
        _stale = os.path.join(memory_dir, "tfidf_index.json")
        try:
            os.unlink(_stale)
        except OSError:
            pass
        return 0

    n_docs = len(docs)
    min_df = 2 if n_docs > 50 else 1
    # BM25-style IDF: boosts rare terms, floor at 0.1
    idf = {
        w: max(0.1, math.log(
            (n_docs - count + 0.5) / (count + 0.5) + 1
        ))
        for w, count in df.items()
        if count >= min_df
    }

    # Pass 2: TF-IDF vectors + magnitudes + postings
    vectors = {}
    magnitudes = {}
    postings = defaultdict(list)

    for name, words in docs.items():
        tf = Counter(words)
        total = len(words)
        vec = {}
        for w, count in tf.items():
            score = (count / total) * idf.get(w, 0)
            if score > 0.001:
                vec[w] = round(score, 4)
                postings[w].append(name)
        if not vec:
            continue
        vectors[name] = vec
        mag = math.sqrt(sum(v * v for v in vec.values()))
        magnitudes[name] = round(mag, 4)

    del docs, df

    meta = {k: v for k, v in meta.items() if k in vectors}
    n_indexed = len(vectors)

    index_path = os.path.join(memory_dir, "tfidf_index.json")
    tmp = index_path + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # Stream sections to reduce peak memory
            f.write('{"vectors":')
            _dump(vectors, f)
            del vectors
            f.write(',"idf":')
            _dump(
                {k: round(v, 4) for k, v in idf.items()},
                f,
            )
            del idf
            f.write(',"magnitudes":')
            _dump(magnitudes, f)
            del magnitudes
            f.write(',"postings":')
            _dump(dict(postings), f)
            del postings
            f.write(',"metadata":')
            _dump(meta, f)
            del meta
            f.write(',"doc_count":')
            f.write(str(n_indexed))
            f.write(',"built":"')
            f.write(time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ))
            f.write('"}')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, index_path)
        # Clear dirty marker if present
        dirty = os.path.join(memory_dir, ".index-dirty")
        try:
            os.unlink(dirty)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return n_indexed


def log_pruned(memory_dir, pruned_count, merged_count):
    """Append to pruned.log, rotate if oversized."""
    log_path = os.path.join(memory_dir, "pruned.log")
    try:
        if os.path.getsize(log_path) > _cfg["MAX_LOG_BYTES"]:
            os.replace(log_path, log_path + ".old")
    except OSError:
        pass
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                f"{ts}  pruned={pruned_count}  "
                f"merged={merged_count}\n"
            )
    except OSError:
        pass


def _print_graph_stats(entities, n_relations, pruned,
                       merged, memory_dir):
    """Print graph health statistics."""
    if not entities:
        return
    n = len(entities)
    type_counts = Counter(
        e.get("entityType", "unknown") for e in entities
    )
    avg_obs = sum(
        len(e.get("observations", []))
        for e in entities
    ) / n
    dates = [
        e.get("_created") or e.get("_updated", "")
        for e in entities
    ]
    dates = [d for d in dates if d]
    idx_kb = 0
    try:
        idx_kb = os.path.getsize(
            os.path.join(memory_dir, "tfidf_index.json")
        ) // 1024
    except OSError:
        pass
    print(f"=== Graph: {n} entities, "
          f"{n_relations} relations ===")
    for t, c in type_counts.most_common(10):
        print(f"  {t}: {c}")
    print(f"  avg obs/entity: {avg_obs:.1f}")
    if dates:
        print(f"  oldest: {min(dates)}  "
              f"newest: {max(dates)}")
    branch_counts = Counter(
        e.get("_branch", "unknown") for e in entities
    )
    for b, c in branch_counts.most_common(5):
        print(f"  branch {b}: {c}")
    print(f"  pruned={pruned} merged={merged} "
          f"index={idx_kb}KB")


# P20: proper no-op context manager when fcntl unavailable
@contextmanager
def _noop_lock():
    yield


def _acquire_lock(memory_dir):
    """Acquire exclusive flock with 10s timeout. Returns fd or None."""
    if fcntl is None:
        return _noop_lock()  # Windows: no-op context manager
    lock_path = os.path.join(memory_dir, ".graph.lock")
    # DO NOT unlink stale lock files — flock is per-inode.
    # Unlinking + recreating gives a new inode, allowing
    # two processes to hold "exclusive" flocks simultaneously.
    fd = None
    try:
        fd = open(lock_path, "a")
        delay = 0.1
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                fcntl.flock(
                    fd, fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                return fd
            except (IOError, OSError):
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        fd.close()
        return None
    except (OSError, IOError):
        if fd is not None:
            fd.close()
        return None


def _release_lock(lock_fd):
    """Release maintenance lock safely."""
    if lock_fd is None:
        return
    # no-op context manager (Windows path) — nothing to release
    if hasattr(lock_fd, '__exit__'):
        return
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    except OSError:
        pass


def _backup_graph(graph_path):
    bak_path = graph_path + ".bak"
    try:
        try:
            os.unlink(bak_path)
        except OSError:
            pass
        os.link(graph_path, bak_path)
    except OSError:
        try:
            shutil.copy2(graph_path, bak_path)
        except OSError as exc:
            print(f"Maintenance: backup failed, aborting: {exc}")
            return False
    return True

def _compute_maintenance(entities, relations, project_dir, memory_dir):
    branch = get_branch(cwd=project_dir)
    recall_counts = read_recall_counts(memory_dir)
    entities = stamp_metadata(entities, branch)
    entities, relations, pruned = prune_entities(
        entities, relations, recall_counts,
        max_age_days=_cfg["MAX_AGE_DAYS"],
        decay_threshold=_cfg["DECAY_THRESHOLD"]
    )
    entities, relations, merged = consolidate(
        entities, relations,
        min_merge_name_len=_cfg["MIN_MERGE_NAME_LEN"]
    )
    return entities, relations, pruned, merged, recall_counts

def _finalize_maintenance(memory_dir, entities, recall_counts, pruned, merged):
    if pruned or merged:
        log_pruned(memory_dir, pruned, merged)
        print(f"Maintenance: pruned {pruned}, merged {merged} entities")
    if recall_counts:
        live_names = {e.get("name", "") for e in entities}
        stale = [k for k in recall_counts if k not in live_names]
        if stale:
            for k in stale:
                del recall_counts[k]
            rc_path = os.path.join(memory_dir, "recall_counts.json")
            with open(rc_path, "w") as f:
                json.dump(recall_counts, f)
    rebuild_index(memory_dir)


def run(project_dir):
    """Main: stamp → prune → consolidate → rewrite → index."""
    _cfg.update(_DEFAULTS)
    memory_dir = os.path.join(project_dir, ".memory")
    _load_config(memory_dir)
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    marker = os.path.join(memory_dir, ".last-maintenance")

    if not os.path.exists(graph_path):
        return

    if os.path.exists(marker):
        age_h = (time.time() - os.path.getmtime(marker)) / 3600
        if age_h < _cfg["THROTTLE_HOURS"]:
            return

    # P3: acquire lock BEFORE partition to eliminate TOCTOU
    lock_fd = _acquire_lock(memory_dir)
    if lock_fd is None:
        print("Maintenance: skipped (another instance running)")
        return

    try:
        # Merge pending inside lock
        mem_path = Path(memory_dir)
        gp = mem_path / "graph.jsonl"
        pending = mem_path / "graph.jsonl.pending"
        merge_pending(mem_path, gp, pending, lock=None, invalidate_cb=None)

        if not _backup_graph(graph_path):
            return

        try:
            entities, relations, others = partition_graph(graph_path)
        except (OSError, MemoryError) as exc:
            print(f"Maintenance: failed to load graph: {exc}")
            return
        if not entities and not relations and not others:
            Path(marker).touch()
            return

        entities, relations, pruned, merged, recall_counts = \
            _compute_maintenance(entities, relations, project_dir, memory_dir)

        write_jsonl(graph_path, chain(entities, relations, others))
        Path(marker).touch()
    finally:
        _release_lock(lock_fd)

    _finalize_maintenance(
        memory_dir, entities, recall_counts, pruned, merged
    )


def rebuild_index(memory_dir):
    """Rebuild TF-IDF index without throttle/prune/merge. Returns count."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    if not os.path.exists(graph_path):
        return 0
    try:
        entities, _, _ = partition_graph(graph_path)
    except (OSError, MemoryError):
        return 0
    if not entities:
        return 0
    branch = get_branch(
        cwd=os.path.dirname(memory_dir)
    )
    # P13: stamp_metadata mutates entities; write them back before indexing
    entities = stamp_metadata(entities, branch)
    # Rewrite graph so stamped metadata is persisted
    try:
        all_ents, all_rels, all_others = partition_graph(graph_path)
        stamped_names = {e.get("name", ""): e for e in entities}
        merged_ents = [stamped_names.get(e.get("name", ""), e) for e in all_ents]
        write_jsonl(graph_path, chain(merged_ents, all_rels, all_others))
    except (OSError, MemoryError):
        pass  # best-effort; still proceed to index
    index_input = [
        {"name": e.get("name", ""),
         "entityType": e.get("entityType", ""),
         "observations": e.get("observations", []),
         "_branch": e.get("_branch", "")}
        for e in entities
    ]
    return build_tfidf_index(index_input, memory_dir)


if __name__ == "__main__":
    _dry_run = "--dry-run" in sys.argv
    _args = [a for a in sys.argv[1:] if a != "--dry-run"]
    proj = (
        _args[0]
        if _args
        else os.environ.get(
            "CLAUDE_PROJECT_DIR", os.getcwd()
        )
    )
    mem_dir = os.path.join(proj, ".memory")
    if not os.path.isdir(mem_dir):
        print(
            f"Maintenance: skipped — no .memory/ in {proj}"
        )
    elif _dry_run:
        _load_config(mem_dir)
        gp = os.path.join(mem_dir, "graph.jsonl")
        if os.path.exists(gp):
            ents, rels, _ = partition_graph(gp)
            rc = read_recall_counts(mem_dir)
            _, _, pruned = prune_entities(
                list(ents), list(rels), rc,
                max_age_days=_cfg["MAX_AGE_DAYS"],
                decay_threshold=_cfg["DECAY_THRESHOLD"]
            )
            _, _, merged = consolidate(
                list(ents), list(rels),
                min_merge_name_len=_cfg["MIN_MERGE_NAME_LEN"]
            )
            print(
                f"Dry-run: would prune {pruned}, "
                f"merge {merged} of {len(ents)} "
                f"entities, {len(rels)} relations"
            )
    else:
        run(proj)

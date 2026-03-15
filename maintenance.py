#!/usr/bin/env python3
"""Memory graph maintenance: decay, prune, consolidate, TF-IDF index.

Pure Python — zero external dependencies. Works without any venv.
Designed to run from SessionStart hook, throttled to 1x/day.
Platform: Unix/macOS (uses fcntl for file locking).

Usage:
    python3 maintenance.py [project_dir]
    # or via env: CLAUDE_PROJECT_DIR=/path/to/project python3 maintenance.py
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
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path

# Fast JSON backend — same pattern as semantic_server/_json.py
# Falls back gracefully to stdlib json.
try:
    from semantic_server._json import (
        loads as _loads, dumps as _dumps, dump as _dump,
    )
except ImportError:
    try:
        import orjson as _orjson
        def _loads(s): return _orjson.loads(s)
        def _dumps(obj, **kw):
            return _orjson.dumps(obj).decode("utf-8")
        def _dump(obj, f, **kw):
            f.write(_orjson.dumps(obj).decode("utf-8"))
    except ImportError:
        _loads = json.loads
        def _dumps(obj, **kw):
            sep = kw.get("separators", (",", ":"))
            return json.dumps(obj, separators=sep)
        def _dump(obj, f, **kw):
            sep = kw.get("separators", (",", ":"))
            json.dump(obj, f, separators=sep)

# --- Configuration (defaults, overridable via .memory/config.json) ---
_DEFAULTS = {
    "DECAY_THRESHOLD": 0.1,
    "MAX_AGE_DAYS": 90,
    "THROTTLE_HOURS": 24,
    "MIN_MERGE_NAME_LEN": 4,
    "MAX_LOG_BYTES": 100_000,
}

# Mutable config — populated by _load_config()
_cfg = dict(_DEFAULTS)


def _valid(cfg, key, types, lo, hi):
    """Return cfg[key] if valid type + in bounds, else None."""
    v = cfg.get(key)
    if (v is not None and isinstance(v, types)
            and not isinstance(v, bool)
            and lo <= v <= hi):
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


# Pre-compiled regexes (avoid per-call re.compile overhead)
# Unicode-aware camelCase splitting
_RE_CAMEL = re.compile(
    r'([a-z\u00e0-\u00ff])([A-Z\u00c0-\u00df])'
)
_RE_SEPS = re.compile(r'[_\-.\s]+')
_RE_WORDS = re.compile(r'\w+', re.UNICODE)


def iter_jsonl(path):
    """Yield dicts from JSONL file, skip malformed/non-dict lines.

    Generator — avoids building full list in memory.
    Consumers that need a list can call list(iter_jsonl(path)).
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    obj = _loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except (json.JSONDecodeError, ValueError,
                        OverflowError):
                    continue


def partition_graph(path):
    """Load and partition JSONL graph in a single pass.

    Returns (entities, relations, others) lists.
    Streams from disk — never holds the full unparsed list.
    """
    entities, relations, others = [], [], []
    for e in iter_jsonl(path):
        t = e.get("type")
        if t == "entity":
            entities.append(e)
        elif t == "relation":
            relations.append(e)
        else:
            others.append(e)
    return entities, relations, others


def _safe_jsonl_lines(entries):
    """Yield JSONL lines, skipping unserializable entries."""
    for e in entries:
        try:
            yield _dumps(e) + "\n"
        except (TypeError, ValueError, OverflowError):
            continue


def write_jsonl(path, entries):
    """Atomic write: write to .new, then os.replace.

    Uses .new suffix so interrupted writes leave original
    intact. Cleans up on failure. Skips unserializable
    entries instead of aborting.
    """
    tmp = path + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(_safe_jsonl_lines(entries))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_branch(cwd=None):
    """Get current git branch, or 'unknown'.

    Args:
        cwd: directory to run git in (defaults to process cwd).
             Prevents stamping wrong branch when hook cwd
             differs from project dir.
    """
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


def parse_iso_date(s):
    """Parse ISO 8601 date string to tz-aware datetime.

    Always returns UTC-aware datetime or None. Bare ISO
    strings (no tz) are assumed UTC to avoid TypeError when
    subtracting from datetime.now(timezone.utc).
    """
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def load_recall_counts(memory_dir):
    """Load recall frequency counts from sidecar file."""
    rc_path = os.path.join(memory_dir, "recall_counts.json")
    try:
        with open(rc_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def score_entity(entity, now, recall_counts=None,
                 now_ts=None, cutoff_str=None):
    """Score entity: obs_count * recency * recall_boost.

    Uses string comparison for fast age check. Only falls
    back to datetime parsing for entities near the decay
    threshold. now_ts/cutoff_str are pre-computed by caller
    for batch efficiency.

    recall_boost = 1 + log(recall_count) when recall data
    is available (Hebbian reinforcement).
    """
    obs_count = len(entity.get("observations", []))
    if obs_count == 0:
        return 0.0

    updated = entity.get("_updated", "")
    if (not updated
            or (cutoff_str and updated < cutoff_str)):
        days = _cfg["MAX_AGE_DAYS"]
    else:
        dt = parse_iso_date(updated)
        if not dt:
            days = _cfg["MAX_AGE_DAYS"]
        elif now_ts:
            days = max(
                int((now_ts - dt.timestamp()) / 86400), 0
            )
        else:
            days = max((now - dt).days, 0)

    recency = 1.0 / (1.0 + days)
    score = obs_count * recency

    if recall_counts:
        rc = recall_counts.get(entity.get("name", ""), 0)
        if rc > 0:
            score *= (1.0 + math.log(rc))

    return score


def prune_entities(entities, relations, recall_counts=None):
    """Remove low-score entities with zero inbound relations.

    Pre-computes cutoff_str for fast string-based age
    comparison in score_entity().
    """
    has_inbound = {r.get("to", "") for r in relations}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    # Pre-compute cutoff date string for fast path
    cutoff_dt = now - timedelta(days=_cfg["MAX_AGE_DAYS"])
    cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    pruned_names = set()
    kept = []
    for e in entities:
        name = e.get("name", "")
        if not name.strip():
            pruned_names.add(name)
            continue
        score = score_entity(
            e, now, recall_counts,
            now_ts=now_ts, cutoff_str=cutoff_str,
        )
        if (score < _cfg["DECAY_THRESHOLD"]
                and name not in has_inbound):
            pruned_names.add(name)
            continue
        kept.append(e)

    # Drop orphaned relations
    kept_rels = [
        r for r in relations
        if r.get("from") not in pruned_names
        and r.get("to") not in pruned_names
    ]
    return kept, kept_rels, len(pruned_names)


def normalize_name(name):
    """Normalize entity name for fuzzy matching.

    Handles camelCase, snake_case, kebab-case.
    Unicode-aware via re.UNICODE flag.
    """
    name = _RE_CAMEL.sub(r'\1 \2', name)
    return _RE_SEPS.sub(' ', name.lower().strip())


def _safe_obs_dedup(observations):
    """Deduplicate observations preserving insertion order."""
    if not observations:
        return []
    seen = set()
    result = []
    for o in observations:
        key = (o if isinstance(o, str)
               else json.dumps(o, sort_keys=True))
        if key not in seen:
            seen.add(key)
            result.append(o)
    return result


_MAIN_BRANCHES = frozenset({
    "main", "master", "trunk", "develop",
})
_GUARD_AGE_DAYS = 7
_MAX_CONSOLIDATE_ENTITIES = 50_000


def consolidate(entities, relations):
    """Merge entities with same type + overlapping names.

    O(n log n) sorted-merge: sorts by (entityType,
    normalized_name), then scans linearly with a small
    lookahead window to find substring matches among
    adjacent entries.

    Short names (< MIN_MERGE_NAME_LEN) merge only on
    exact normalized match.

    Skips consolidation if entity count exceeds
    _MAX_CONSOLIDATE_ENTITIES to prevent OOM.
    """
    # Memory guard
    if len(entities) > _MAX_CONSOLIDATE_ENTITIES:
        import sys as _sys
        print(
            f"Maintenance: skipping consolidation "
            f"({len(entities)} entities > "
            f"{_MAX_CONSOLIDATE_ENTITIES} cap)",
            file=_sys.stderr,
        )
        return entities, relations, 0

    min_merge = _cfg["MIN_MERGE_NAME_LEN"]

    # Age-gated cross-branch consolidation guard
    guard_cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=_GUARD_AGE_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build sort keys: (entityType, normalized_name, idx)
    n = len(entities)
    keyed = []
    for i, e in enumerate(entities):
        norm = normalize_name(e.get("name", ""))
        etype = e.get("entityType", "")
        keyed.append((etype, norm, i))

    # O(n log n) sort — puts merge candidates adjacent
    keyed.sort()

    merged_count = 0
    absorbed = set()  # indices absorbed into another
    renames = {}

    # Lookahead window — check next entries with same
    # entityType for substring match. Window size is
    # small (20) so inner loop is effectively O(1).
    WINDOW = 20

    for pos in range(n):
        etype_i, norm_i, idx_i = keyed[pos]
        if idx_i in absorbed:
            continue
        if not norm_i.strip():
            continue

        ent_i = entities[idx_i]
        obs_dict_i = None
        len_i = len(norm_i)

        # Scan forward within same entityType
        for ahead in range(1, WINDOW + 1):
            j = pos + ahead
            if j >= n:
                break
            etype_j, norm_j, idx_j = keyed[j]
            # Stop at type boundary
            if etype_j != etype_i:
                break
            if idx_j in absorbed:
                continue
            if not norm_j.strip():
                continue

            len_j = len(norm_j)
            # Length ratio check — tightened to 2x
            # to prevent false merges on common names
            if len_i > 2 * len_j or len_j > 2 * len_i:
                continue

            # Short names merge only on exact match
            shorter = min(len_i, len_j)
            if shorter < min_merge:
                if norm_i != norm_j:
                    continue

            # Word-boundary containment — prevents
            # "config" merging with "project config"
            # unless one is a proper word-boundary
            # substring of the other.
            padded_i = f" {norm_i} "
            padded_j = f" {norm_j} "
            if (norm_i == norm_j
                    or padded_i in padded_j
                    or padded_j in padded_i):
                ent_j = entities[idx_j]
                # Cross-branch guard: block merging
                # young entities from different features
                bi = ent_i.get("_branch", "")
                bj = ent_j.get("_branch", "")
                if (bi and bj and bi != bj
                        and bi not in _MAIN_BRANCHES
                        and bj not in _MAIN_BRANCHES):
                    ci = ent_i.get("_created", "")
                    cj = ent_j.get("_created", "")
                    if (ci and ci > guard_cutoff
                            and cj
                            and cj > guard_cutoff):
                        continue
                # Lazy dedup — built once per absorber
                if obs_dict_i is None:
                    obs_dict_i = _safe_obs_dedup(
                        ent_i.get("observations", [])
                    )
                    _seen_i = {
                        (o if isinstance(o, str)
                         else json.dumps(
                             o, sort_keys=True))
                        for o in obs_dict_i
                    }
                for o in ent_j.get("observations", []):
                    key = (o if isinstance(o, str)
                           else json.dumps(
                               o, sort_keys=True))
                    if key not in _seen_i:
                        _seen_i.add(key)
                        obs_dict_i.append(o)
                # Keep newer _updated
                upd_j = ent_j.get("_updated", "")
                upd_i = ent_i.get("_updated", "")
                if upd_j and (not upd_i
                              or upd_j > upd_i):
                    ent_i["_updated"] = upd_j
                absorbed.add(idx_j)
                old_name = ent_j.get("name", "")
                renames[old_name] = ent_i.get(
                    "name", ""
                )
                merged_count += 1

        if obs_dict_i is not None:
            ent_i["observations"] = list(obs_dict_i)

    # Free sort key list
    del keyed

    kept = [
        e for i, e in enumerate(entities)
        if i not in absorbed
    ]

    # Cap observations to prevent unbounded growth.
    # activity-log entities keep newest 50; others 200.
    MAX_OBS_ACTIVITY = 50
    MAX_OBS_DEFAULT = 200
    for e in kept:
        obs = e.get("observations", [])
        etype = e.get("entityType", "")
        cap = (MAX_OBS_ACTIVITY if etype == "activity-log"
               else MAX_OBS_DEFAULT)
        if len(obs) > cap:
            e["observations"] = obs[-cap:]

    # Resolve transitive rename chains (A→B→C → A→C)
    for k in list(renames):
        v = renames[k]
        while v in renames and renames[v] != v:
            v = renames[v]
        renames[k] = v

    # Update relation references, drop self-refs + dupes
    updated_rels = []
    seen_rels = set()
    for r in relations:
        fr = r.get("from", "")
        to = r.get("to", "")
        new_fr = renames.get(fr, fr)
        new_to = renames.get(to, to)
        if new_fr == new_to:
            continue
        rel_key = (
            new_fr, new_to, r.get("relationType", "")
        )
        if rel_key not in seen_rels:
            seen_rels.add(rel_key)
            if new_fr != fr or new_to != to:
                r = dict(
                    r, **{"from": new_fr, "to": new_to}
                )
            updated_rels.append(r)

    return kept, updated_rels, merged_count


def stamp_metadata(entities, branch):
    """Add _branch, _created to NEW entities only.

    Only stamps _updated on entities missing it entirely
    (first time seen). Existing _updated is preserved so
    decay scoring works correctly.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for e in entities:
        if "_branch" not in e:
            e["_branch"] = branch
        if "_created" not in e:
            e["_created"] = now
        # Only set _updated if missing — preserve existing
        if "_updated" not in e:
            e["_updated"] = now
    return entities


# Stopword set for TF-IDF filtering
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "need", "must", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "about", "like", "through", "after", "over",
    "between", "out", "against", "during", "without",
    "before", "under", "around", "among", "it", "its",
    "this", "that", "these", "those", "he", "she", "they",
    "we", "you", "i", "me", "him", "her", "us", "them",
    "my", "your", "his", "our", "their", "what", "which",
    "who", "whom", "how", "when", "where", "why", "all",
    "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because",
    "but", "and", "or", "if", "then", "else", "also",
})

# Hex/UUID noise pattern
_RE_HEX_NOISE = re.compile(r'^[0-9a-f]{8,}$')


def _filter_token(w):
    """Return True if token should be kept in TF-IDF index."""
    if len(w) < 2 or len(w) > 50:
        return False
    if w in _STOPWORDS:
        return False
    if _RE_HEX_NOISE.match(w):
        return False
    return True


def build_tfidf_index(entities, memory_dir):
    """Build TF-IDF index with magnitudes, postings, metadata.

    Two-pass: (1) tokenize + DF, (2) TF-IDF vectors + magnitudes.
    Stores entity metadata for self-sufficient search results.
    Streams JSON output to reduce peak memory.
    Filters stopwords and noise tokens.
    Excludes terms with DF < 2 (singletons).
    """
    if not entities:
        return 0

    # Pass 1: tokenize + compute DF + collect metadata
    docs = {}
    meta = {}
    df = Counter()
    for ent in entities:
        name = ent.get("name", "")
        obs = ent.get("observations", [])
        # Coerce non-string observations
        obs_strs = []
        for o in obs:
            if isinstance(o, str):
                obs_strs.append(o)
            else:
                obs_strs.append(str(o))
        etype = ent.get("entityType", "")
        # Tokenize each component independently — avoids
        # triple temporary string (join+concat+lower) per
        # entity. ~1.2GB less GC pressure for 10K entities.
        words = []
        for piece in chain((name, etype), obs_strs):
            words.extend(
                w for w in _RE_WORDS.findall(
                    piece.lower()
                )
                if _filter_token(w)
            )
        if words:
            docs[name] = words
            meta[name] = {
                "entityType": etype,
                "observations": obs_strs[:5],
                "_branch": ent.get("_branch", ""),
            }
            for w in set(words):
                df[w] += 1

    if not docs:
        return 0

    n_docs = len(docs)
    # Exclude singleton terms (DF < 2)
    # Threshold at 50 to avoid over-filtering small corpora
    min_df = 2 if n_docs > 50 else 1
    idf = {
        w: math.log((n_docs + 1) / (count + 1)) + 1
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
            continue  # skip entities with no significant terms
        vectors[name] = vec
        mag = math.sqrt(sum(v * v for v in vec.values()))
        magnitudes[name] = round(mag, 4)

    # Free intermediate data before serialization
    del docs, df

    # Filter metadata to indexed entities only
    meta = {k: v for k, v in meta.items() if k in vectors}
    n_indexed = len(vectors)

    index_path = os.path.join(memory_dir, "tfidf_index.json")
    tmp = index_path + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # Stream sections to reduce peak memory
            f.write('{"vectors":')
            _dump(vectors, f)
            del vectors  # free before next section
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


def _acquire_lock(memory_dir):
    """Acquire exclusive lock for maintenance.

    Returns lock file descriptor or None if lock held.
    Uses fcntl.flock for cross-process safety.
    Detects stale locks older than 1 hour.
    Uses .graph.lock (shared with MCP server) to prevent
    concurrent rewrites from both processes.
    """
    if fcntl is None:
        return True  # Windows: no locking, return truthy
    lock_path = os.path.join(memory_dir, ".graph.lock")
    # DO NOT unlink stale lock files — flock is per-inode.
    # Unlinking + recreating gives a new inode, allowing
    # two processes to hold "exclusive" flocks on
    # different inodes simultaneously (TOCTOU race).
    # Instead, use non-blocking flock with a timeout.
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
        # Timeout — could not acquire lock
        fd.close()
        return None
    except (OSError, IOError):
        if fd is not None:
            fd.close()
        return None


def _release_lock(lock_fd):
    """Release maintenance lock safely."""
    if lock_fd is not None and lock_fd is not True:
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except OSError:
            pass


def run(project_dir):
    """Main maintenance routine with timing instrumentation.

    Lock scope reduced — lock acquired only for write
    phases, not for the entire read+compute cycle.
    """
    # Reset config to defaults before loading project overrides
    # (prevents leaking config between calls if imported as lib)
    _cfg.update(_DEFAULTS)
    memory_dir = os.path.join(project_dir, ".memory")
    _load_config(memory_dir)
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    marker = os.path.join(memory_dir, ".last-maintenance")

    if not os.path.exists(graph_path):
        return

    # Throttle
    if os.path.exists(marker):
        age_h = (
            (time.time() - os.path.getmtime(marker)) / 3600
        )
        if age_h < _cfg["THROTTLE_HOURS"]:
            return

    # --- Read + compute phase (NO lock) ---
    # Backup before mutation — hard link (O(1)) with
    # fallback to copy. Safe because write_jsonl uses
    # atomic replace (new inode).
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
            print(
                f"Maintenance: backup failed: {exc}"
            )

    # Stream + partition in one pass
    try:
        entities, relations, others = partition_graph(
            graph_path
        )
    except (OSError, MemoryError) as exc:
        print(f"Maintenance: failed to load graph: "
              f"{exc}")
        return
    if not entities and not relations and not others:
        Path(marker).touch()
        return

    # Record graph mtime before compute — detect races
    try:
        pre_mtime = os.path.getmtime(graph_path)
    except OSError:
        pre_mtime = 0.0

    branch = get_branch(cwd=project_dir)
    recall_counts = load_recall_counts(memory_dir)

    t0 = time.monotonic()
    entities = stamp_metadata(entities, branch)
    t1 = time.monotonic()
    entities, relations, pruned = prune_entities(
        entities, relations, recall_counts
    )
    t2 = time.monotonic()
    entities, relations, merged = consolidate(
        entities, relations
    )
    t3 = time.monotonic()

    # --- Write phase (LOCKED) ---
    lock_fd = _acquire_lock(memory_dir)
    if lock_fd is None:
        print("Maintenance: skipped (another instance running)")
        return

    try:
        # Check for concurrent modification
        try:
            post_mtime = os.path.getmtime(graph_path)
        except OSError:
            post_mtime = 0.0
        if post_mtime != pre_mtime:
            print("Maintenance: graph modified during "
                  "compute — skipping write (will retry)")
            # Do NOT touch marker — allow prompt retry
            return

        # chain() avoids allocating a merged list
        write_jsonl(
            graph_path,
            chain(entities, relations, others),
        )
        Path(marker).touch()
    finally:
        _release_lock(lock_fd)

    del others

    if pruned or merged:
        log_pruned(memory_dir, pruned, merged)
        print(
            f"Maintenance: pruned {pruned}, "
            f"merged {merged} entities"
        )

    # Phase 1b: Prune stale recall counts (no lock needed)
    if recall_counts:
        live_names = {
            e.get("name", "") for e in entities
        }
        stale = [
            k for k in recall_counts
            if k not in live_names
        ]
        if stale:
            for k in stale:
                del recall_counts[k]
            rc_path = os.path.join(
                memory_dir, "recall_counts.json"
            )
            rc_tmp = rc_path + ".new"
            try:
                with open(rc_tmp, "w",
                          encoding="utf-8") as f:
                    json.dump(recall_counts, f,
                              separators=(",", ":"))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(rc_tmp, rc_path)
            except Exception:
                try:
                    os.unlink(rc_tmp)
                except OSError:
                    pass

    n_relations = len(relations)
    del recall_counts, relations

    # Graph statistics (before index build so entities
    # can be freed sooner — reduces peak memory)
    _print_graph_stats(
        entities, n_relations, pruned, merged, memory_dir
    )

    # Extract lightweight data for index build —
    # frees full entity dicts and extra metadata fields.
    index_input = [
        {"name": e.get("name", ""),
         "entityType": e.get("entityType", ""),
         "observations": e.get("observations", []),
         "_branch": e.get("_branch", "")}
        for e in entities
    ]
    del entities

    # Phase 2: TF-IDF index (writes its own temp file)
    try:
        indexed = build_tfidf_index(
            index_input, memory_dir
        )
        t4 = time.monotonic()
        if indexed:
            print(
                f"TF-IDF index: {indexed} entities indexed"
            )
        print(
            f"  timing: stamp={t1 - t0:.3f}s "
            f"prune={t2 - t1:.3f}s "
            f"consolidate={t3 - t2:.3f}s "
            f"index={t4 - t3:.3f}s"
        )
    except Exception as e:
        print(f"Warning: TF-IDF index build failed: {e}")

    del index_input


if __name__ == "__main__":
    proj = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get(
            "CLAUDE_PROJECT_DIR", os.getcwd()
        )
    )
    run(proj)

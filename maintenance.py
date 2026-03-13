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
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path

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

_CONFIG_KEYS = {
    "decay_threshold": ("DECAY_THRESHOLD", (int, float)),
    "max_age_days": ("MAX_AGE_DAYS", int),
    "throttle_hours": ("THROTTLE_HOURS", (int, float)),
    "min_merge_name_len": ("MIN_MERGE_NAME_LEN", int),
    "max_log_bytes": ("MAX_LOG_BYTES", int),
}

# #2: Validation bounds for config values
_CONFIG_BOUNDS = {
    "DECAY_THRESHOLD": (0.0, 10.0),
    "MAX_AGE_DAYS": (1, 3650),
    "THROTTLE_HOURS": (0.1, 720),
    "MIN_MERGE_NAME_LEN": (1, 100),
    "MAX_LOG_BYTES": (1000, 100_000_000),
}


def _load_config(memory_dir):
    """#2: Load per-project config with validation + atomic apply."""
    cfg_path = os.path.join(memory_dir, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(cfg, dict):
        return
    # Build validated overrides first, then apply atomically
    overrides = {}
    for key, (gname, types) in _CONFIG_KEYS.items():
        if key in cfg and isinstance(cfg[key], types) \
                and not isinstance(cfg[key], bool):
            val = cfg[key]
            bounds = _CONFIG_BOUNDS.get(gname)
            if bounds:
                lo, hi = bounds
                if val < lo or val > hi:
                    continue
            overrides[gname] = val
    # Atomic apply — all or nothing per valid key
    _cfg.update(overrides)


# Pre-compiled regexes (avoid per-call re.compile overhead)
# #27: Unicode-aware camelCase splitting
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
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
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


def write_jsonl(path, entries):
    """#8: Atomic write: write to .new, then os.replace.

    Uses .new suffix so interrupted writes leave original
    intact. Cleans up on failure.
    """
    tmp = path + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(
                json.dumps(e, separators=(",", ":")) + "\n"
                for e in entries
            )
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


def score_entity(entity, now, recall_counts=None):
    """Score entity: obs_count * recency * recall_boost.

    recall_boost = 1 + log(recall_count) when recall data
    is available, making frequently-searched entities resist
    decay (Hebbian reinforcement).
    """
    obs_count = len(entity.get("observations", []))
    if obs_count == 0:
        return 0.0

    updated = entity.get("_updated", "")
    dt = parse_iso_date(updated) if updated else None
    if dt:
        days = max((now - dt).days, 0)
    else:
        days = _cfg["MAX_AGE_DAYS"]

    recency = 1.0 / (1.0 + days)
    score = obs_count * recency

    if recall_counts:
        rc = recall_counts.get(entity.get("name", ""), 0)
        if rc > 0:
            score *= (1.0 + math.log(rc))

    return score


def prune_entities(entities, relations, recall_counts=None):
    """Remove low-score entities with zero inbound relations.

    Operates on pre-partitioned entity/relation lists.
    recall_counts: optional dict from load_recall_counts().
    """
    has_inbound = {r.get("to", "") for r in relations}
    now = datetime.now(timezone.utc)

    pruned_names = set()
    kept = []
    for e in entities:
        name = e.get("name", "")
        if not name.strip():
            pruned_names.add(name)
            continue
        score = score_entity(e, now, recall_counts)
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
    """#27: Normalize entity name for fuzzy matching.

    Handles camelCase, snake_case, kebab-case.
    Unicode-aware via re.UNICODE flag.
    """
    name = _RE_CAMEL.sub(r'\1 \2', name)
    return _RE_SEPS.sub(' ', name.lower().strip())


def _obs_to_str(obs):
    """Normalize a single observation to a string."""
    if isinstance(obs, str):
        return obs
    try:
        return json.dumps(obs, sort_keys=True)
    except (TypeError, ValueError):
        return str(obs)


def _safe_obs_set(observations):
    """#23: Convert observations to a set safely.

    Normalizes non-string items (dicts, lists) to their
    JSON string representation for dedup. Returns a dict
    of {normalized_key: original_value} so callers can
    preserve original types after deduplication.
    """
    result = {}
    for obs in observations:
        key = _obs_to_str(obs)
        if key not in result:
            result[key] = obs
    return result


def consolidate(entities, relations):
    """Merge entities with same type + overlapping names.

    Uses token-based candidate filtering to avoid full O(n^2)
    pairwise comparison. Short names (< MIN_MERGE_NAME_LEN)
    merge only on exact normalized match.
    """
    min_merge = _cfg["MIN_MERGE_NAME_LEN"]
    by_type = defaultdict(list)
    for i, e in enumerate(entities):
        by_type[e.get("entityType", "")].append((i, e))

    merged_count = 0
    to_remove = set()
    renames = {}

    for _etype, group in by_type.items():
        # Pre-compute normalized names and token index
        norm_data = []
        token_idx = defaultdict(set)
        for gi, (_, ent) in enumerate(group):
            norm = normalize_name(ent.get("name", ""))
            tokens = set(norm.split()) if norm else set()
            norm_data.append((norm, tokens))
            for t in tokens:
                token_idx[t].add(gi)

        for gi in range(len(group)):
            idx_i, ent_i = group[gi]
            if idx_i in to_remove:
                continue
            name_i, tokens_i = norm_data[gi]

            # Skip names that normalize to whitespace-only
            if not name_i.strip():
                continue

            # Candidates: entities sharing >= 1 name token
            candidates = set()
            for t in tokens_i:
                candidates |= token_idx[t]

            # #19: Lazy obs dict — built once if any merge,
            # reused across multiple merges
            obs_dict_i = None

            for gj in sorted(candidates):
                if gj <= gi:
                    continue
                idx_j, ent_j = group[gj]
                if idx_j in to_remove:
                    continue
                name_j, _ = norm_data[gj]

                if not name_j.strip():
                    continue

                # Short names merge only on exact match
                shorter = min(len(name_i), len(name_j))
                if shorter < min_merge:
                    if name_i != name_j:
                        continue

                if name_i in name_j or name_j in name_i:
                    # #23: safe obs dict — preserves types
                    if obs_dict_i is None:
                        obs_dict_i = _safe_obs_set(
                            ent_i.get("observations", [])
                        )
                    for k, v in _safe_obs_set(
                        ent_j.get("observations", [])
                    ).items():
                        if k not in obs_dict_i:
                            obs_dict_i[k] = v
                    # Keep the newer _updated timestamp
                    dt_j = parse_iso_date(
                        ent_j.get("_updated", "")
                    )
                    dt_i = parse_iso_date(
                        ent_i.get("_updated", "")
                    )
                    if dt_j and (not dt_i or dt_j > dt_i):
                        ent_i["_updated"] = ent_j.get(
                            "_updated", ""
                        )
                    to_remove.add(idx_j)
                    old_name = ent_j.get("name", "")
                    renames[old_name] = ent_i.get(
                        "name", ""
                    )
                    merged_count += 1

            # Deferred list conversion — once per absorber
            if obs_dict_i is not None:
                ent_i["observations"] = list(
                    obs_dict_i.values()
                )

    kept = [
        e for i, e in enumerate(entities)
        if i not in to_remove
    ]

    # Resolve transitive rename chains (A→B→C → A→C)
    changed = True
    while changed:
        changed = False
        for k, v in list(renames.items()):
            if v in renames and renames[v] != v:
                renames[k] = renames[v]
                changed = True

    # Update relation references, drop self-refs + dupes
    updated_rels = []
    seen_rels = set()
    for r in relations:
        fr = r.get("from", "")
        to = r.get("to", "")
        if fr in renames:
            r["from"] = renames[fr]
            fr = r["from"]
        if to in renames:
            r["to"] = renames[to]
            to = r["to"]
        if fr == to:
            continue
        rel_key = (fr, to, r.get("relationType", ""))
        if rel_key not in seen_rels:
            seen_rels.add(rel_key)
            updated_rels.append(r)

    return kept, updated_rels, merged_count


def stamp_metadata(entities, branch):
    """#3: Add _branch, _created to NEW entities only.

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


def build_tfidf_index(entities, memory_dir):
    """Build TF-IDF index with magnitudes, postings, metadata.

    Two-pass: (1) tokenize + DF, (2) TF-IDF vectors + magnitudes.
    Stores entity metadata for self-sufficient search results.
    #15: Streams JSON output to reduce peak memory.
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
        # #23: coerce non-string observations
        obs_strs = []
        for o in obs:
            if isinstance(o, str):
                obs_strs.append(o)
            else:
                obs_strs.append(str(o))
        etype = ent.get("entityType", "")
        text = f"{name} {etype} " + " ".join(obs_strs)
        words = _RE_WORDS.findall(text.lower())
        if words:
            docs[name] = words
            meta[name] = {
                "entityType": etype,
                "observations": obs_strs[:5],
            }
            for w in set(words):
                df[w] += 1

    if not docs:
        return 0

    n_docs = len(docs)
    idf = {
        w: math.log((n_docs + 1) / (count + 1)) + 1
        for w, count in df.items()
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

    # #15: Free intermediate data before serialization
    del docs, df

    index_path = os.path.join(memory_dir, "tfidf_index.json")
    tmp = index_path + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "vectors": vectors,
                "idf": {
                    k: round(v, 4) for k, v in idf.items()
                },
                "magnitudes": magnitudes,
                "postings": dict(postings),
                "metadata": {
                    k: v for k, v in meta.items()
                    if k in vectors
                },
                "doc_count": len(vectors),
                "built": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            }, f, separators=(",", ":"))
        os.replace(tmp, index_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(vectors)


def log_pruned(memory_dir, pruned_count, merged_count):
    """Append maintenance event to pruned.log.

    Rotates when log exceeds MAX_LOG_BYTES.
    """
    log_path = os.path.join(memory_dir, "pruned.log")
    try:
        if (os.path.exists(log_path)
                and os.path.getsize(log_path)
                > _cfg["MAX_LOG_BYTES"]):
            bak = log_path + ".old"
            try:
                os.replace(log_path, bak)
            except OSError:
                pass
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


def _print_graph_stats(entities, relations, pruned, merged,
                       memory_dir):
    """Print graph health statistics after maintenance."""
    if not entities:
        return
    type_counts = Counter(
        e.get("entityType", "unknown") for e in entities
    )
    obs_counts = [
        len(e.get("observations", [])) for e in entities
    ]
    avg_obs = (
        sum(obs_counts) / len(obs_counts) if obs_counts
        else 0
    )
    dates = [
        e.get("_created") or e.get("_updated", "")
        for e in entities
    ]
    dates = [d for d in dates if d]
    idx_path = os.path.join(memory_dir, "tfidf_index.json")
    idx_kb = 0
    try:
        idx_kb = os.path.getsize(idx_path) // 1024
    except OSError:
        pass
    print(f"=== Graph Stats: {len(entities)} entities, "
          f"{len(relations)} relations ===")
    for t, c in type_counts.most_common(10):
        print(f"  {t}: {c}")
    print(f"  avg observations/entity: {avg_obs:.1f}")
    if dates:
        print(f"  oldest: {min(dates)}  newest: {max(dates)}")
    print(f"  pruned={pruned}  merged={merged}  "
          f"index={idx_kb}KB")


def _acquire_lock(memory_dir):
    """#10: Acquire exclusive lock for maintenance.

    Returns lock file descriptor or None if lock held.
    Uses fcntl.flock for cross-process safety.
    """
    if fcntl is None:
        return True  # Windows: no locking, return truthy
    lock_path = os.path.join(memory_dir, ".maintenance.lock")
    fd = None
    try:
        fd = open(lock_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, IOError):
        if fd is not None:
            fd.close()
        return None


def run(project_dir):
    """Main maintenance routine with timing instrumentation."""
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

    # #10: Acquire exclusive lock
    lock_fd = _acquire_lock(memory_dir)
    if lock_fd is None:
        print("Maintenance: skipped (another instance running)")
        return

    try:
        # Backup before mutation — non-fatal if it fails
        try:
            shutil.copy2(graph_path, graph_path + ".bak")
        except OSError as exc:
            print(f"Maintenance: backup failed: {exc}")

        # Stream + partition in one pass
        try:
            entities, relations, others = partition_graph(
                graph_path
            )
        except OSError:
            return
        if not entities and not relations and not others:
            Path(marker).touch()
            return

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

        # chain() avoids allocating a merged list
        write_jsonl(
            graph_path,
            chain(entities, relations, others),
        )
        Path(marker).touch()

        if pruned or merged:
            log_pruned(memory_dir, pruned, merged)
            print(
                f"Maintenance: pruned {pruned}, "
                f"merged {merged} entities"
            )

        # Phase 2: TF-IDF index
        try:
            indexed = build_tfidf_index(
                entities, memory_dir
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

        # Phase 3: Graph statistics
        _print_graph_stats(
            entities, relations, pruned, merged, memory_dir
        )
    finally:
        # Release lock
        try:
            if fcntl is not None and hasattr(lock_fd, 'close'):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except OSError:
            pass


if __name__ == "__main__":
    proj = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get(
            "CLAUDE_PROJECT_DIR", os.getcwd()
        )
    )
    run(proj)

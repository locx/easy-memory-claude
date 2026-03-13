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

# Validation bounds for config values
_CONFIG_BOUNDS = {
    "DECAY_THRESHOLD": (0.0, 10.0),
    "MAX_AGE_DAYS": (1, 3650),
    "THROTTLE_HOURS": (0.1, 720),
    "MIN_MERGE_NAME_LEN": (1, 100),
    "MAX_LOG_BYTES": (1000, 100_000_000),
}


def _load_config(memory_dir):
    """Load per-project config with validation + atomic apply."""
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
    """Atomic write: write to .new, then os.replace.

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
    if not updated:
        days = _cfg["MAX_AGE_DAYS"]
    elif cutoff_str and updated < cutoff_str:
        # Fast path: clearly old, skip datetime parse
        days = _cfg["MAX_AGE_DAYS"]
    elif now_ts:
        # Epoch-based calculation — avoids datetime math
        dt = parse_iso_date(updated)
        if dt:
            days = max(
                int((now_ts - dt.timestamp()) / 86400), 0
            )
        else:
            days = _cfg["MAX_AGE_DAYS"]
    else:
        dt = parse_iso_date(updated)
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


def _obs_to_str(obs):
    """Normalize a single observation to a string."""
    if isinstance(obs, str):
        return obs
    try:
        return json.dumps(obs, sort_keys=True)
    except (TypeError, ValueError):
        return str(obs)


def _safe_obs_set(observations):
    """Convert observations to a set safely.

    Normalizes non-string items (dicts, lists) to their
    JSON string representation for dedup. Returns a dict
    of {normalized_key: original_value} so callers can
    preserve original types after deduplication.
    Fast path for all-string lists (>95% case).
    """
    if not observations:
        return {}
    # Fast path — skip isinstance per-element
    # when all observations are strings (common case)
    if isinstance(observations[0], str):
        all_str = True
        for obs in observations:
            if not isinstance(obs, str):
                all_str = False
                break
        if all_str:
            return {o: o for o in observations}
    result = {}
    for obs in observations:
        if isinstance(obs, str):
            if obs not in result:
                result[obs] = obs
        else:
            key = _obs_to_str(obs)
            if key not in result:
                result[key] = obs
    return result


_MAX_CONSOLIDATE_ENTITIES = 50_000


def consolidate(entities, relations):
    """Merge entities with same type + overlapping names.

    Uses token-based candidate filtering to avoid full O(n^2)
    pairwise comparison. Short names (< MIN_MERGE_NAME_LEN)
    merge only on exact normalized match.

    Skips consolidation if entity count exceeds
    _MAX_CONSOLIDATE_ENTITIES to prevent OOM.
    Frees norm_data/token_idx per type group.
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

        # Filter high-frequency tokens to bound fan-out
        max_freq = max(int(len(group) ** 0.5), 10)
        token_idx = {
            t: s for t, s in token_idx.items()
            if len(s) <= max_freq
        }

        # Map original indices to set for fast lookup
        removed_gi = set()

        for gi in range(len(group)):
            idx_i, ent_i = group[gi]
            if idx_i in to_remove or gi in removed_gi:
                continue
            name_i, tokens_i = norm_data[gi]

            # Skip names that normalize to whitespace-only
            if not name_i.strip():
                continue

            # Candidates: entities sharing >= 1 name token
            # Capped at 50 to prevent O(n^2) when many
            # entities share common tokens.
            candidates = set()
            for t in tokens_i:
                if t in token_idx:
                    candidates |= token_idx[t]
                    if len(candidates) > 50:
                        break

            # Lazy obs dict — built once if any merge,
            # reused across multiple merges
            obs_dict_i = None

            for gj in sorted(candidates):
                if gj <= gi:
                    continue
                idx_j, ent_j = group[gj]
                if idx_j in to_remove or gj in removed_gi:
                    continue
                name_j, _ = norm_data[gj]

                if not name_j.strip():
                    continue

                # Skip if length ratio makes
                # substring containment impossible
                len_i = len(name_i)
                len_j = len(name_j)
                if len_i > 3 * len_j or len_j > 3 * len_i:
                    continue

                # Short names merge only on exact match
                shorter = min(len_i, len_j)
                if shorter < min_merge:
                    if name_i != name_j:
                        continue

                if name_i in name_j or name_j in name_i:
                    # Safe obs dict — preserves types
                    if obs_dict_i is None:
                        obs_dict_i = _safe_obs_set(
                            ent_i.get("observations", [])
                        )
                    for k, v in _safe_obs_set(
                        ent_j.get("observations", [])
                    ).items():
                        if k not in obs_dict_i:
                            obs_dict_i[k] = v
                    # Keep the newer _updated — string
                    # comparison is safe for ISO 8601
                    upd_j = ent_j.get("_updated", "")
                    upd_i = ent_i.get("_updated", "")
                    if upd_j and (not upd_i
                                  or upd_j > upd_i):
                        ent_i["_updated"] = upd_j
                    to_remove.add(idx_j)
                    removed_gi.add(gj)
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

        # Free per-group intermediates
        del norm_data, token_idx, removed_gi

    kept = [
        e for i, e in enumerate(entities)
        if i not in to_remove
    ]

    # Cap observations to prevent unbounded growth.
    # Runs after filtering removed entities to avoid
    # wasted work on entities about to be discarded.
    # activity-log entities (from capture-tool-context)
    # keep newest 50; all others keep newest 200.
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
    max_iter = min(len(renames), 100) if renames else 0
    for _iter in range(max_iter):
        changed = False
        for k, v in list(renames.items()):
            if v in renames and renames[v] != v:
                renames[k] = renames[v]
                changed = True
        if not changed:
            break

    # Update relation references, drop self-refs + dupes
    # Copy-on-rename to avoid mutating input list
    updated_rels = []
    seen_rels = set()
    for r in relations:
        fr = r.get("from", "")
        to = r.get("to", "")
        new_fr = renames.get(fr, fr)
        new_to = renames.get(to, to)
        if new_fr == new_to:
            continue
        rel_key = (new_fr, new_to, r.get("relationType", ""))
        if rel_key not in seen_rels:
            seen_rels.add(rel_key)
            if new_fr != fr or new_to != to:
                r = dict(r, **{"from": new_fr, "to": new_to})
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
        text = f"{name} {etype} " + " ".join(obs_strs)
        # Filter stopwords + noise
        words = [
            w for w in _RE_WORDS.findall(text.lower())
            if _filter_token(w)
        ]
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
            sep = (",", ":")
            f.write('{"vectors":')
            json.dump(vectors, f, separators=sep)
            del vectors  # free before next section
            f.write(',"idf":')
            json.dump(
                {k: round(v, 4) for k, v in idf.items()},
                f, separators=sep,
            )
            del idf
            f.write(',"magnitudes":')
            json.dump(magnitudes, f, separators=sep)
            del magnitudes
            f.write(',"postings":')
            json.dump(dict(postings), f, separators=sep)
            del postings
            f.write(',"metadata":')
            json.dump(meta, f, separators=sep)
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


def _print_graph_stats(entities, n_relations, pruned,
                       merged, memory_dir):
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
          f"{n_relations} relations ===")
    for t, c in type_counts.most_common(10):
        print(f"  {t}: {c}")
    print(f"  avg observations/entity: {avg_obs:.1f}")
    if dates:
        print(f"  oldest: {min(dates)}  newest: {max(dates)}")
    print(f"  pruned={pruned}  merged={merged}  "
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
    # Stale lock detection: remove if older than 1 hour
    try:
        age = time.time() - os.path.getmtime(lock_path)
        if age > 3600:
            os.unlink(lock_path)
    except OSError:
        pass
    fd = None
    try:
        fd = open(lock_path, "a")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
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
    import gc
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

    # Free structures not needed for index build
    del others
    gc.collect()

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
            except BaseException:
                try:
                    os.unlink(rc_tmp)
                except OSError:
                    pass
    # Free recall + relations before index build
    n_relations = len(relations)
    del recall_counts, relations
    gc.collect()

    # Phase 2: TF-IDF index (writes its own temp file)
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
        entities, n_relations, pruned, merged, memory_dir
    )

    # Release large structures to reduce peak memory
    del entities


if __name__ == "__main__":
    proj = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get(
            "CLAUDE_PROJECT_DIR", os.getcwd()
        )
    )
    run(proj)

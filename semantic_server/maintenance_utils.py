"""Graph maintenance utilities: pruning and consolidation.

Extracted from maintenance.py to reduce bloat.
"""
import json
import math
import time
import os
from datetime import datetime, timedelta, timezone
from .text import normalize_name

# Configuration defaults (mirrored or passed from maintenance)
_GUARD_AGE_DAYS = 7
_MAX_CONSOLIDATE_ENTITIES = 50_000

try:
    from .config import MAIN_BRANCHES as _MAIN_BRANCHES
except ImportError:
    _MAIN_BRANCHES = frozenset({"main", "master", "trunk", "develop"})

def read_recall_counts(memory_dir):
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

def parse_iso_date(s):
    """Parse ISO 8601 to tz-aware datetime (assumes UTC if bare)."""
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def score_entity(entity, now_ts, recall_counts=None, cutoff_str=None, max_age_days=90):
    """Score: obs_count * recency * recall_boost."""
    obs_count = len(entity.get("observations", []))
    if obs_count == 0:
        return 0.0

    updated = entity.get("_updated", "")
    if not updated or (cutoff_str and updated < cutoff_str):
        days = max_age_days
    else:
        dt = parse_iso_date(updated)
        if not dt:
            days = max_age_days
        else:
            days = max(int((now_ts - dt.timestamp()) / 86400), 0)

    recency = 1.0 / (1.0 + days)
    score = obs_count * recency

    if recall_counts:
        rc = recall_counts.get(entity.get("name", ""), 0)
        if rc > 0:
            score *= (1.0 + math.log(rc))

    return score

def prune_entities(entities, relations, recall_counts=None, max_age_days=90, decay_threshold=0.1):
    """Remove low-score entities with zero inbound relations."""
    has_inbound = {r.get("to", "") for r in relations}
    now_ts = time.time()
    cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=max_age_days))
    cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    pruned_names = set()
    kept = []
    for e in entities:
        name = e.get("name", "")
        if not name.strip():
            continue
        if name in has_inbound:
            kept.append(e)
            continue
        score = score_entity(e, now_ts, recall_counts, cutoff_str, max_age_days)
        if score < decay_threshold:
            pruned_names.add(name)
        else:
            kept.append(e)

    kept_rels = [
        r for r in relations
        if r.get("from") not in pruned_names and r.get("to") not in pruned_names
    ]
    return kept, kept_rels, len(pruned_names)

def _safe_obs_dedup(observations):
    """Deduplicate observations preserving insertion order."""
    if not observations:
        return []
    seen = set()
    result = []
    for o in observations:
        key = (o if isinstance(o, str) else json.dumps(o, sort_keys=True))
        if key not in seen:
            seen.add(key)
            result.append(o)
    return result

def _can_merge(ent_i, ent_j, norm_i, norm_j, len_i, len_j, guard_cutoff, min_merge):
    if len_i > 2 * len_j or len_j > 2 * len_i:
        return False
    shorter = min(len_i, len_j)
    if shorter < min_merge and norm_i != norm_j:
        return False
    padded_i = f" {norm_i} "
    padded_j = f" {norm_j} "
    if not (norm_i == norm_j or padded_i in padded_j or padded_j in padded_i):
        return False
    bi = ent_i.get("_branch", "")
    bj = ent_j.get("_branch", "")
    if bi and bj and bi != bj and bi not in _MAIN_BRANCHES and bj not in _MAIN_BRANCHES:
        ci = ent_i.get("_created", "")
        cj = ent_j.get("_created", "")
        if ci and ci > guard_cutoff and cj and cj > guard_cutoff:
            return False
    return True

def consolidate(entities, relations, min_merge_name_len=4):
    """Merge entities with same type + overlapping names."""
    if len(entities) > _MAX_CONSOLIDATE_ENTITIES:
        return entities, relations, 0

    guard_cutoff = (datetime.now(timezone.utc) - timedelta(days=_GUARD_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = len(entities)
    keyed = []
    for i, e in enumerate(entities):
        norm = normalize_name(e.get("name", ""))
        etype = e.get("entityType", "")
        keyed.append((etype, norm, i))

    keyed.sort()
    merged_count = 0
    absorbed = set()
    renames = {}
    WINDOW = 20 if n < 5000 else 10
    total_comparisons = 0
    _MAX_COMPARISONS = 500_000

    for pos in range(n):
        etype_i, norm_i, idx_i = keyed[pos]
        if idx_i in absorbed or not norm_i.strip():
            continue

        ent_i = entities[idx_i]
        obs_dict_i = None
        len_i = len(norm_i)

        for ahead in range(1, WINDOW + 1):
            total_comparisons += 1
            if total_comparisons > _MAX_COMPARISONS:
                break
            j = pos + ahead
            if j >= n:
                break
            etype_j, norm_j, idx_j = keyed[j]
            if etype_j != etype_i:
                break
            if idx_j in absorbed or not norm_j.strip():
                continue

            ent_j = entities[idx_j]
            len_j = len(norm_j)
            if not _can_merge(ent_i, ent_j, norm_i, norm_j, len_i, len_j, guard_cutoff, min_merge_name_len):
                continue
            
            if obs_dict_i is None:
                obs_dict_i = _safe_obs_dedup(ent_i.get("observations", []))
                _seen_i = {(o if isinstance(o, str) else json.dumps(o, sort_keys=True)) for o in obs_dict_i}
            
            for o in ent_j.get("observations", []):
                key = (o if isinstance(o, str) else json.dumps(o, sort_keys=True))
                if key not in _seen_i:
                    _seen_i.add(key)
                    obs_dict_i.append(o)
            
            upd_j = ent_j.get("_updated", "")
            upd_i = ent_i.get("_updated", "")
            if upd_j and (not upd_i or upd_j > upd_i):
                ent_i["_updated"] = upd_j
            absorbed.add(idx_j)
            renames[ent_j.get("name", "")] = ent_i.get("name", "")
            merged_count += 1
        
        if total_comparisons > _MAX_COMPARISONS:
            sys.stderr.write(
                f"warn: consolidation cap reached after "
                f"{merged_count} merges, remaining entities skipped\n"
            )
            break
        if obs_dict_i is not None:
            ent_i["observations"] = list(obs_dict_i)

    kept = [e for i, e in enumerate(entities) if i not in absorbed]

    # Cap observations
    for e in kept:
        obs = e.get("observations", [])
        cap = 200
        if len(obs) > cap:
            e["observations"] = obs[-cap:]

    # Resolve transitive renames
    for k in list(renames):
        v = renames[k]
        while v in renames and renames[v] != v:
            v = renames[v]
        renames[k] = v

    updated_rels = []
    seen_rels = set()
    for r in relations:
        fr, to = r.get("from", ""), r.get("to", "")
        new_fr, new_to = renames.get(fr, fr), renames.get(to, to)
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
    """Add _branch/_created to new entities."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for e in entities:
        if "_branch" not in e:
            e["_branch"] = branch
        if "_created" not in e:
            e["_created"] = now
        if "_updated" not in e:
            # Always ensure _updated exists
            e["_updated"] = e.get("_updated", now)
    return entities

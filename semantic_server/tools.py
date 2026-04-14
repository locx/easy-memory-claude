"""Write operations + intelligence tools.

Includes: create, update, delete entities/relations,
create_decision, update_decision_outcome, graph_stats.
"""
import calendar
import os
import time
from collections import Counter

from .config import (
    MAX_ENTITIES_PER_CALL,
    MAX_GRAPH_BYTES,
    MAX_OBS_LENGTH,
    MAX_OBS_PER_CALL,
    MAX_RELATIONS_PER_CALL,
    get_current_branch,
    log_event,
    now_iso,
    session_stats,
)
from .graph import (
    _obs_dedup_key,
    append_jsonl,
    check_graph_size,
    invalidate_caches,
    invalidate_entity_cache_only,
    invalidate_relation_cache_only,
    load_graph_entities,
    load_graph_relations,
    rewrite_graph,
)
from .text import normalize_name, normalize_type

_DECISION_PREFIX = "decision: "


def _build_norm_index(existing_entities):
    """Pre-compute {normalized_name: original_name} for O(1) fuzzy lookup."""
    index = {}
    for name in existing_entities:
        norm = normalize_name(name)
        if norm and len(norm) >= 3:
            # Keep first occurrence on collision
            index.setdefault(norm, name)
    return index


def _find_similar_entity(name, norm_index):
    """O(1) fuzzy lookup against pre-computed normalized index."""
    norm = normalize_name(name)
    if not norm or len(norm) < 3:
        return None
    existing = norm_index.get(norm)
    if existing and existing != name:
        return existing
    return None


def create_entities(entities_input, memory_dir):
    """Create entities via append-only write."""
    if not isinstance(entities_input, list):
        return {"error": "entities must be a list"}
    if len(entities_input) > MAX_ENTITIES_PER_CALL:
        return {
            "error": f"Max {MAX_ENTITIES_PER_CALL} "
                     f"entities per call"
        }
    size_err = check_graph_size(memory_dir)
    if size_err:
        return size_err

    now = now_iso()
    branch = get_current_branch()
    new_entries = []

    for ent in entities_input:
        if not isinstance(ent, dict):
            continue
        name = ent.get("name", "")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name:
            continue
        etype = normalize_type(ent.get("entityType", ""))
        obs = ent.get("observations", [])
        if not isinstance(obs, list):
            obs = [str(obs)]
        obs = [
            o[:MAX_OBS_LENGTH] for o in obs
            if isinstance(o, str) and o.strip()
        ]

        new_entries.append({
            "type": "entity",
            "name": name,
            "entityType": etype,
            "observations": obs,
            "_branch": branch,
            "_created": now,
            "_updated": now,
        })

    if not new_entries:
        return {
            "created": 0,
            "message": "No valid entities",
        }

    # Fuzzy match: pre-normalize once, then O(1) lookups
    existing = load_graph_entities(memory_dir)
    norm_index = _build_norm_index(existing)
    similar_warnings = []
    for entry in new_entries:
        name = entry["name"]
        if name in existing:
            continue
        similar = _find_similar_entity(name, norm_index)
        if similar:
            similar_warnings.append(
                f"'{name}' similar to existing "
                f"'{similar}'"
            )

    if not append_jsonl(memory_dir, new_entries):
        return {
            "error": "Write failed (lock timeout)",
            "created": 0,
        }
    invalidate_entity_cache_only()

    names = [e["name"] for e in new_entries]
    session_stats["entities_created"] += len(new_entries)
    log_event(
        "CREATE",
        f"{len(new_entries)} entities: {names}",
    )
    result = {"created": len(new_entries)}
    if similar_warnings:
        result["similar_entities"] = similar_warnings
        result["hint"] = (
            "Consider using existing entity names "
            "or renaming to avoid duplicates"
        )
    return result


def create_relations(relations_input, memory_dir):
    """Create relations via append-only write."""
    if not isinstance(relations_input, list):
        return {"error": "relations must be a list"}
    if len(relations_input) > MAX_RELATIONS_PER_CALL:
        return {
            "error": f"Max {MAX_RELATIONS_PER_CALL} "
                     f"relations per call"
        }
    size_err = check_graph_size(memory_dir)
    if size_err:
        return size_err

    seen = set()
    new_entries = []
    for rel in relations_input:
        if not isinstance(rel, dict):
            continue
        fr = rel.get("from", "")
        to = rel.get("to", "")
        rt = rel.get("relationType", "")
        if not fr or not to \
                or not isinstance(fr, str) \
                or not isinstance(to, str):
            continue
        if fr == to:
            continue
        key = (fr, to, rt)
        if key in seen:
            continue
        seen.add(key)
        new_entries.append({
            "type": "relation",
            "from": fr,
            "to": to,
            "relationType": rt,
        })

    if not new_entries:
        return {
            "created": 0,
            "message": "No new relations",
        }

    if not append_jsonl(memory_dir, new_entries):
        return {
            "error": "Write failed (lock timeout)",
            "created": 0,
        }
    invalidate_relation_cache_only()

    session_stats["relations_created"] += len(new_entries)
    descs = [
        f"{e['from']}--{e['relationType']}-->"
        f"{e['to']}"
        for e in new_entries[:5]
    ]
    log_event(
        "RELATE",
        f"{len(new_entries)} relations: "
        + ", ".join(descs),
    )
    return {"created": len(new_entries)}


_NEG_WORDS = frozenset({
    "not", "no", "never", "dont", "doesnt",
    "removed", "deprecated", "reverted",
    "disabled", "dropped", "replaced",
    "incorrect", "wrong", "broken",
})


def _detect_contradictions(new_obs, existing_obs):
    conflicts = []
    for new_o in new_obs:
        new_lower = set(new_o.lower().split())
        new_has_neg = bool(new_lower & _NEG_WORDS)
        for exist_o in existing_obs:
            if not isinstance(exist_o, str):
                continue
            exist_lower = set(exist_o.lower().split())
            exist_has_neg = bool(exist_lower & _NEG_WORDS)
            if new_has_neg != exist_has_neg:
                shared = (new_lower & exist_lower) - {
                    "the", "a", "is", "are", "was", "to", "in", "for", "and", "of", "it", "this", "that", "with"
                }
                if len(shared) >= 3:
                    conflicts.append({"new": new_o[:100], "existing": exist_o[:100]})
            if len(conflicts) >= 3:
                break
        if len(conflicts) >= 3:
            break
    return conflicts


def add_observations(entity_name, observations, memory_dir,
                     _retry=False):
    """Add observations to an existing entity.

    Mtime guard detects concurrent writes — retries once.
    """
    if not isinstance(entity_name, str) \
            or not entity_name:
        return {"error": "entity name required"}
    if not isinstance(observations, list):
        return {"error": "observations must be a list"}
    if len(observations) > MAX_OBS_PER_CALL:
        return {
            "error": f"Max {MAX_OBS_PER_CALL} "
                     f"observations per call"
        }
    size_err = check_graph_size(memory_dir)
    if size_err:
        return size_err

    new_obs = [
        o[:MAX_OBS_LENGTH] for o in observations
        if isinstance(o, str) and o.strip()
    ]
    if not new_obs:
        return {
            "added": 0,
            "message": "No valid observations",
        }

    now = now_iso()

    graph_path = os.path.join(memory_dir, "graph.jsonl")
    try:
        pre_mtime = os.path.getmtime(graph_path)
    except OSError:
        pre_mtime = 0.0

    # Must use load_graph_entities (not raw cache) for
    # cache coherence after create_entities in same session
    cached = load_graph_entities(memory_dir)
    if entity_name not in cached:
        return {
            "error": (
                f"Entity '{entity_name}' not found"
            ),
        }
    info = cached[entity_name]
    cur_obs_keys = {
        _obs_dedup_key(o)
        for o in info.get("observations", [])
    }
    new_obs = [
        o for o in new_obs
        if _obs_dedup_key(o) not in cur_obs_keys
    ]
    if not new_obs:
        return {
            "added": 0,
            "message": "All observations "
                       "already exist",
        }
    etype = info.get("entityType", "")
    created = info.get("_created", now)
    conflicts = _detect_contradictions(new_obs, info.get("observations", []))

    # Check for concurrent delete/update between read and write
    try:
        post_mtime = os.path.getmtime(graph_path)
    except OSError:
        post_mtime = 0.0
    if post_mtime != pre_mtime and not _retry:
        invalidate_caches()
        return add_observations(
            entity_name, observations, memory_dir,
            _retry=True,
        )

    if not append_jsonl(memory_dir, [{
        "type": "entity",
        "name": entity_name,
        "entityType": etype,
        "observations": new_obs,
        "_created": created,
        "_updated": now,
    }]):
        return {
            "error": "Write failed (lock timeout)",
            "added": 0,
        }
    invalidate_entity_cache_only()

    total = len(new_obs)
    session_stats["observations_added"] += total
    log_event(
        "ADD_OBS",
        f'entity="{entity_name}" added={total}',
    )
    result = {"added": total}
    if conflicts:
        result["conflicts"] = conflicts
        result["warning"] = (
            f"{len(conflicts)} potential "
            f"contradiction(s) detected"
        )
    return result


def delete_entities(entity_names, memory_dir,
                    _retry=False):
    """Delete entities and cascade-remove relations.

    Mtime guard detects concurrent writes — retries once.
    """
    if not isinstance(entity_names, list):
        return {"error": "entity_names must be a list"}

    graph_path = os.path.join(memory_dir, "graph.jsonl")
    try:
        pre_mtime = os.path.getmtime(graph_path)
    except OSError:
        pre_mtime = 0.0

    entities = load_graph_entities(memory_dir)
    to_delete = {
        n for n in entity_names
        if isinstance(n, str) and n in entities
    }
    if not to_delete:
        return {
            "deleted": 0,
            "message": "No matching entities found",
        }

    remaining = {
        k: v for k, v in entities.items()
        if k not in to_delete
    }

    rels = load_graph_relations(memory_dir)
    kept_rels = [
        r for r in rels
        if r.get("from") not in to_delete
        and r.get("to") not in to_delete
    ]

    try:
        post_mtime = os.path.getmtime(graph_path)
    except OSError:
        post_mtime = 0.0
    if post_mtime != pre_mtime and not _retry:
        invalidate_caches()
        return delete_entities(
            entity_names, memory_dir, _retry=True,
        )

    try:
        rewrite_graph(memory_dir, remaining, kept_rels)
    except OSError:
        return {
            "error": "Write failed (lock timeout)",
            "deleted": 0,
        }
    # rewrite_graph already calls invalidate_caches()

    n_del = len(to_delete)
    n_rels = len(rels) - len(kept_rels)
    session_stats["entities_deleted"] += n_del
    log_event(
        "DELETE",
        f"{n_del} entities: {list(to_delete)[:5]}"
        f", {n_rels} relations cascaded",
    )
    return {
        "deleted": n_del,
        "relations_removed": n_rels,
    }


def _build_decision_obs(args):
    """Cleanly extract strings into an observation list for decisions."""
    rationale = args.get("rationale", "")
    obs = [f"Rationale: {rationale[:MAX_OBS_LENGTH]}"]

    alts = args.get("alternatives", [])
    if isinstance(alts, list):
        for alt in alts[:10]:
            if isinstance(alt, str) and alt.strip():
                obs.append(f"Alternative rejected: {alt[:MAX_OBS_LENGTH]}")

    scope = args.get("scope", "")
    if isinstance(scope, str) and scope.strip():
        obs.append(f"Scope: {scope[:MAX_OBS_LENGTH]}")

    chosen = args.get("chosen", "")
    if isinstance(chosen, str) and chosen.strip():
        obs.append(f"Chosen: {chosen[:MAX_OBS_LENGTH]}")

    outcome = args.get("outcome", "pending")
    if outcome not in (
        "pending", "successful", "failed", "revised",
        "adopted", "rejected", "deferred", "obsolete",
    ):
        outcome = "pending"
    obs.append(f"Outcome: {outcome}")
    return obs, outcome

def create_decision(args, memory_dir):
    """Create a structured decision entity with relations."""
    if not isinstance(args, dict):
        return {"error": "arguments must be a dict"}

    title = args.get("title", "")
    if not title or not isinstance(title, str):
        return {"error": "title is required"}

    rationale = args.get("rationale", "")
    if not rationale or not isinstance(rationale, str):
        return {"error": "rationale is required"}

    obs, outcome = _build_decision_obs(args)

    entity_name = f"{_DECISION_PREFIX}{title}"
    result = create_entities(
        [{
            "name": entity_name,
            "entityType": "decision",
            "observations": obs,
        }],
        memory_dir,
    )
    if "error" in result:
        return result

    related = args.get("related_entities", [])
    rel_result = None
    if isinstance(related, list) and related:
        rel_entries = []
        for target in related[:10]:
            if isinstance(target, str) \
                    and target.strip():
                rel_entries.append({
                    "from": entity_name,
                    "to": target,
                    "relationType": "decided-for",
                })
        if rel_entries:
            rel_result = create_relations(
                rel_entries, memory_dir
            )

    log_event(
        "DECISION",
        f'"{title}" outcome={outcome}',
    )
    resp = {
        "created": result.get("created", 0),
        "decision": entity_name,
        "outcome": outcome,
    }
    if rel_result and "error" in rel_result:
        resp["relations_error"] = rel_result["error"]
    elif rel_result:
        resp["relations_created"] = rel_result.get(
            "created", 0
        )
    return resp


def update_decision_outcome(args, memory_dir):
    """Update a decision's outcome and record lesson."""
    if not isinstance(args, dict):
        return {"error": "arguments must be a dict"}

    title = args.get("title", "")
    if not title or not isinstance(title, str):
        return {"error": "title is required"}

    outcome = args.get("outcome", "")
    _valid_outcomes = (
        "successful", "failed", "revised",
        "adopted", "rejected", "deferred",
        "obsolete",
    )
    if outcome not in _valid_outcomes:
        return {
            "error": "outcome must be one of: "
                     + ", ".join(_valid_outcomes)
        }

    lesson = args.get("lesson", "")

    # Try both prefixed and unprefixed names
    if title.startswith(_DECISION_PREFIX):
        candidates = [title]
    else:
        candidates = [
            f"{_DECISION_PREFIX}{title}",
            title,
        ]

    new_obs = [f"Outcome: {outcome}"]
    if isinstance(lesson, str) and lesson.strip():
        new_obs.append(
            f"Lesson: {lesson[:MAX_OBS_LENGTH]}"
        )

    for entity_name in candidates:
        result = add_observations(
            entity_name, new_obs, memory_dir
        )
        if "error" not in result:
            log_event(
                "OUTCOME",
                f'"{title}" -> {outcome}'
                + (f" lesson: {lesson[:80]}"
                   if lesson else ""),
            )
            return {
                "updated": entity_name,
                "outcome": outcome,
                "observations_added": result.get(
                    "added", 0
                ),
            }

    return {
        "error": f"Decision '{title}' not found",
    }


def _file_info(path):
    """Return (mtime_iso, size_kb) or (None, 0)."""
    try:
        mt = os.path.getmtime(path)
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(mt)
        ), os.path.getsize(path) // 1024
    except OSError:
        return None, 0


def graph_stats(memory_dir):
    """Return graph health and session stats."""
    entities = load_graph_entities(memory_dir)
    relations = load_graph_relations(memory_dir)

    type_counts = Counter(
        normalize_type(info.get("entityType", "unknown")) or "unknown"
        for info in entities.values()
    )

    branch_counts = Counter(
        info.get("_branch", "unknown")
        for info in entities.values()
    )

    _, graph_kb = _file_info(
        os.path.join(memory_dir, "graph.jsonl")
    )
    index_age, index_kb = _file_info(
        os.path.join(memory_dir, "tfidf_index.json")
    )
    last_maint, _ = _file_info(
        os.path.join(memory_dir, ".last-maintenance")
    )

    try:
        from .recall import recall_counts as _rc
        top_recall = sorted(
            (
                (n, c) for n, c in _rc.items()
                if isinstance(c, (int, float))
            ),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
    except Exception:
        top_recall = []

    n_pending = sum(
        1 for info in entities.values()
        if info.get("entityType") == "decision"
        and not any(
            isinstance(o, str)
            and o.startswith("Outcome: ")
            and not o.startswith("Outcome: pending")
            for o in info.get("observations", [])
        )
    )

    result = {
        "entities": len(entities),
        "relations": len(relations),
        "graph_size_kb": graph_kb,
        "index_size_kb": index_kb,
        "index_built": index_age or "not built",
        "last_maintenance": last_maint or "never",
        "type_breakdown": dict(
            type_counts.most_common(20)
        ),
        "branch_distribution": dict(
            branch_counts.most_common(10)
        ),
        "current_branch": get_current_branch(),
        "top_by_recall": [
            {"name": n, "recalls": c}
            for n, c in top_recall
        ],
        "pending_decisions": n_pending,
        "session": dict(session_stats),
    }

    log_event(
        "STATS",
        f"{len(entities)} entities, "
        f"{len(relations)} relations, "
        f"{n_pending} pending decisions",
    )
    return result


def list_decisions(memory_dir, stale_days=None):
    """List all decisions with status.

    Args:
        stale_days: If set, return only pending decisions
            older than this many days (stale hygiene).
            stale_days=0 returns all pending decisions.
    """
    if stale_days is not None:
        try:
            stale_days = max(0, float(stale_days))
        except (TypeError, ValueError):
            return {"error": "stale_days must be a number"}

    entities = load_graph_entities(memory_dir)
    now_ts = time.time()
    decisions = []
    for name, info in entities.items():
        if info.get("entityType") != "decision":
            continue
        obs = info.get("observations", [])
        outcome = "pending"
        for o in obs:
            if isinstance(o, str) \
                    and o.startswith("Outcome: "):
                outcome = o[9:]
        updated = info.get("_updated", "")

        if stale_days is not None:
            if outcome != "pending":
                continue
            if updated:
                try:
                    ut = calendar.timegm(time.strptime(
                        updated[:19], "%Y-%m-%dT%H:%M:%S"
                    ))
                    age = (now_ts - ut) / 86400
                    if age < stale_days:
                        continue
                except (ValueError, OverflowError):
                    pass

        display = name
        if display.startswith(_DECISION_PREFIX):
            display = display[len(_DECISION_PREFIX):]
        decisions.append({
            "title": display,
            "outcome": outcome,
            "observations": obs[:5],
            "updated": updated,
        })
    # Newest first by default; oldest first for stale hygiene
    decisions.sort(
        key=lambda d: d["updated"],
        reverse=(stale_days is None),
    )
    return {"decisions": decisions, "total": len(decisions)}


def remove_observations(entity_name, observations,
                        memory_dir):
    """Remove specific observations from an entity."""
    if not isinstance(entity_name, str) \
            or not entity_name:
        return {"error": "entity name required"}
    if not isinstance(observations, list):
        return {"error": "observations must be a list"}

    entities = load_graph_entities(memory_dir)
    if entity_name not in entities:
        return {"error": f"Entity '{entity_name}' not found"}

    info = entities[entity_name]
    cur_obs = info.get("observations", [])
    to_remove = {_obs_dedup_key(o) for o in observations}
    kept = [o for o in cur_obs
            if _obs_dedup_key(o) not in to_remove]
    removed = len(cur_obs) - len(kept)
    if removed == 0:
        return {"removed": 0,
                "message": "No matching observations"}

    updated = dict(entities)
    updated[entity_name] = {
        **info,
        "observations": kept,
        "_updated": now_iso(),
    }
    rels = load_graph_relations(memory_dir)
    try:
        rewrite_graph(memory_dir, updated, rels)
    except OSError:
        return {"error": "Write failed (lock timeout)"}
    # rewrite_graph already calls invalidate_caches()
    log_event("REMOVE_OBS",
              f'entity="{entity_name}" removed={removed}')
    return {"removed": removed}


def rename_entity(old_name, new_name, memory_dir):
    """Rename an entity, updating all relation references.

    Drops self-loops and dedups duplicate (from, to, type) edges
    that can arise when both old_name and new_name appear in the
    same relation.
    """
    if not old_name or not new_name:
        return {"error": "old_name and new_name required"}
    if old_name == new_name:
        return {"error": "names are identical"}

    entities = load_graph_entities(memory_dir)
    if old_name not in entities:
        return {"error": f"Entity '{old_name}' not found"}
    if new_name in entities:
        return {"error": f"Entity '{new_name}' already exists"}

    updated = {}
    for name, info in entities.items():
        if name == old_name:
            updated[new_name] = {
                **info, "_updated": now_iso(),
            }
        else:
            updated[name] = info

    rels = load_graph_relations(memory_dir)
    fixed_rels = []
    seen_rels = set()
    dropped_self_loops = 0
    dropped_dups = 0
    relations_updated = 0
    for r in rels:
        orig_fr = r.get("from", "")
        orig_to = r.get("to", "")
        fr = new_name if orig_fr == old_name else orig_fr
        to = new_name if orig_to == old_name else orig_to
        if fr == to:
            dropped_self_loops += 1
            continue
        rt = r.get("relationType", "")
        key = (fr, to, rt)
        if key in seen_rels:
            dropped_dups += 1
            continue
        seen_rels.add(key)
        fixed_rels.append({
            "from": fr, "to": to, "relationType": rt,
        })
        if orig_fr == old_name or orig_to == old_name:
            relations_updated += 1

    try:
        rewrite_graph(memory_dir, updated, fixed_rels)
    except OSError:
        return {"error": "Write failed (lock timeout)"}
    # rewrite_graph already calls invalidate_caches()
    log_event("RENAME",
              f'"{old_name}" -> "{new_name}"')
    resp = {
        "renamed": old_name,
        "to": new_name,
        "relations_updated": relations_updated,
    }
    if dropped_self_loops:
        resp["self_loops_removed"] = dropped_self_loops
    if dropped_dups:
        resp["duplicate_relations_merged"] = dropped_dups
    return resp

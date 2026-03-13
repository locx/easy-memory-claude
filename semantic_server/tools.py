"""Write operations: create, update, delete entities and relations."""
from .config import (
    MAX_ENTITIES_PER_CALL,
    MAX_GRAPH_BYTES,
    MAX_OBS_LENGTH,
    MAX_OBS_PER_CALL,
    MAX_RELATIONS_PER_CALL,
    now_iso,
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
from .cache import entity_cache


def create_entities(entities_input, memory_dir):
    """Create or merge entities into the graph.

    If an entity with same name exists, a duplicate entry
    with merged observations is appended (O(1)). Maintenance
    consolidation deduplicates on next run. This avoids O(n)
    full graph rewrite on every merge call.
    """
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
    # Skip full graph load on cold cache —
    # append unconditionally, let maintenance dedup.
    cache_warm = entity_cache["data"] is not None
    existing = load_graph_entities(memory_dir) \
        if cache_warm else {}
    new_entries = []

    for ent in entities_input:
        if not isinstance(ent, dict):
            continue
        name = ent.get("name", "")
        if not name or not isinstance(name, str):
            continue
        etype = ent.get("entityType", "")
        if not isinstance(etype, str):
            etype = str(etype)
        obs = ent.get("observations", [])
        if not isinstance(obs, list):
            obs = [str(obs)]
        obs = [
            o[:MAX_OBS_LENGTH] for o in obs
            if isinstance(o, str) and o.strip()
        ]

        if cache_warm and name in existing:
            # Merge: deduplicate new obs, append entity
            # line. Maintenance consolidation merges later.
            cur = existing[name]
            cur_obs_list = cur.get("observations", [])
            cur_obs_keys = {
                _obs_dedup_key(o) for o in cur_obs_list
            }
            new_obs = []
            for o in obs:
                k = _obs_dedup_key(o)
                if k not in cur_obs_keys:
                    new_obs.append(o)
                    cur_obs_keys.add(k)
            merged = list(cur_obs_list) + new_obs
            new_entries.append({
                "type": "entity",
                "name": name,
                "entityType": cur.get(
                    "entityType", etype
                ),
                "observations": merged,
                "_created": cur.get("_created", now),
                "_updated": now,
            })
        else:
            new_entries.append({
                "type": "entity",
                "name": name,
                "entityType": etype,
                "observations": obs,
                "_created": now,
                "_updated": now,
            })

    if not new_entries:
        return {"created": 0, "message": "No valid entities"}

    # Append-only for all cases (new + merge).
    # Duplicate names from merges are resolved by
    # _parse_graph_file() on next cache load and by
    # maintenance consolidation on next run.
    append_jsonl(memory_dir, new_entries)
    invalidate_entity_cache_only()
    return {"created": len(new_entries)}


def create_relations(relations_input, memory_dir):
    """Create relations, skipping duplicates."""
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

    existing_rels = load_graph_relations(memory_dir)
    seen = {
        (r["from"], r["to"], r.get("relationType", ""))
        for r in existing_rels
    }
    new_entries = []
    for rel in relations_input:
        if not isinstance(rel, dict):
            continue
        fr = rel.get("from", "")
        to = rel.get("to", "")
        rt = rel.get("relationType", "")
        if not fr or not to or not isinstance(fr, str) \
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
            "message": "No new relations (all duplicates "
                       "or invalid)",
        }

    append_jsonl(memory_dir, new_entries)
    invalidate_relation_cache_only()
    return {"created": len(new_entries)}


def add_observations(entity_name, observations, memory_dir):
    """Add observations to an existing entity."""
    if not isinstance(entity_name, str) or not entity_name:
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

    entities = load_graph_entities(memory_dir)
    if entity_name not in entities:
        return {
            "error": f"Entity '{entity_name}' not found"
        }

    now = now_iso()
    info = entities[entity_name]
    cur_obs_list = info.get("observations", [])
    cur_obs_keys = {
        _obs_dedup_key(o) for o in cur_obs_list
    }
    new_obs = []
    for o in observations:
        if not isinstance(o, str) or not o.strip():
            continue
        o = o[:MAX_OBS_LENGTH]
        if o not in cur_obs_keys:
            new_obs.append(o)
            cur_obs_keys.add(o)

    if not new_obs:
        return {
            "added": 0,
            "message": "All observations already exist",
        }

    # Append-only: write a duplicate entity line with
    # just the new observations. Maintenance consolidation
    # merges duplicates on next run. Avoids O(n) full
    # graph rewrite for each add_observations call.
    append_jsonl(memory_dir, [{
        "type": "entity",
        "name": entity_name,
        "entityType": info.get("entityType", ""),
        "observations": new_obs,
        "_created": info.get("_created", now),
        "_updated": now,
    }])
    invalidate_entity_cache_only()
    return {"added": len(new_obs)}


def delete_entities(entity_names, memory_dir):
    """Delete entities and cascade-remove their relations."""
    if not isinstance(entity_names, list):
        return {"error": "entity_names must be a list"}

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

    # Build a copy — never mutate cached dict before
    # disk write succeeds
    remaining = {
        k: v for k, v in entities.items()
        if k not in to_delete
    }

    rels = load_graph_relations(memory_dir)
    kept_rels = [
        r for r in rels
        if r["from"] not in to_delete
        and r["to"] not in to_delete
    ]

    rewrite_graph(memory_dir, remaining, kept_rels)
    invalidate_caches()
    return {
        "deleted": len(to_delete),
        "relations_removed": len(rels) - len(kept_rels),
    }

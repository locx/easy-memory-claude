#!/usr/bin/env python3
"""Smart recall: score-ranked entity summary for SessionStart.

Replaces the naive 30-entity dump with top-N entities ranked by
relevance (obs_count * recency * recall_boost), each with inline
1-hop relations. Also surfaces pending decisions and graph stats.

Standalone script — no dependency on semantic_server package.
Uses bytecode caching (.pyc) for fast repeated invocations.

Usage: python3 smart_recall.py <memory_dir>
"""
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone


def _parse_iso_days_ago(ts, now_ts):
    """Return days since timestamp, or 999 if unparseable."""
    if not ts or not isinstance(ts, str):
        return 999
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(int((now_ts - dt.timestamp()) / 86400), 0)
    except Exception:
        return 999


def _load_recall_counts(memory_dir):
    """Load recall frequency counts."""
    rc_path = os.path.join(memory_dir, "recall_counts.json")
    try:
        with open(rc_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _load_graph(memory_dir):
    """Single-pass graph load into entities + relations."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    entities = {}
    relations = []
    try:
        with open(graph_path, encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "entity":
                        name = obj.get("name", "")
                        if not name:
                            continue
                        if name in entities:
                            prev = entities[name]
                            # Use try/except for non-hashable
                            # observations (dicts, lists)
                            try:
                                seen = set(
                                    prev["observations"]
                                )
                            except TypeError:
                                seen = {
                                    str(o) for o
                                    in prev["observations"]
                                }
                            for o in obj.get(
                                "observations", []
                            ):
                                if not isinstance(o, str):
                                    continue
                                if o not in seen:
                                    prev["observations"].append(o)
                                    seen.add(o)
                            new_u = obj.get("_updated", "")
                            if new_u and (
                                not prev["_updated"]
                                or new_u > prev["_updated"]
                            ):
                                prev["_updated"] = new_u
                        else:
                            obs = obj.get("observations", [])
                            entities[name] = {
                                "entityType": obj.get(
                                    "entityType", ""
                                ),
                                "observations": [
                                    o for o in obs
                                    if isinstance(o, str)
                                ],
                                "_created": obj.get(
                                    "_created", ""
                                ),
                                "_updated": obj.get(
                                    "_updated", ""
                                ),
                            }
                    elif t == "relation":
                        fr = obj.get("from", "")
                        to = obj.get("to", "")
                        rt = obj.get("relationType", "")
                        if fr and to:
                            relations.append(
                                (fr, to, rt)
                            )
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass
    return entities, relations


def _score_entity(info, now_ts, recall_counts, name):
    """Score: obs_count / (1 + days_stale) * recall_boost."""
    obs = info.get("observations", [])
    obs_count = len(obs)
    if obs_count == 0:
        return 0.0
    updated = info.get("_updated") or info.get(
        "_created", ""
    )
    days = _parse_iso_days_ago(updated, now_ts)
    recency = 1.0 / (1.0 + days)
    score = obs_count * recency
    rc = recall_counts.get(name, 0)
    if isinstance(rc, (int, float)) and rc > 0:
        score *= (1.0 + math.log(rc))
    return score


def _build_adjacency(relations):
    """Build outbound adjacency for 1-hop relation display."""
    adj = defaultdict(list)
    for fr, to, rt in relations:
        adj[fr].append((to, rt))
        adj[to].append((fr, rt))
    return adj


def _format_relations(name, adj, max_rels=3):
    """Format 1-hop relations as compact string."""
    neighbors = adj.get(name, [])
    if not neighbors:
        return ""
    # Deduplicate
    seen = set()
    parts = []
    for target, rt in neighbors:
        key = (target, rt)
        if key not in seen and len(parts) < max_rels:
            seen.add(key)
            parts.append(f"{rt}->{target}" if rt
                         else target)
    if not parts:
        return ""
    extra = len(neighbors) - len(parts)
    suffix = f" +{extra} more" if extra > 0 else ""
    return " | " + ", ".join(parts) + suffix


def _pick_best_observation(obs_list, max_len=120):
    """Pick the most informative observation (not a timestamp)."""
    for obs in reversed(obs_list):
        if not obs:
            continue
        # Skip pure timestamp activity logs
        if obs.startswith("[20") and "] " in obs[:30]:
            stripped = obs[obs.index("] ") + 2:]
            if stripped.startswith(("Edited ", "Ran: ",
                                    "Created/wrote ")):
                continue
        return obs[:max_len]
    return obs_list[-1][:max_len] if obs_list else ""


def _count_pending_decisions(entities):
    """Count decisions with outcome: pending."""
    count = 0
    pending = []
    for name, info in entities.items():
        if info.get("entityType") != "decision":
            continue
        obs = info.get("observations", [])
        has_outcome = any(
            o.startswith("Outcome: ")
            and not o.startswith("Outcome: pending")
            for o in obs if isinstance(o, str)
        )
        if not has_outcome:
            count += 1
            if len(pending) < 2:
                # Strip "decision: " prefix for display
                display = name
                if display.lower().startswith("decision: "):
                    display = display[10:]
                pending.append(display)
    return count, pending


def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    memory_dir = sys.argv[1]

    graph_path = os.path.join(memory_dir, "graph.jsonl")
    if not os.path.exists(graph_path) \
            or os.path.getsize(graph_path) == 0:
        print(
            "Memory graph is empty. Use create_entities "
            "or create_decision to build knowledge."
        )
        return

    entities, relations = _load_graph(memory_dir)
    if not entities:
        print(
            "Memory graph is empty. Use create_entities "
            "or create_decision to build knowledge."
        )
        return

    recall_counts = _load_recall_counts(memory_dir)
    now_ts = time.time()

    # Score all entities, skip activity-logs
    scored = []
    type_counts = defaultdict(int)
    for name, info in entities.items():
        etype = info.get("entityType", "unknown")
        type_counts[etype] += 1
        if etype == "activity-log":
            continue
        score = _score_entity(
            info, now_ts, recall_counts, name
        )
        if score > 0:
            scored.append((score, name, info))

    scored.sort(reverse=True)
    adj = _build_adjacency(relations)

    # Top 5 entities with observations + relations
    top_n = min(5, len(scored))
    if top_n > 0:
        print(f"=== Top Memory ({top_n} most relevant) ===")
        for score, name, info in scored[:top_n]:
            etype = info.get("entityType", "")
            obs = info.get("observations", [])
            best_obs = _pick_best_observation(obs)
            rels = _format_relations(name, adj)
            type_tag = f" ({etype})" if etype else ""
            print(
                f"  {name}{type_tag}: "
                f"{best_obs}{rels}"
            )

    # Pending decisions
    n_pending, pending_names = _count_pending_decisions(
        entities
    )
    if n_pending > 0:
        print(f"\n  Pending decisions ({n_pending}):")
        for d in pending_names:
            print(f"    - {d}")
        shown = len(pending_names)
        if n_pending > shown:
            print(
                f"    ... and {n_pending - shown} more"
            )

    # One-liner stats
    n_ent = len(entities)
    n_rel = len(relations)
    n_decisions = type_counts.get("decision", 0)
    n_warnings = type_counts.get("file-warning", 0)

    # Last maintenance time
    marker = os.path.join(memory_dir, ".last-maintenance")
    maint_ago = ""
    try:
        age_s = time.time() - os.path.getmtime(marker)
        if age_s < 3600:
            maint_ago = f"{int(age_s / 60)}m ago"
        elif age_s < 86400:
            maint_ago = f"{int(age_s / 3600)}h ago"
        else:
            maint_ago = f"{int(age_s / 86400)}d ago"
    except OSError:
        maint_ago = "never"

    stats_parts = [
        f"{n_ent} entities",
        f"{n_rel} relations",
    ]
    if n_decisions:
        stats_parts.append(f"{n_decisions} decisions")
    if n_warnings:
        stats_parts.append(f"{n_warnings} warnings")
    stats_parts.append(f"maintained {maint_ago}")
    print(f"\nMemory: {' | '.join(stats_parts)}")

    # Tool reminder
    print(
        "Tools: semantic_search_memory | "
        "traverse_relations | create_decision | "
        "graph_stats"
    )


if __name__ == "__main__":
    main()

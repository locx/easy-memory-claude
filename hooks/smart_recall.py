#!/usr/bin/env python3
"""Smart recall: score-ranked entity summary for SessionStart.

Top-N entities ranked by obs_count * recency * recall_boost,
with inline 1-hop relations. Surfaces pending decisions and
graph stats. CLI-only (hooks don't fire in VSCode — CLAUDE.md
handles that path).

Standalone — no dependency on semantic_server package.

Usage: python3 smart_recall.py <memory_dir>
"""
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

_MAIN_BRANCHES = frozenset({
    "main", "master", "trunk", "develop",
})


def _read_git_head(project_dir):
    """Read branch from .git/HEAD."""
    git_head = os.path.join(project_dir, ".git", "HEAD")
    try:
        with open(git_head) as f:
            content = f.read(256).strip()
        if content.startswith("ref: refs/heads/"):
            return content[16:]
        if content.startswith("ref: "):
            return content[5:].rsplit("/", 1)[-1]
        return content[:12] if len(content) >= 8 else ""
    except OSError:
        return ""


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
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


_MAX_ENTITY_COUNT = 100_000
_MAX_LINE_LEN = 10_000_000
_PARSE_TIME_BUDGET = 10.0


def _load_graph(memory_dir):
    """Single-pass graph load into entities + relations."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    entities = {}
    relations = []
    rel_seen = set()
    deadline = time.monotonic() + _PARSE_TIME_BUDGET
    line_count = 0
    try:
        with open(graph_path, encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                line_count += 1
                if len(line) > _MAX_LINE_LEN:
                    continue
                if line_count % 1000 == 0:
                    if time.monotonic() > deadline:
                        break
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
                            seen = set(
                                prev["observations"]
                            )
                            for o in obj.get(
                                "observations", []
                            ):
                                if (isinstance(o, str)
                                        and o not in seen):
                                    prev[
                                        "observations"
                                    ].append(o)
                                    seen.add(o)
                            new_u = obj.get(
                                "_updated", ""
                            )
                            if new_u and (
                                not prev["_updated"]
                                or new_u
                                > prev["_updated"]
                            ):
                                prev["_updated"] = new_u
                            b = obj.get("_branch", "")
                            if b and not prev.get(
                                "_branch"
                            ):
                                prev["_branch"] = b
                        else:
                            if (len(entities)
                                    >= _MAX_ENTITY_COUNT):
                                continue
                            obs = obj.get(
                                "observations", []
                            )
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
                                "_branch": obj.get(
                                    "_branch", ""
                                ),
                            }
                    elif t == "relation":
                        fr = obj.get("from", "")
                        to = obj.get("to", "")
                        rt = obj.get(
                            "relationType", ""
                        )
                        rk = (fr, to, rt)
                        if (fr and to
                                and rk not in rel_seen):
                            rel_seen.add(rk)
                            relations.append(
                                (fr, to, rt)
                            )
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass
    return entities, relations


def _score_entity(info, now_ts, recall_counts, name,
                  current_branch):
    """Score: obs_count * recency * recall * branch."""
    obs = info.get("observations", [])
    if not obs:
        return 0.0
    updated = (info.get("_updated")
               or info.get("_created", ""))
    days = _parse_iso_days_ago(updated, now_ts)
    score = len(obs) / (1.0 + days)
    rc = recall_counts.get(name, 0)
    if isinstance(rc, (int, float)) and rc > 0:
        score *= (1.0 + math.log(rc))
    entity_branch = info.get("_branch", "")
    if (entity_branch and current_branch
            and entity_branch != current_branch):
        score *= (0.95 if entity_branch
                  in _MAIN_BRANCHES else 0.85)
    return score


def _build_adjacency(relations):
    """Build bidirectional adjacency."""
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
    seen = set()
    parts = []
    for target, rt in neighbors:
        key = (target, rt)
        if key not in seen and len(parts) < max_rels:
            seen.add(key)
            parts.append(
                f"{rt}->{target}" if rt else target
            )
    if not parts:
        return ""
    extra = len(neighbors) - len(parts)
    suffix = f" +{extra} more" if extra > 0 else ""
    return " | " + ", ".join(parts) + suffix


def _pick_best_observation(obs_list, max_len=120):
    """Pick most informative observation."""
    for obs in reversed(obs_list):
        if not obs:
            continue
        if obs.startswith("[20") and "] " in obs[:30]:
            stripped = obs[obs.index("] ") + 2:]
            if stripped.startswith(
                ("Edited ", "Ran: ", "Created/wrote ")
            ):
                continue
        return obs[:max_len]
    return obs_list[-1][:max_len] if obs_list else ""


def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    memory_dir = sys.argv[1]
    project_dir = os.path.dirname(memory_dir)
    current_branch = _read_git_head(project_dir) or ""

    entities, relations = _load_graph(memory_dir)
    if not entities:
        print("Memory graph is empty.")
        return

    recall_counts = _load_recall_counts(memory_dir)
    now_ts = time.time()

    # Score entities
    scored = []
    type_counts = defaultdict(int)
    for name, info in entities.items():
        etype = info.get("entityType", "unknown")
        type_counts[etype] += 1
        if etype == "activity-log":
            continue
        score = _score_entity(
            info, now_ts, recall_counts, name,
            current_branch,
        )
        if score > 0:
            scored.append((score, name, info))

    scored.sort(reverse=True)
    adj = _build_adjacency(relations)

    # Stats line
    n_ent = len(entities)
    n_rel = len(relations)
    n_dec = type_counts.get("decision", 0)
    n_warn = type_counts.get("file-warning", 0)
    print(
        f"Memory: {n_ent}e {n_rel}r "
        f"{n_dec}d {n_warn}w"
        + (f" branch:{current_branch}"
           if current_branch else "")
    )

    # Top 5
    top_n = min(5, len(scored))
    for _, name, info in scored[:top_n]:
        etype = info.get("entityType", "")
        best_obs = _pick_best_observation(
            info.get("observations", [])
        )
        rels = _format_relations(name, adj)
        tag = f" ({etype})" if etype else ""
        print(f"  {name}{tag}: {best_obs}{rels}")

    # Pending decisions
    for name, info in entities.items():
        if info.get("entityType") != "decision":
            continue
        obs = info.get("observations", [])
        if not any(
            o.startswith("Outcome: ")
            and not o.startswith("Outcome: pending")
            for o in obs if isinstance(o, str)
        ):
            display = name
            if display.lower().startswith("decision: "):
                display = display[10:]
            print(f"  [pending] {display}")

    # Tools reminder (CLI mode only — this hook
    # doesn't fire in VSCode)
    print(
        "Tools: semantic_search_memory | "
        "create_decision | graph_stats"
    )


if __name__ == "__main__":
    main()

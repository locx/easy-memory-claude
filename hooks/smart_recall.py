#!/usr/bin/env python3
"""Smart recall: compact status + progressive disclosure for SessionStart.

Standalone — no dependency on semantic_server package.
Usage: python3 smart_recall.py <memory_dir>

Progressive disclosure tiers:
  Tier 1 (SessionStart default): status line + entity names/types (~50 tokens)
  Tier 2 (mem search --compact):  + scores + top observation (~200 tokens)
  Tier 3 (mem search / mem recall): full observations + relations (~1000 tokens)
"""
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

try:
    import orjson as _orjson
    def _loads(s):
        try:
            return _orjson.loads(s)
        except _orjson.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
except ImportError:
    def _loads(s):
        return json.loads(s)

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


def _get_active_files(project_dir):
    """Get list of recently modified/active files via git status."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "-C", project_dir, "status", "-s"],
            text=True, stderr=subprocess.DEVNULL, timeout=1.0
        )
        files = []
        for line in out.splitlines():
            if len(line) > 3:
                path = line[3:].strip()
                basename = os.path.basename(path)
                if basename:
                    files.append(basename.lower())
        return set(files)
    except Exception:
        return set()


def _read_recall_counts(memory_dir):
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


def _parse_entity(obj, entities):
    name = obj.get("name", "")
    if not name:
        return
    if name in entities:
        prev = entities[name]
        seen = set(prev["observations"])
        for o in obj.get("observations", []):
            if isinstance(o, str) and o not in seen:
                prev["observations"].append(o)
                seen.add(o)
        new_u = obj.get("_updated", "")
        if new_u and (not prev["_updated"] or new_u > prev["_updated"]):
            prev["_updated"] = new_u
        b = obj.get("_branch", "")
        if b and not prev.get("_branch"):
            prev["_branch"] = b
    else:
        if len(entities) >= _MAX_ENTITY_COUNT:
            return
        obs = obj.get("observations", [])
        entities[name] = {
            "entityType": obj.get("entityType", ""),
            "observations": [o for o in obs if isinstance(o, str)],
            "_created": obj.get("_created", ""),
            "_updated": obj.get("_updated", ""),
            "_branch": obj.get("_branch", ""),
        }


def _parse_relation(obj, relations, rel_seen):
    fr = obj.get("from", "")
    to = obj.get("to", "")
    rt = obj.get("relationType", "")
    rk = (fr, to, rt)
    if fr and to and rk not in rel_seen:
        rel_seen.add(rk)
        relations.append(rk)


def _load_graph(memory_dir):
    """Single-pass graph load into entities + relations."""
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    entities = {}
    relations = []
    rel_seen = set()
    deadline = time.monotonic() + _PARSE_TIME_BUDGET
    line_count = 0
    try:
        with open(graph_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line_count += 1
                if len(line) > _MAX_LINE_LEN:
                    continue
                if line_count % 1000 == 0 and time.monotonic() > deadline:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _loads(line)
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "entity":
                        _parse_entity(obj, entities)
                    elif t == "relation":
                        _parse_relation(obj, relations, rel_seen)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass
    return entities, relations


def _score_entity(info, now_ts, recall_counts, name,
                  current_branch, active_files=None):
    """Score: obs_count * recency * recall * branch * active."""
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

    # Proactive Priming: boost if entity relates to active files
    if active_files:
        name_lower = name.lower()
        is_active = False
        for f in active_files:
            if f in name_lower:
                is_active = True
                break
        if not is_active:
            for o in obs:
                o_lower = o.lower()
                for f in active_files:
                    if f in o_lower:
                        is_active = True
                        break
                if is_active:
                    break
        if is_active:
            score *= 3.0  # Significant boost for active context

    return score


def _build_adjacency(relations):
    """Build bidirectional adjacency."""
    adj = defaultdict(list)
    for fr, to, rt in relations:
        adj[fr].append((to, rt))
        adj[to].append((fr, rt))
    return adj


def _print_compact_entities(scored, adj):
    """Tier 1: Compact entity names + types, one line.

    ~50 tokens total. Claude calls `mem search` for details.
    """
    n_scored = len(scored)
    if n_scored <= 20:
        budget = min(10, n_scored)
    elif n_scored <= 200:
        budget = min(5, n_scored)
    else:
        budget = min(3, n_scored)

    parts = []
    for _, name, info in scored[:budget]:
        etype = info.get("entityType", "")
        tag = f"({etype})" if etype else ""
        # Count 1-hop connections
        n_conn = len(adj.get(name, []))
        conn = f" [{n_conn} conn]" if n_conn else ""
        parts.append(f"{name}{tag}{conn}")
    print("  Top: " + ", ".join(parts))


def _print_pending_decisions(entities):
    """Print decisions that are still in pending state."""
    pending = []
    for name, info in entities.items():
        if info.get("entityType") != "decision":
            continue
        obs = info.get("observations", [])
        if not any(
            o.startswith("Outcome: ") and not o.startswith("Outcome: pending")
            for o in obs if isinstance(o, str)
        ):
            display = name[10:] if name.lower().startswith("decision: ") else name
            pending.append(display)
    if pending:
        print(f"  Pending decisions ({len(pending)}):")
        for d in pending[:5]:
            print(f"    - {d}")
        if len(pending) > 5:
            print(f"    +{len(pending) - 5} more")


def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    memory_dir = sys.argv[1]
    if not os.path.isdir(memory_dir):
        print("Memory directory not found.")
        return
    project_dir = os.path.dirname(memory_dir)
    current_branch = _read_git_head(project_dir) or ""

    entities, relations = _load_graph(memory_dir)
    if not entities:
        if relations:
            print("Memory graph has relations but no entities.")
        else:
            print("Memory graph is empty.")
        return

    recall_counts = _read_recall_counts(memory_dir)
    now_ts = time.time()
    active_files = _get_active_files(project_dir)

    scored = []
    type_counts = defaultdict(int)
    for name, info in entities.items():
        etype = info.get("entityType", "unknown")
        type_counts[etype] += 1
        if etype == "activity-log":
            continue
        score = _score_entity(
            info, now_ts, recall_counts, name,
            current_branch, active_files,
        )
        if score > 0:
            scored.append((score, name, info))

    scored.sort(reverse=True)
    adj = _build_adjacency(relations)

    n_ent = len(entities)
    n_rel = len(relations)
    n_dec = type_counts.get("decision", 0)
    n_warn = type_counts.get("file-warning", 0)

    # Tier 1: Compact status line (~50 tokens total)
    print(
        f"Memory: {n_ent}e {n_rel}r "
        f"{n_dec}d {n_warn}w"
        + (f" branch:{current_branch}"
           if current_branch else "")
    )
    _print_compact_entities(scored, adj)
    _print_pending_decisions(entities)
    print(
        "Use `mem search <query>` or "
        "`mem recall <query>` for details."
    )


if __name__ == "__main__":
    main()

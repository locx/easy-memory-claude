#!/usr/bin/env python3
"""CLI bridge for memory tools — VSCode fallback.

Usage: python3 memory-cli.py [--memory-dir DIR] <tool> [json_args]
"""
import json
import os
import sys

# Add script's own directory to path
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)


def _resolve_memory_dir(argv):
    """Extract memory_dir from --memory-dir flag, env, or cwd."""
    md = None
    cleaned = []
    i = 0
    while i < len(argv):
        if argv[i] == "--memory-dir" and i + 1 < len(argv):
            md = argv[i + 1]
            i += 2
        elif argv[i].startswith("--memory-dir="):
            md = argv[i].split("=", 1)[1]
            i += 1
        else:
            cleaned.append(argv[i])
            i += 1
    if md is None:
        md = os.environ.get("MEMORY_DIR")
    if md is None:
        md = os.path.join(os.getcwd(), ".memory")
    return md, cleaned


def _usage():
    print(
        "Usage: mem [--memory-dir DIR] "
        "<command> [args]\n"
        "\nUnified Commands (recommended):\n"
        "  search <query>          "
        "Semantic search (positional)\n"
        "  search <query> --compact"
        "  Compact output\n"
        "  recall <query>          "
        "Smart recall: search + 1-hop\n"
        "  write  '<json>'         "
        "Create entities+relations+observations\n"
        "  decide '<json>'         "
        "Create or resolve a decision\n"
        "  remove '<json>'         "
        "Delete entities/observations/rename\n"
        "  status                  "
        "Graph stats + decision nudge\n"
        "  doctor                  "
        "Health check\n"
        "  rebuild                 "
        "Rebuild TF-IDF index\n"
        "  viz [entity]            "
        "Graph visualization (DOT format)\n"
        "  timeline [--global]     "
        "Recent activity across projects\n"
        "\nLegacy tool names also work for "
        "backward compatibility.",
        file=sys.stderr,
    )
    sys.exit(1)


def _parse_positional(args):
    """Parse positional args for unified tools.

    Supports:
      search auth             -> {"query": "auth"}
      search auth --compact   -> {"query": "auth", "compact": true}
      search auth --top-k 3   -> {"query": "auth", "top_k": 3}
      recall auth             -> {"query": "auth"}
      status                  -> {}
      doctor                  -> {}
    Falls back to JSON parsing for complex args.
    """
    if not args:
        return {}
    # If first arg looks like JSON, parse it directly
    first = args[0]
    if first.startswith('{') or first.startswith('['):
        try:
            parsed = json.loads(first)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    # Positional mode: first arg is value, rest are flags
    result = {"query": first}
    i = 1
    while i < len(args):
        flag = args[i]
        if flag == "--compact":
            result["compact"] = True
        elif flag == "--top-k" and i + 1 < len(args):
            i += 1
            try:
                result["top_k"] = int(args[i])
            except ValueError:
                pass
        elif flag == "--mode" and i + 1 < len(args):
            i += 1
            result["mode"] = args[i]
        elif flag == "--since" and i + 1 < len(args):
            i += 1
            result["since"] = args[i]
        elif flag == "--type" and i + 1 < len(args):
            i += 1
            result["entity_type"] = args[i]
        i += 1
    return result


def _do_merge(merging, graph):
    """Read merging file, append to graph, delete merging.

    If append succeeds but unlink fails, truncate the
    file to prevent re-appending the same data.
    """
    # Safety limit: refuse to merge sidecars > 50MB into memory
    try:
        if os.path.exists(merging) and os.path.getsize(merging) > 50 * 1024 * 1024:
            sys.stderr.write(f"Error: Pending merge file {merging} exceeds 50MB limit. Skipping.\n")
            try:
                os.unlink(merging)
            except OSError:
                pass
            return
    except OSError:
        pass

    try:
        with open(merging, "rb") as src:
            data = src.read()
        if not data:
            os.unlink(merging)
            return
        if not data.endswith(b"\n"):
            data += b"\n"
        with open(graph, "ab") as dst:
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
        # Data is in graph — safe to remove source
        try:
            os.unlink(merging)
        except OSError:
            # Unlink failed — truncate to prevent
            # re-append on next recovery cycle
            try:
                with open(merging, "w"):
                    pass
            except OSError:
                pass
    except OSError:
        pass  # pre-append failure — orphan stays


def _merge_pending(memory_dir):
    """Merge .pending sidecar into graph.jsonl.

    Atomic rename prevents TOCTOU with concurrent hook
    writers — new writes go to a fresh .pending file.
    Recovers orphaned .merging/.processing from crash.
    """
    pending = os.path.join(
        memory_dir, "graph.jsonl.pending"
    )
    merging = pending + ".merging"
    graph = os.path.join(memory_dir, "graph.jsonl")

    # Only recover .merging — .processing is owned by
    # the MCP server process (server.py)
    if os.path.exists(merging):
        _do_merge(merging, graph)

    try:
        os.rename(pending, merging)
    except OSError:
        return
    _do_merge(merging, graph)


def _load_graph_doctor(graph_path, issues):
    entities = {}
    relations = []
    try:
        with open(graph_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    issues.append("Corrupt JSONL line")
                    continue
                if "name" in row:
                    entities[row["name"]] = row
                elif "from" in row and "to" in row:
                    relations.append(row)
    except OSError as exc:
        issues.append(f"Cannot read graph: {exc}")
    return entities, relations

def _check_stale_decisions(entities, now, issues):
    from datetime import datetime
    for name, ent in entities.items():
        if ent.get("entityType") == "decision":
            for obs in ent.get("observations", []):
                obs_s = obs if isinstance(obs, str) else ""
                if "pending" in obs_s.lower():
                    updated = ent.get("_updated", "")
                    if updated:
                        try:
                            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                            age_days = (now - dt.timestamp()) / 86400
                            if age_days > 30:
                                issues.append(f"Stale decision: {name} ({int(age_days)}d pending)")
                        except (ValueError, OSError):
                            pass
                    break

def _check_orphan_relations(entities, relations, issues):
    entity_names = set(entities.keys())
    orphan_count = sum(1 for r in relations if r.get("from") not in entity_names or r.get("to") not in entity_names)
    if orphan_count:
        issues.append(f"Orphan relations: {orphan_count} reference non-existent entities")

def _check_oversized_entities(entities, issues):
    oversized = []
    for name, ent in entities.items():
        n_obs = len(ent.get("observations", []))
        if n_obs > 100:
            oversized.append(f"{name} ({n_obs} obs)")
    if oversized:
        issues.append(f"Oversized entities: {', '.join(oversized[:5])}")

def _run_doctor(memory_dir):
    """Health check: stale decisions, orphans, oversized,
    index age, MEMORY.md conflicts."""
    import time
    from collections import Counter
    issues = []
    graph_path = os.path.join(memory_dir, "graph.jsonl")
    if not os.path.exists(graph_path):
        print(json.dumps({
            "status": "no_graph",
            "issues": ["No graph.jsonl found"],
        }))
        return

    entities, relations = _load_graph_doctor(graph_path, issues)
    now = time.time()

    _check_stale_decisions(entities, now, issues)
    _check_orphan_relations(entities, relations, issues)
    _check_oversized_entities(entities, issues)

    # Index staleness
    idx_path = os.path.join(memory_dir, "tfidf_index.json")
    if os.path.exists(idx_path):
        idx_age_h = (now - os.path.getmtime(idx_path)) / 3600
        if idx_age_h > 24:
            issues.append(f"Stale index: {idx_age_h:.0f}h old (rebuild with: memory-cli.py rebuild_index)")
    else:
        issues.append("No TF-IDF index found")

    # MEMORY.md cross-reference check
    project_dir = os.path.dirname(memory_dir)
    claude_memory = os.path.join(
        os.path.expanduser("~"), ".claude",
        "projects", os.path.basename(project_dir),
        "memory", "MEMORY.md",
    )
    if os.path.exists(claude_memory):
        issues.append("MEMORY.md exists — consider adding dedup guidance in CLAUDE.md to avoid duplicate knowledge")

    # Type distribution
    type_counts = Counter(ent.get("entityType", "unknown") for ent in entities.values())

    status = "healthy" if not issues else "issues_found"
    print(json.dumps({
        "status": status,
        "entities": len(entities),
        "relations": len(relations),
        "type_distribution": dict(type_counts.most_common(10)),
        "issues": issues,
        "issue_count": len(issues),
    }, indent=2))


_WRITE_TOOLS = frozenset({
    "create_entities", "create_relations",
    "add_observations", "remove_observations",
    "delete_entities", "rename_entity",
    "create_decision", "update_decision_outcome",
})

# --- Extracted Unified Tool Handlers ---

def _unified_search(a, memory_dir):
    from semantic_server.search import search, search_by_time
    from semantic_server.traverse import traverse_relations
    mode = a.get("mode", "semantic")
    if mode == "temporal":
        return search_by_time(
            memory_dir, a.get("since"), a.get("until"), a.get("limit", 20),
            branch_filter=a.get("branch_filter"), entity_type=a.get("entity_type")
        )
    elif mode == "graph":
        return traverse_relations(
            a.get("entity", a.get("query", "")), memory_dir,
            a.get("direction", "both"), a.get("max_depth", 2)
        )
    return search(
        a.get("query", ""), memory_dir, a.get("top_k", 5),
        branch=a.get("branch"), compact=a.get("compact", False)
    )

def _auto_create_relation_entities(rels, ents, memory_dir, results):
    existing = {e.get("name", "") for e in ents} if ents else set()
    from semantic_server.graph import load_graph_entities
    existing.update(load_graph_entities(memory_dir).keys())
    auto_ents = []
    for r in rels:
        for key in ("from", "to"):
            name = r.get(key, "")
            if name and name not in existing:
                auto_ents.append({
                    "name": name, "entityType": "unknown",
                    "observations": ["Auto-created from relation reference"]
                })
                existing.add(name)
    if auto_ents:
        results["auto_created"] = [e["name"] for e in auto_ents]
        return ents + auto_ents
    return ents

def _handle_obs_map(obs_map, memory_dir, results):
    from semantic_server.tools import add_observations
    obs_results = {}
    for entity, obs_list in obs_map.items():
        if isinstance(obs_list, list):
            obs_results[entity] = add_observations(entity, obs_list, memory_dir)
    results["observations"] = obs_results

def _unified_write(a, memory_dir):
    from semantic_server.tools import create_entities, create_relations, add_observations
    results = {}
    ents = a.get("entities", [])
    rels = a.get("relations", [])
    obs_map = a.get("observations", {})

    if rels:
        ents = _auto_create_relation_entities(rels, ents, memory_dir, results)

    if ents:
        results["entities"] = create_entities(ents, memory_dir)
    if rels:
        results["relations"] = create_relations(rels, memory_dir)
    if obs_map and isinstance(obs_map, dict):
        _handle_obs_map(obs_map, memory_dir, results)

    if not ents and not rels and not obs_map:
        entity_name, observation = a.get("entity", ""), a.get("observation", "")
        if entity_name and observation:
            results = add_observations(entity_name, [observation], memory_dir)
    return results or {"error": "Nothing to write"}

def _unified_recall(a, memory_dir):
    query = a.get("query", "")
    if not query:
        return {"error": "query required"}
    from semantic_server.search import search
    from semantic_server.traverse import traverse_relations
    sr = search(query, memory_dir, top_k=a.get("top_k", 3), branch=a.get("branch"), compact=True)
    results = sr.get("results", [])
    if not results:
        return sr
    enriched = []
    for r in results[:3]:
        entity = r.get("entity", "")
        tr = traverse_relations(entity, memory_dir, "both", 1)
        connected = [
            {"name": n.get("name", ""), "type": n.get("entityType", ""), "relation": n.get("_relation", "")}
            for n in tr.get("nodes", []) if n.get("name") != entity
        ][:5]
        enriched.append({
            "entity": entity, "score": r.get("score", 0),
            "entityType": r.get("entityType", ""), "connected": connected
        })
    return {"results": enriched, "total_indexed": sr.get("total_indexed", 0)}

def _unified_decide(a, memory_dir):
    from semantic_server.tools import create_decision, update_decision_outcome
    return update_decision_outcome(a, memory_dir) if a.get("action", "create") == "resolve" else create_decision(a, memory_dir)

def _unified_remove(a, memory_dir):
    from semantic_server.tools import rename_entity, remove_observations, delete_entities
    action = a.get("action", "")
    if action == "rename":
        return rename_entity(a.get("old_name", ""), a.get("new_name", ""), memory_dir)
    if action == "remove_observations":
        return remove_observations(a.get("entity", ""), a.get("observations", []), memory_dir)
    names = a.get("entity_names", []) or ([a.get("entity")] if a.get("entity") else [])
    return delete_entities(names, memory_dir)

def _unified_status(a, memory_dir):
    from semantic_server.tools import graph_stats, list_decisions
    stats = graph_stats(memory_dir)
    pending = list_decisions(memory_dir, stale_days=2).get("decisions", [])
    if pending:
        stats["decision_nudge"] = {
            "pending_count": len(pending),
            "message": f"{len(pending)} decisions pending > 2 days",
            "oldest": [d.get("title", "") for d in pending[:5]]
        }
    return stats


def _run_viz(memory_dir, entity_name=None):
    """Output DOT graph for visualization."""
    from semantic_server.graph import load_graph_entities, load_graph_relations
    entities = load_graph_entities(memory_dir)
    relations = load_graph_relations(memory_dir)
    if not entities:
        print("digraph memory { label=\"Empty graph\"; }")
        return

    # If entity specified, show subgraph (2-hop neighborhood)
    if entity_name and entity_name in entities:
        relevant = {entity_name}
        for r in relations:
            if r.get("from") == entity_name:
                relevant.add(r["to"])
            elif r.get("to") == entity_name:
                relevant.add(r["from"])
        # 2nd hop
        hop2 = set()
        for r in relations:
            if r.get("from") in relevant:
                hop2.add(r["to"])
            elif r.get("to") in relevant:
                hop2.add(r["from"])
        relevant |= hop2
    else:
        relevant = set(entities.keys())

    _TYPE_COLORS = {
        "component": "#4A90D9", "service": "#7B68EE",
        "decision": "#FFD700", "file-warning": "#FF6347",
        "module": "#3CB371", "function": "#20B2AA",
        "bug": "#FF4500", "activity-log": "#808080",
    }

    lines = ['digraph memory {', '  rankdir=LR;',
             '  node [shape=box, style="rounded,filled", fontname="Helvetica"];',
             '  edge [fontname="Helvetica", fontsize=10];']
    for name in relevant:
        if name not in entities:
            continue
        info = entities[name]
        etype = info.get("entityType", "")
        color = _TYPE_COLORS.get(etype, "#D3D3D3")
        label = name.replace('"', '\\"')
        if etype:
            label += f"\\n({etype})"
        lines.append(f'  "{name}" [label="{label}", fillcolor="{color}"];')
    for r in relations:
        fr, to = r.get("from", ""), r.get("to", "")
        if fr in relevant and to in relevant:
            rt = r.get("relationType", "")
            rt_label = f' [label="{rt}"]' if rt else ""
            lines.append(f'  "{fr}" -> "{to}"{rt_label};')
    lines.append("}")
    print("\n".join(lines))


def _run_timeline(memory_dir, global_mode=False):
    """Show recent activity across one or all projects."""
    import glob as _glob
    from datetime import datetime, timezone

    dirs = []
    if global_mode:
        # Scan common project memory locations
        home = os.path.expanduser("~")
        for pattern in [
            os.path.join(home, "*", ".memory"),
            os.path.join(home, "*", "*", ".memory"),
            os.path.join(home, "projects", "*", ".memory"),
            os.path.join(home, "code", "*", ".memory"),
        ]:
            dirs.extend(_glob.glob(pattern))
        # Deduplicate by realpath
        seen = set()
        unique = []
        for d in dirs:
            rp = os.path.realpath(d)
            if rp not in seen:
                seen.add(rp)
                unique.append(d)
        dirs = unique
    else:
        dirs = [memory_dir]

    entries = []
    for md in dirs:
        graph_path = os.path.join(md, "graph.jsonl")
        project = os.path.basename(os.path.dirname(md))
        try:
            with open(graph_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("type") != "entity":
                            continue
                        updated = obj.get("_updated", "")
                        if not updated:
                            continue
                        entries.append({
                            "project": project,
                            "name": obj.get("name", ""),
                            "type": obj.get("entityType", ""),
                            "updated": updated,
                        })
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            continue

    entries.sort(key=lambda e: e["updated"], reverse=True)

    if sys.stdout.isatty():
        print(f"\nTimeline ({len(entries)} entities"
              + (" across projects" if global_mode else "")
              + "):\n")
        for e in entries[:30]:
            proj = f"[{e['project']}] " if global_mode else ""
            etype = f"({e['type']})" if e['type'] else ""
            print(f"  {e['updated'][:16]}  "
                  f"{proj}{e['name']} {etype}")
        if len(entries) > 30:
            print(f"\n  ... +{len(entries) - 30} more")
        print()
    else:
        print(json.dumps({"entries": entries[:50]}, indent=2))


def _format_tty_output(tool_name, result):
    """Format result for human-readable TTY output."""
    if tool_name in ("search", "recall",
                     "memory_search", "memory_recall"):
        res_list = result.get("results", [])
        print(f"Graph Search "
              f"({result.get('total_indexed', 0)} "
              f"indexed entities):")
        for r in res_list:
            name = r.get("entity", "")
            etype = r.get("entityType", "")
            score = r.get("score", 0.0)
            print(f"\n- \033[1;36m{name}\033[0m "
                  f"({etype}) [score: {score:.2f}]")
            if "observations" in r:
                for obs in r["observations"]:
                    print(f"    \u2022 {obs}")
            if "connected" in r:
                conns = [
                    f"{c.get('relation', '--')}->"
                    f"{c.get('name', '')}"
                    for c in r["connected"]
                ]
                if conns:
                    print(f"    \u21b3 {', '.join(conns)}")
    elif tool_name in ("status", "memory_status",
                       "graph_stats"):
        print("\n\033[1mGraph Diagnostics\033[0m")
        for k, v in result.items():
            if isinstance(v, dict) \
                    and k == "decision_nudge":
                print(f"\n  \033[1;33m\u26a0\ufe0f  "
                      f"{v.get('message', '')}\033[0m")
                for old_d in v.get("oldest", []):
                    print(f"      - {old_d}")
            elif isinstance(v, dict):
                print(f"\n  {k}:")
                for subk, subv in v.items():
                    print(f"    {subk}: {subv}")
            else:
                print(f"  {k}: {v}")
        print("")
    else:
        print(json.dumps(result, indent=2))


def _parse_tool_args(tool_name, extra_args):
    """Parse CLI arguments into a tool_args dict."""
    _POSITIONAL_TOOLS = {
        "search", "recall", "status",
        "doctor", "rebuild_index",
        "viz", "timeline",
    }
    if tool_name in _POSITIONAL_TOOLS:
        return _parse_positional(extra_args)
    if extra_args:
        first = extra_args[0]
        if first.startswith('{') or first.startswith('['):
            try:
                tool_args = json.loads(first)
            except (json.JSONDecodeError, ValueError) as e:
                print(
                    f"Error: invalid JSON: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not isinstance(tool_args, dict):
                print(
                    "Error: args must be a JSON object",
                    file=sys.stderr,
                )
                sys.exit(1)
            return tool_args
        # Try positional for any tool
        return _parse_positional(extra_args)
    return {}


def main():
    memory_dir, args = _resolve_memory_dir(sys.argv[1:])

    if not args:
        _usage()

    tool_name = args[0]
    extra_args = args[1:]

    # --- Unified tool aliases -> legacy names ---
    _ALIASES = {
        "rebuild": "rebuild_index",
    }
    tool_name = _ALIASES.get(tool_name, tool_name)

    tool_args = _parse_tool_args(tool_name, extra_args)

    # --- Auto-Init Logic ---
    if not os.path.isdir(memory_dir):
        try:
            os.makedirs(memory_dir, exist_ok=True)
            graph_path = os.path.join(memory_dir, "graph.jsonl")
            if not os.path.exists(graph_path):
                open(graph_path, "a").close()
            # Inform user if interactive
            if sys.stdout.isatty():
                print(f"Initialized knowledge graph at {memory_dir}", file=sys.stderr)
        except OSError as exc:
            print(
                f"Error: Could not initialize MEMORY_DIR {memory_dir}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Merge pending sidecar before any reads
    _merge_pending(memory_dir)

    if tool_name in ("rebuild_index", "rebuild"):
        import maintenance
        indexed = maintenance.rebuild_index(memory_dir)
        print(json.dumps({
            "rebuilt": indexed > 0,
            "indexed": indexed,
        }))
        return

    if tool_name == "doctor":
        _run_doctor(memory_dir)
        return

    if tool_name == "viz":
        entity = extra_args[0] if extra_args else None
        _run_viz(memory_dir, entity)
        return

    if tool_name == "timeline":
        global_mode = "--global" in extra_args
        _run_timeline(memory_dir, global_mode)
        return

    from semantic_server.search import (
        search, search_by_time,
    )
    from semantic_server.tools import (
        add_observations,
        create_decision,
        create_entities,
        create_relations,
        delete_entities,
        graph_stats,
        list_decisions,
        remove_observations,
        rename_entity,
        update_decision_outcome,
    )
    from semantic_server.traverse import (
        traverse_relations,
    )
    from semantic_server.graph import load_index
    from semantic_server.recall import (
        init_recall_state,
        flush_recall_counts,
    )

    try:
        load_index(memory_dir)
        init_recall_state(memory_dir)
    except Exception as exc:
        print(f"Warning: index init failed ({exc}), search may be degraded", file=sys.stderr)

    dispatch = {
        # --- Unified tools (new) ---
        "search": lambda a: _unified_search(a, memory_dir),
        "write": lambda a: _unified_write(a, memory_dir),
        "recall": lambda a: _unified_recall(a, memory_dir),
        "decide": lambda a: _unified_decide(a, memory_dir),
        "remove": lambda a: _unified_remove(a, memory_dir),
        "status": lambda a: _unified_status(a, memory_dir),
        # --- Legacy tools (backward compat) ---
        "semantic_search_memory": lambda a: search(
            a.get("query", ""),
            memory_dir,
            a.get("top_k", 5),
            branch=a.get("branch"),
            compact=a.get("compact", False),
        ),
        "traverse_relations": lambda a: (
            traverse_relations(
                a.get("entity", ""),
                memory_dir,
                a.get("direction", "both"),
                a.get("max_depth", 2),
            )
        ),
        "search_memory_by_time": lambda a: (
            search_by_time(
                memory_dir,
                a.get("since"),
                a.get("until"),
                a.get("limit", 20),
                branch_filter=a.get(
                    "branch_filter"
                ),
                entity_type=a.get("entity_type"),
            )
        ),
        "create_entities": lambda a: (
            create_entities(
                a.get("entities", []),
                memory_dir,
            )
        ),
        "create_relations": lambda a: (
            create_relations(
                a.get("relations", []),
                memory_dir,
            )
        ),
        "add_observations": lambda a: (
            add_observations(
                a.get("entity", ""),
                a.get("observations", []),
                memory_dir,
            )
        ),
        "remove_observations": lambda a: (
            remove_observations(
                a.get("entity", ""),
                a.get("observations", []),
                memory_dir,
            )
        ),
        "delete_entities": lambda a: (
            delete_entities(
                a.get("entity_names", []),
                memory_dir,
            )
        ),
        "rename_entity": lambda a: (
            rename_entity(
                a.get("old_name", ""),
                a.get("new_name", ""),
                memory_dir,
            )
        ),
        "create_decision": lambda a: (
            create_decision(a, memory_dir)
        ),
        "update_decision_outcome": lambda a: (
            update_decision_outcome(a, memory_dir)
        ),
        "list_decisions": lambda a: (
            list_decisions(
                memory_dir,
                stale_days=a.get("stale_days"),
            )
        ),
        "graph_stats": lambda a: (
            graph_stats(memory_dir)
        ),
        # Aliases for unified names used with
        # legacy MCP protocol names
        "memory_search": lambda a: _unified_search(a, memory_dir),
        "memory_write": lambda a: _unified_write(a, memory_dir),
        "memory_recall": lambda a: _unified_recall(a, memory_dir),
        "memory_decide": lambda a: _unified_decide(a, memory_dir),
        "memory_remove": lambda a: _unified_remove(a, memory_dir),
        "memory_status": lambda a: _unified_status(a, memory_dir),
    }

    handler = dispatch.get(tool_name)
    if handler is None:
        print(
            f"Error: unknown tool '{tool_name}'",
            file=sys.stderr,
        )
        _usage()

    try:
        result = handler(tool_args)
    except Exception as exc:
        print(
            json.dumps({"error": str(exc)}, indent=2)
        )
        sys.exit(1)

    # TTY human-readable formatting or raw JSON
    if sys.stdout.isatty() and isinstance(result, dict) \
            and not result.get("error"):
        _format_tty_output(tool_name, result)
    else:
        print(json.dumps(result, indent=2))

    # Rebuild index after write ops
    _WRITE_OPS = _WRITE_TOOLS | {
        "write", "memory_write",
        "decide", "memory_decide",
        "remove", "memory_remove",
    }
    if tool_name in _WRITE_OPS:
        try:
            import maintenance
            maintenance.rebuild_index(memory_dir)
        except Exception as exc:
            print(
                f"Warning: index rebuild failed: "
                f"{exc}",
                file=sys.stderr,
            )

    # Flush recall counts (no-op if nothing changed)
    try:
        flush_recall_counts()
    except Exception:
        pass


if __name__ == "__main__":
    main()

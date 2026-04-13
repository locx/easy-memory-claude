#!/usr/bin/env python3
"""CLI for the knowledge graph memory system.

Usage: python3 memory-cli.py [--memory-dir DIR] <command> [args]

Commands: search, recall, write, decide, remove, status, doctor,
          rebuild, diff.
"""
import json
import os
import sys
import time

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
        "\nCommands:\n"
        "  search <query>          "
        "Search knowledge graph\n"
        "  recall <query>          "
        "Search + 1-hop graph neighbors\n"
        "  write  '<json>'         "
        "Create graph entities/relations/obs\n"
        "  decide '<json>'         "
        "Create or resolve a decision\n"
        "  remove '<json>'         "
        "Delete graph entities/observations\n"
        "  status                  "
        "Graph health + diagnostics\n"
        "  doctor                  "
        "Deep health check\n"
        "  rebuild                 "
        "Rebuild TF-IDF index\n"
        "  diff                    "
        "Changes since last session",
        file=sys.stderr,
    )
    sys.exit(1)


def _parse_positional(args):
    """Parse positional args into a tool_args dict.

    Supports flags before or after positional values:
      search auth service     -> {"query": "auth service"}
      search auth --compact   -> {"query": "auth", "compact": true}
    Falls back to JSON parsing for complex args.
    """
    if not args:
        return {}
    first = args[0]
    if first.startswith('{') or first.startswith('['):
        try:
            parsed = json.loads(first)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    result = {}
    positionals = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--compact":
            result["compact"] = True
        elif arg == "--top-k" and i + 1 < len(args):
            i += 1
            try:
                result["top_k"] = int(args[i])
            except ValueError:
                pass
        elif arg == "--mode" and i + 1 < len(args):
            i += 1
            result["mode"] = args[i]
        elif arg == "--since" and i + 1 < len(args):
            i += 1
            result["since"] = args[i]
        elif arg == "--type" and i + 1 < len(args):
            i += 1
            result["entity_type"] = args[i]
        elif arg == "--depth" and i + 1 < len(args):
            i += 1
            try:
                result["depth"] = int(args[i])
            except ValueError:
                pass
        elif not arg.startswith("--"):
            positionals.append(arg)
        i += 1
    if positionals:
        result["query"] = " ".join(positionals)
    return result


# --- Pending sidecar merge ---

def _do_merge(merging, graph):
    """Read merging file, append to graph, delete merging."""
    try:
        size = os.path.getsize(merging)
    except OSError:
        return
    if size > 50 * 1024 * 1024:
        sys.stderr.write(
            f"Error: Pending merge file {merging} "
            f"exceeds 50MB limit. Skipping.\n"
        )
        try:
            os.unlink(merging)
        except OSError:
            pass
        return

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
        try:
            os.unlink(merging)
        except OSError:
            try:
                with open(merging, "w"):
                    pass
            except OSError:
                pass
    except OSError:
        pass


def _merge_pending(memory_dir):
    """Merge .pending sidecar into graph.jsonl.

    Atomic rename prevents TOCTOU with concurrent hook
    writers. Recovers orphaned .merging from crash.
    """
    pending = os.path.join(
        memory_dir, "graph.jsonl.pending"
    )
    merging = pending + ".merging"
    graph = os.path.join(memory_dir, "graph.jsonl")

    _do_merge(merging, graph)

    try:
        os.rename(pending, merging)
    except OSError:
        return
    _do_merge(merging, graph)


# --- Doctor ---

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
                            dt = datetime.fromisoformat(
                                updated.replace("Z", "+00:00")
                            )
                            age_days = (now - dt.timestamp()) / 86400
                            if age_days > 30:
                                issues.append(
                                    f"Stale decision: {name} "
                                    f"({int(age_days)}d pending)"
                                )
                        except (ValueError, OSError):
                            pass
                    break


def _check_orphan_relations(entities, relations, issues):
    entity_names = set(entities.keys())
    orphan_count = sum(
        1 for r in relations
        if r.get("from") not in entity_names
        or r.get("to") not in entity_names
    )
    if orphan_count:
        issues.append(
            f"Orphan relations: {orphan_count} "
            f"reference non-existent entities"
        )


def _check_oversized_entities(entities, issues):
    oversized = []
    for name, ent in entities.items():
        n_obs = len(ent.get("observations", []))
        if n_obs > 100:
            oversized.append(f"{name} ({n_obs} obs)")
    if oversized:
        issues.append(
            f"Oversized entities: "
            f"{', '.join(oversized[:5])}"
        )


def _run_doctor(memory_dir):
    """Health check for the knowledge graph."""
    from collections import Counter
    issues = []
    graph_path = os.path.join(memory_dir, "graph.jsonl")

    if os.path.exists(graph_path):
        entities, relations = _load_graph_doctor(
            graph_path, issues
        )
        now = time.time()
        _check_stale_decisions(entities, now, issues)
        _check_orphan_relations(entities, relations, issues)
        _check_oversized_entities(entities, issues)

        idx_path = os.path.join(memory_dir, "tfidf_index.json")
        if os.path.exists(idx_path):
            idx_age_h = (
                (now - os.path.getmtime(idx_path)) / 3600
            )
            if idx_age_h > 24:
                issues.append(
                    f"Stale index: {idx_age_h:.0f}h old "
                    f"(run: mem rebuild)"
                )
        elif entities:
            issues.append(
                "No TF-IDF index found (run: mem rebuild)"
            )

        type_counts = Counter(
            ent.get("entityType", "unknown")
            for ent in entities.values()
        )
    else:
        entities = {}
        relations = []
        type_counts = Counter()
        issues.append("No graph.jsonl found")

    status = "healthy" if not issues else "issues_found"
    result = {
        "status": status,
        "graph": {
            "entities": len(entities),
            "relations": len(relations),
            "type_distribution": dict(
                type_counts.most_common(10)
            ),
        },
        "issues": issues,
        "issue_count": len(issues),
    }

    if sys.stdout.isatty():
        print(f"\n\033[1mMemory Doctor\033[0m — {status}")
        g = result["graph"]
        print(
            f"  Graph: {g['entities']}e "
            f"{g['relations']}r"
        )
        if issues:
            print(f"\n  Issues ({len(issues)}):")
            for issue in issues:
                print(f"    \033[33m!\033[0m {issue}")
        else:
            print("  \033[32mNo issues found\033[0m")
        print()
    else:
        print(json.dumps(result, indent=2))


# --- Unified tool handlers ---

def _unified_search(a, memory_dir):
    from semantic_server.search import search, search_by_time
    from semantic_server.traverse import traverse_relations

    mode = a.get("mode", "semantic")
    if mode == "temporal":
        return search_by_time(
            memory_dir, a.get("since"), a.get("until"),
            a.get("limit", 20),
            branch_filter=a.get("branch_filter"),
            entity_type=a.get("entity_type"),
        )
    elif mode == "graph":
        return traverse_relations(
            a.get("entity", a.get("query", "")), memory_dir,
            a.get("direction", "both"), a.get("max_depth", 2),
        )

    query = a.get("query", "")
    top_k = a.get("top_k", 5)

    return search(
        query, memory_dir, top_k=top_k,
        branch=a.get("branch"),
        compact=a.get("compact", False),
    )


def _auto_create_relation_entities(rels, ents, memory_dir,
                                   results):
    existing = (
        {e.get("name", "") for e in ents} if ents else set()
    )
    from semantic_server.graph import load_graph_entities
    existing.update(load_graph_entities(memory_dir).keys())
    auto_ents = []
    for r in rels:
        for key in ("from", "to"):
            name = r.get(key, "")
            if name and name not in existing:
                auto_ents.append({
                    "name": name, "entityType": "unknown",
                    "observations": [
                        "Auto-created from relation reference"
                    ],
                })
                existing.add(name)
    if auto_ents:
        results["auto_created"] = [
            e["name"] for e in auto_ents
        ]
        return ents + auto_ents
    return ents


def _handle_obs_map(obs_map, memory_dir, results):
    from semantic_server.tools import add_observations
    obs_results = {}
    for entity, obs_list in obs_map.items():
        if isinstance(obs_list, list):
            obs_results[entity] = add_observations(
                entity, obs_list, memory_dir
            )
    results["observations"] = obs_results


def _unified_write(a, memory_dir):
    from semantic_server.tools import (
        create_entities, create_relations, add_observations,
    )
    results = {}
    ents = a.get("entities", [])
    rels = a.get("relations", [])
    obs_map = a.get("observations", {})

    if rels:
        ents = _auto_create_relation_entities(
            rels, ents, memory_dir, results
        )

    if ents:
        results["entities"] = create_entities(ents, memory_dir)
    if rels:
        results["relations"] = create_relations(
            rels, memory_dir
        )
    if obs_map and isinstance(obs_map, dict):
        _handle_obs_map(obs_map, memory_dir, results)

    if not ents and not rels and not obs_map:
        entity_name = a.get("entity", "")
        observation = a.get("observation", "")
        if entity_name and observation:
            results = add_observations(
                entity_name, [observation], memory_dir
            )
    return results or {"error": "Nothing to write"}


def _unified_recall(a, memory_dir):
    query = a.get("query", "")
    if not query:
        return {"error": "query required"}
    from semantic_server.search import search
    from semantic_server.traverse import traverse_relations

    sr = search(
        query, memory_dir, top_k=a.get("top_k", 3),
        branch=a.get("branch"), compact=True,
    )
    results = sr.get("results", [])
    if not results:
        return sr
    enriched = []
    for r in results[:3]:
        entity = r.get("entity", "")
        tr = traverse_relations(entity, memory_dir, "both", 1)
        connected = [
            {
                "name": n.get("name", ""),
                "type": n.get("entityType", ""),
                "relation": n.get("_relation", ""),
            }
            for n in tr.get("nodes", [])
            if n.get("name") != entity
        ][:5]
        enriched.append({
            "entity": entity,
            "score": r.get("score", 0),
            "entityType": r.get("entityType", ""),
            "connected": connected,
        })
    return {
        "results": enriched,
        "total_indexed": sr.get("total_indexed", 0),
    }


def _unified_decide(a, memory_dir):
    from semantic_server.tools import (
        create_decision, update_decision_outcome,
    )
    if a.get("action", "create") == "resolve":
        return update_decision_outcome(a, memory_dir)
    return create_decision(a, memory_dir)


def _unified_remove(a, memory_dir):
    from semantic_server.tools import (
        rename_entity, remove_observations, delete_entities,
    )
    action = a.get("action", "")
    if action == "rename":
        return rename_entity(
            a.get("old_name", ""), a.get("new_name", ""),
            memory_dir,
        )
    if action == "remove_observations":
        return remove_observations(
            a.get("entity", ""), a.get("observations", []),
            memory_dir,
        )
    names = (
        a.get("entity_names", [])
        or ([a.get("entity")] if a.get("entity") else [])
    )
    return delete_entities(names, memory_dir)


def _unified_status(a, memory_dir):
    from semantic_server.tools import graph_stats, list_decisions
    stats = graph_stats(memory_dir)
    pending = list_decisions(
        memory_dir, stale_days=2
    ).get("decisions", [])
    if pending:
        stats["decision_nudge"] = {
            "pending_count": len(pending),
            "message": (
                f"{len(pending)} decisions pending > 2 days"
            ),
            "oldest": [
                d.get("title", "") for d in pending[:5]
            ],
        }
    return stats


# --- Diff ---

def _run_diff(memory_dir):
    """Show entities changed since last session."""
    marker = os.path.join(memory_dir, ".last-session-start")
    try:
        with open(marker) as f:
            last_ts = f.read().strip()
    except OSError:
        last_ts = None

    if not last_ts:
        mem = os.path.expanduser("~/.claude/memory/mem")
        print("No previous session recorded. "
              f"Run `{mem} status` in a new session first.")
        return

    graph_path = os.path.join(memory_dir, "graph.jsonl")
    new_entities = []
    updated_entities = []
    new_decisions = []
    resolved_decisions = []

    try:
        with open(graph_path, encoding="utf-8") as f:
            entities = {}
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") != "entity":
                        continue
                    name = obj.get("name", "")
                    if not name:
                        continue
                    if name in entities:
                        prev = entities[name]
                        new_u = obj.get("_updated", "")
                        if new_u and (not prev.get("_updated")
                                      or new_u > prev["_updated"]):
                            prev["_updated"] = new_u
                        for o in obj.get("observations", []):
                            if isinstance(o, str):
                                prev.setdefault(
                                    "observations", []
                                ).append(o)
                    else:
                        entities[name] = dict(obj)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        print("Cannot read graph.")
        return

    for name, info in entities.items():
        etype = info.get("entityType", "")
        created = info.get("_created", "")
        updated = info.get("_updated", "")
        if created and created > last_ts:
            entry = {"name": name, "type": etype,
                     "timestamp": created}
            if etype == "decision":
                new_decisions.append(entry)
            else:
                new_entities.append(entry)
        elif updated and updated > last_ts:
            if etype == "decision":
                obs = info.get("observations", [])
                resolved = any(
                    isinstance(o, str)
                    and o.startswith("Outcome: ")
                    and not o.startswith("Outcome: pending")
                    for o in obs
                )
                if resolved:
                    resolved_decisions.append(
                        {"name": name, "timestamp": updated}
                    )
                    continue
            updated_entities.append(
                {"name": name, "type": etype,
                 "timestamp": updated}
            )

    if sys.stdout.isatty():
        total = (len(new_entities) + len(updated_entities)
                 + len(new_decisions) + len(resolved_decisions))
        if total == 0:
            print(f"No changes since {last_ts[:16]}.")
            return
        print(f"\n\033[1mChanges since {last_ts[:16]}\033[0m\n")
        if new_entities:
            print(f"  \033[32m+ New ({len(new_entities)}):\033[0m")
            for e in new_entities[:10]:
                tag = f" ({e['type']})" if e['type'] else ""
                print(f"    {e['name']}{tag}")
            if len(new_entities) > 10:
                print(f"    +{len(new_entities) - 10} more")
        if updated_entities:
            print(
                f"  \033[33m~ Updated "
                f"({len(updated_entities)}):\033[0m"
            )
            for e in updated_entities[:10]:
                tag = f" ({e['type']})" if e['type'] else ""
                print(f"    {e['name']}{tag}")
            if len(updated_entities) > 10:
                print(
                    f"    +{len(updated_entities) - 10} more"
                )
        if new_decisions:
            print(
                f"  \033[35mDecisions made "
                f"({len(new_decisions)}):\033[0m"
            )
            for d in new_decisions[:5]:
                name = d["name"]
                if name.lower().startswith("decision: "):
                    name = name[10:]
                print(f"    {name}")
        if resolved_decisions:
            print(
                f"  \033[36mDecisions resolved "
                f"({len(resolved_decisions)}):\033[0m"
            )
            for d in resolved_decisions[:5]:
                name = d["name"]
                if name.lower().startswith("decision: "):
                    name = name[10:]
                print(f"    {name}")
        print()
    else:
        print(json.dumps({
            "since": last_ts,
            "new": new_entities,
            "updated": updated_entities,
            "new_decisions": new_decisions,
            "resolved_decisions": resolved_decisions,
        }, indent=2))


# --- TTY formatting ---

def _format_tty_output(tool_name, result):
    """Format result for human-readable TTY output."""
    if tool_name in ("search", "recall"):
        res_list = result.get("results", [])
        print(
            f"Search "
            f"({result.get('total_indexed', 0)} indexed):"
        )
        for r in res_list:
            name = r.get("entity", "")
            etype = r.get("entityType", "")
            score = r.get("score", 0.0)
            print(
                f"\n- \033[1;36m{name}\033[0m "
                f"({etype}) [score: {score:.2f}]"
            )
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
                    print(
                        f"    \u21b3 {', '.join(conns)}"
                    )
    elif tool_name == "status":
        print("\n\033[1mMemory Diagnostics\033[0m")
        for k, v in result.items():
            if (isinstance(v, dict)
                    and k == "decision_nudge"):
                print(
                    f"\n  \033[1;33m\u26a0\ufe0f  "
                    f"{v.get('message', '')}\033[0m"
                )
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


# --- Arg parsing ---

def _parse_tool_args(tool_name, extra_args):
    """Parse CLI arguments into a tool_args dict."""
    _POSITIONAL_TOOLS = {
        "search", "recall", "status",
        "doctor", "rebuild", "diff",
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
        return _parse_positional(extra_args)
    return {}


def main():
    memory_dir, args = _resolve_memory_dir(sys.argv[1:])

    if not args:
        _usage()

    tool_name = args[0]
    extra_args = args[1:]

    tool_args = _parse_tool_args(tool_name, extra_args)

    # --- Auto-Init ---
    if not os.path.isdir(memory_dir):
        try:
            os.makedirs(memory_dir, exist_ok=True)
            # "a" mode creates if not exists, no-op if exists
            with open(os.path.join(memory_dir, "graph.jsonl"), "a"):
                pass
            if sys.stdout.isatty():
                print(
                    f"Initialized knowledge graph at "
                    f"{memory_dir}",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(
                f"Error: Could not initialize "
                f"MEMORY_DIR {memory_dir}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Init branch detection (project_dir = parent of .memory)
    from semantic_server.config import init_branch
    project_dir = os.path.dirname(memory_dir)
    init_branch(project_dir)

    # Merge pending sidecar before any reads
    _merge_pending(memory_dir)

    # --- Commands that don't need semantic_server ---
    if tool_name == "rebuild":
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

    if tool_name == "diff":
        _run_diff(memory_dir)
        return

    # --- Commands that need semantic_server ---
    from semantic_server.graph import load_index
    from semantic_server.recall import (
        init_recall_state,
        flush_recall_counts,
    )

    try:
        load_index(memory_dir)
        init_recall_state(memory_dir)
    except Exception as exc:
        print(
            f"Warning: index init failed ({exc}), "
            f"search may be degraded",
            file=sys.stderr,
        )

    dispatch = {
        "search": lambda a: _unified_search(a, memory_dir),
        "write": lambda a: _unified_write(a, memory_dir),
        "recall": lambda a: _unified_recall(a, memory_dir),
        "decide": lambda a: _unified_decide(a, memory_dir),
        "remove": lambda a: _unified_remove(a, memory_dir),
        "status": lambda a: _unified_status(a, memory_dir),
    }

    handler = dispatch.get(tool_name)
    if handler is None:
        print(
            f"Error: unknown command '{tool_name}'",
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
    if (sys.stdout.isatty() and isinstance(result, dict)
            and not result.get("error")):
        _format_tty_output(tool_name, result)
    else:
        print(json.dumps(result, indent=2))

    # Rebuild index after write ops
    if tool_name in ("write", "decide", "remove"):
        try:
            import maintenance
            maintenance.rebuild_index(memory_dir)
        except Exception as exc:
            print(
                f"Warning: index rebuild failed: "
                f"{exc}",
                file=sys.stderr,
            )

    # Flush recall counts
    try:
        flush_recall_counts()
    except Exception:
        pass


if __name__ == "__main__":
    main()

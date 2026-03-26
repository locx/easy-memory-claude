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
        "Usage: memory-cli.py [--memory-dir DIR] "
        "<tool> [json_args]\n"
        "\nTools:\n"
        "  semantic_search_memory  "
        '{"query":"...","top_k":5}\n'
        "  traverse_relations      "
        '{"entity":"...","direction":"both"}\n'
        "  search_memory_by_time   "
        '{"since":"...","entity_type":"decision"}\n'
        "  create_entities         "
        '{"entities":[...]}\n'
        "  create_relations        "
        '{"relations":[...]}\n'
        "  add_observations        "
        '{"entity":"...","observations":[...]}\n'
        "  remove_observations     "
        '{"entity":"...","observations":[...]}\n'
        "  delete_entities         "
        '{"entity_names":[...]}\n'
        "  rename_entity           "
        '{"old_name":"...","new_name":"..."}\n'
        "  create_decision         "
        '{"title":"...","rationale":"..."}\n'
        "  update_decision_outcome "
        '{"title":"...","outcome":"successful"}\n'
        "  list_decisions          (no args needed)\n"
        "  graph_stats             (no args needed)\n"
        "  rebuild_index           (no args needed)",
        file=sys.stderr,
    )
    sys.exit(1)


def _do_merge(merging, graph):
    """Read merging file, append to graph, delete merging.

    If append succeeds but unlink fails, truncate the
    file to prevent re-appending the same data.
    """
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


_WRITE_TOOLS = frozenset({
    "create_entities", "create_relations",
    "add_observations", "remove_observations",
    "delete_entities", "rename_entity",
    "create_decision", "update_decision_outcome",
})


def main():
    memory_dir, args = _resolve_memory_dir(sys.argv[1:])

    if not args:
        _usage()

    tool_name = args[0]
    tool_args = {}
    if len(args) > 1:
        try:
            tool_args = json.loads(args[1])
        except (json.JSONDecodeError, ValueError) as exc:
            print(
                f"Error: invalid JSON args: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        if not isinstance(tool_args, dict):
            print(
                "Error: args must be a JSON object {}",
                file=sys.stderr,
            )
            sys.exit(1)

    if not os.path.isdir(memory_dir):
        print(
            f"Error: MEMORY_DIR not found: {memory_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Merge pending sidecar before any reads
    _merge_pending(memory_dir)

    if tool_name == "rebuild_index":
        import maintenance
        indexed = maintenance.rebuild_index(memory_dir)
        print(json.dumps({
            "rebuilt": indexed > 0,
            "indexed": indexed,
        }))
        return

    from semantic_server.search import search, search_by_time
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
    from semantic_server.traverse import traverse_relations
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
        "semantic_search_memory": lambda a: search(
            a.get("query", ""),
            memory_dir,
            a.get("top_k", 5),
            branch=a.get("branch"),
        ),
        "traverse_relations": lambda a: traverse_relations(
            a.get("entity", ""),
            memory_dir,
            a.get("direction", "both"),
            a.get("max_depth", 2),
        ),
        "search_memory_by_time": lambda a: search_by_time(
            memory_dir,
            a.get("since"),
            a.get("until"),
            a.get("limit", 20),
            branch_filter=a.get("branch_filter"),
            entity_type=a.get("entity_type"),
        ),
        "create_entities": lambda a: create_entities(
            a.get("entities", []),
            memory_dir,
        ),
        "create_relations": lambda a: create_relations(
            a.get("relations", []),
            memory_dir,
        ),
        "add_observations": lambda a: add_observations(
            a.get("entity", ""),
            a.get("observations", []),
            memory_dir,
        ),
        "remove_observations": lambda a: remove_observations(
            a.get("entity", ""),
            a.get("observations", []),
            memory_dir,
        ),
        "delete_entities": lambda a: delete_entities(
            a.get("entity_names", []),
            memory_dir,
        ),
        "rename_entity": lambda a: rename_entity(
            a.get("old_name", ""),
            a.get("new_name", ""),
            memory_dir,
        ),
        "create_decision": lambda a: create_decision(
            a, memory_dir,
        ),
        "update_decision_outcome": lambda a: (
            update_decision_outcome(a, memory_dir)
        ),
        "list_decisions": lambda a: list_decisions(
            memory_dir,
            stale_days=a.get("stale_days"),
        ),
        "graph_stats": lambda a: graph_stats(memory_dir),
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

    print(json.dumps(result, indent=2))

    # Rebuild index after write ops so subsequent
    # searches find newly created entities
    if tool_name in _WRITE_TOOLS:
        try:
            import maintenance
            maintenance.rebuild_index(memory_dir)
        except Exception as exc:
            print(
                f"Warning: index rebuild failed: {exc}",
                file=sys.stderr,
            )

    # Flush recall counts (no-op if nothing changed)
    try:
        flush_recall_counts()
    except Exception:
        pass


if __name__ == "__main__":
    main()

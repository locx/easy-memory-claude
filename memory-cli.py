#!/usr/bin/env python3
"""CLI bridge for memory tools — VSCode fallback.

When the MCP server can't be connected (e.g., VSCode extension),
this script lets Claude call memory tools directly via Bash:

    python3 ~/.claude/memory/memory-cli.py <tool> [json_args]

Examples:
    python3 ~/.claude/memory/memory-cli.py graph_stats
    python3 ~/.claude/memory/memory-cli.py semantic_search_memory '{"query":"auth"}'
    python3 ~/.claude/memory/memory-cli.py create_decision '{"title":"Use JWT","rationale":"Stateless"}'

Requires MEMORY_DIR env var or --memory-dir flag.
"""
import json
import os
import sys

# Add semantic_server to path
_claude_memory = os.path.join(
    os.path.expanduser("~"), ".claude", "memory"
)
if _claude_memory not in sys.path:
    sys.path.insert(0, _claude_memory)


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
        '{"since":"2026-03-01T00:00:00Z"}\n'
        "  create_entities         "
        '{"entities":[...]}\n'
        "  create_relations        "
        '{"relations":[...]}\n'
        "  add_observations        "
        '{"entity":"...","observations":[...]}\n'
        "  delete_entities         "
        '{"entity_names":[...]}\n'
        "  create_decision         "
        '{"title":"...","rationale":"..."}\n'
        "  update_decision_outcome "
        '{"title":"...","outcome":"successful"}\n'
        "  graph_stats             (no args needed)",
        file=sys.stderr,
    )
    sys.exit(1)


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

    # Import handlers directly — no MCP protocol overhead
    from semantic_server.search import search, search_by_time
    from semantic_server.tools import (
        add_observations,
        create_decision,
        create_entities,
        create_relations,
        delete_entities,
        graph_stats,
        update_decision_outcome,
    )
    from semantic_server.traverse import traverse_relations
    from semantic_server.graph import load_index
    from semantic_server.recall import load_recall_counts

    # Initialize index + recall (normally done by MCP init)
    load_index(memory_dir)
    load_recall_counts(memory_dir)

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
        "delete_entities": lambda a: delete_entities(
            a.get("entity_names", []),
            memory_dir,
        ),
        "create_decision": lambda a: create_decision(
            a, memory_dir,
        ),
        "update_decision_outcome": lambda a: (
            update_decision_outcome(a, memory_dir)
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


if __name__ == "__main__":
    main()

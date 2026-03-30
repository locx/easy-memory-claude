"""MCP protocol: tool schemas and JSON-RPC 2.0 message handling."""
import json
import sys

from ._json import dumps as _fast_dumps

from .config import (
    PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION,
    reset_session_stats, log_event, refresh_branch,
)
from .graph import load_index
from .recall import init_recall_state
from .search import search, search_by_time
from .tools import (
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
from .traverse import traverse_relations

# Load tool schemas from external JSON (292L data, not code)
import importlib.resources as _res
with _res.files(__package__).joinpath(
    "tools_schema.json"
).open() as _f:
    TOOLS = json.load(_f)


def _dispatch_tool_call(tool_name, args, memory_dir):
    if tool_name == "semantic_search_memory":
        return search(
            args.get("query", ""),
            memory_dir,
            args.get("top_k", 5),
            branch=args.get("branch"),
        )
    if tool_name == "traverse_relations":
        return traverse_relations(
            args.get("entity", ""),
            memory_dir,
            args.get("direction", "both"),
            args.get("max_depth", 2),
        )
    if tool_name == "search_memory_by_time":
        return search_by_time(
            memory_dir,
            args.get("since"),
            args.get("until"),
            args.get("limit", 20),
            branch_filter=args.get("branch_filter"),
            entity_type=args.get("entity_type"),
        )
    if tool_name == "create_entities":
        return create_entities(
            args.get("entities", []),
            memory_dir,
        )
    if tool_name == "create_relations":
        return create_relations(
            args.get("relations", []),
            memory_dir,
        )
    if tool_name == "add_observations":
        return add_observations(
            args.get("entity", ""),
            args.get("observations", []),
            memory_dir,
        )
    if tool_name == "delete_entities":
        return delete_entities(
            args.get("entity_names", []),
            memory_dir,
        )
    if tool_name == "create_decision":
        return create_decision(args, memory_dir)
    if tool_name == "update_decision_outcome":
        return update_decision_outcome(args, memory_dir)
    if tool_name == "list_decisions":
        return list_decisions(
            memory_dir,
            stale_days=args.get("stale_days"),
        )
    if tool_name == "remove_observations":
        return remove_observations(
            args.get("entity", ""),
            args.get("observations", []),
            memory_dir,
        )
    if tool_name == "rename_entity":
        return rename_entity(
            args.get("old_name", ""),
            args.get("new_name", ""),
            memory_dir,
        )
    if tool_name == "graph_stats":
        return graph_stats(memory_dir)
    return None


def handle_message(msg, memory_dir):
    """Handle a single JSON-RPC 2.0 message."""
    if not isinstance(msg, dict):
        return None

    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        reset_session_stats()
        refresh_branch()
        load_index(memory_dir)
        init_recall_state(memory_dir)
        log_event("INIT", "session started")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        try:
            result = _dispatch_tool_call(tool_name, args, memory_dir)
            if result is None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unknown tool: {tool_name}",
                    },
                }
        except Exception as exc:
            exc_msg = str(exc)[:500]
            try:
                sys.stderr.write(
                    f"error: {tool_name}: {exc_msg}\n"
                )
            except OSError:
                pass
            result = {
                "error": exc_msg,
                "results": [],
            }

        is_err = isinstance(result, dict) and "error" in result

        try:
            result_text = _fast_dumps(result)
        except (TypeError, ValueError, OverflowError):
            result_text = _fast_dumps({
                "error": "Result not serializable",
            })
            is_err = True
        resp_content = {
            "content": [{
                "type": "text",
                "text": result_text,
            }],
        }
        if is_err:
            resp_content["isError"] = True
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": resp_content,
        }

    if method.startswith("notifications/"):
        return None

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }
    return None

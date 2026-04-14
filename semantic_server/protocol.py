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


# Tool dispatch: each handler is (args, memory_dir) -> result
_TOOL_HANDLERS = {
    "semantic_search_memory": lambda a, md: search(
        a.get("query", ""), md,
        a.get("top_k", 5),
        branch=a.get("branch"),
    ),
    "traverse_relations": lambda a, md: traverse_relations(
        a.get("entity", ""), md,
        a.get("direction", "both"),
        a.get("max_depth", 2),
    ),
    "search_memory_by_time": lambda a, md: search_by_time(
        md,
        a.get("since"), a.get("until"),
        a.get("limit", 20),
        branch_filter=a.get("branch_filter"),
        entity_type=a.get("entity_type"),
    ),
    "create_entities": lambda a, md: create_entities(
        a.get("entities", []), md,
    ),
    "create_relations": lambda a, md: create_relations(
        a.get("relations", []), md,
    ),
    "add_observations": lambda a, md: add_observations(
        a.get("entity", ""),
        a.get("observations", []),
        md,
    ),
    "delete_entities": lambda a, md: delete_entities(
        a.get("entity_names", []), md,
    ),
    "create_decision": lambda a, md: create_decision(a, md),
    "update_decision_outcome": lambda a, md:
        update_decision_outcome(a, md),
    "list_decisions": lambda a, md: list_decisions(
        md, stale_days=a.get("stale_days"),
    ),
    "remove_observations": lambda a, md: remove_observations(
        a.get("entity", ""),
        a.get("observations", []),
        md,
    ),
    "rename_entity": lambda a, md: rename_entity(
        a.get("old_name", ""),
        a.get("new_name", ""),
        md,
    ),
    "graph_stats": lambda a, md: graph_stats(md),
}


def _dispatch_tool_call(tool_name, args, memory_dir):
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return None
    return handler(args, memory_dir)


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

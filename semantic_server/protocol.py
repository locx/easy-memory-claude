"""MCP protocol: tool schemas and JSON-RPC 2.0 message handling."""
import json
import sys

from .config import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION
from .graph import load_index
from .recall import load_recall_counts
from .search import search, search_by_time
from .tools import (
    add_observations,
    create_entities,
    create_relations,
    delete_entities,
)
from .traverse import traverse_relations

TOOLS = [
    {
        "name": "semantic_search_memory",
        "description": (
            "Search the memory knowledge graph using "
            "TF-IDF semantic similarity. Returns entities "
            "ranked by relevance to the query, with their "
            "type and top observations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language search query"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": (
                        "Number of results to return "
                        "(default 5)"
                    ),
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "traverse_relations",
        "description": (
            "Traverse the memory knowledge graph from a "
            "start entity, following relations up to "
            "max_depth hops. Returns connected subgraph "
            "with nodes and edges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Start entity name",
                },
                "direction": {
                    "type": "string",
                    "enum": [
                        "outbound", "inbound", "both"
                    ],
                    "description": (
                        "Traversal direction (default both)"
                    ),
                    "default": "both",
                },
                "max_depth": {
                    "type": "integer",
                    "description": (
                        "Max hops to traverse (1-5, "
                        "default 2)"
                    ),
                    "default": 2,
                },
            },
            "required": ["entity"],
        },
    },
    {
        "name": "search_memory_by_time",
        "description": (
            "Search memory entities by time range. "
            "Returns entities updated/created within the "
            "window, sorted by most recent first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "ISO date start (e.g. "
                        "2026-03-01T00:00:00Z)"
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "ISO date end (e.g. "
                        "2026-03-13T23:59:59Z)"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max results (default 20)"
                    ),
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "create_entities",
        "description": (
            "Create or merge entities in the knowledge "
            "graph. If an entity with the same name exists, "
            "observations are merged and _updated refreshed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "description": (
                        "List of entities to create"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": (
                                    "Entity name"
                                ),
                            },
                            "entityType": {
                                "type": "string",
                                "description": (
                                    "Type (Module, "
                                    "Component, Pattern, "
                                    "Architecture, etc.)"
                                ),
                            },
                            "observations": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                },
                                "description": (
                                    "Facts about the "
                                    "entity"
                                ),
                            },
                        },
                        "required": [
                            "name", "entityType",
                            "observations",
                        ],
                    },
                },
            },
            "required": ["entities"],
        },
    },
    {
        "name": "create_relations",
        "description": (
            "Create directed relations between entities. "
            "Duplicates are silently skipped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "relations": {
                    "type": "array",
                    "description": (
                        "List of relations to create"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "from": {
                                "type": "string",
                                "description": (
                                    "Source entity name"
                                ),
                            },
                            "to": {
                                "type": "string",
                                "description": (
                                    "Target entity name"
                                ),
                            },
                            "relationType": {
                                "type": "string",
                                "description": (
                                    "Relation type "
                                    "(uses, contains, "
                                    "imports, etc.)"
                                ),
                            },
                        },
                        "required": [
                            "from", "to",
                            "relationType",
                        ],
                    },
                },
            },
            "required": ["relations"],
        },
    },
    {
        "name": "add_observations",
        "description": (
            "Add new observations to an existing entity. "
            "Duplicate observations are skipped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity name",
                },
                "observations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "New observations to add"
                    ),
                },
            },
            "required": ["entity", "observations"],
        },
    },
    {
        "name": "delete_entities",
        "description": (
            "Delete entities by name. Relations involving "
            "deleted entities are cascade-removed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Names of entities to delete"
                    ),
                },
            },
            "required": ["entity_names"],
        },
    },
]


def handle_message(msg, memory_dir):
    """Handle a single JSON-RPC 2.0 message.

    Wraps tool calls in try/except for robustness.
    """
    if not isinstance(msg, dict):
        return None

    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        load_index(memory_dir)
        load_recall_counts(memory_dir)
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
            if tool_name == "semantic_search_memory":
                result = search(
                    args.get("query", ""),
                    memory_dir,
                    args.get("top_k", 5),
                )
            elif tool_name == "traverse_relations":
                result = traverse_relations(
                    args.get("entity", ""),
                    memory_dir,
                    args.get("direction", "both"),
                    args.get("max_depth", 2),
                )
            elif tool_name == "search_memory_by_time":
                result = search_by_time(
                    memory_dir,
                    args.get("since"),
                    args.get("until"),
                    args.get("limit", 20),
                )
            elif tool_name == "create_entities":
                result = create_entities(
                    args.get("entities", []),
                    memory_dir,
                )
            elif tool_name == "create_relations":
                result = create_relations(
                    args.get("relations", []),
                    memory_dir,
                )
            elif tool_name == "add_observations":
                result = add_observations(
                    args.get("entity", ""),
                    args.get("observations", []),
                    memory_dir,
                )
            elif tool_name == "delete_entities":
                result = delete_entities(
                    args.get("entity_names", []),
                    memory_dir,
                )
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": (
                            f"Unknown tool: {tool_name}"
                        ),
                    },
                }
        except Exception as exc:
            sys.stderr.write(
                f"error: {tool_name}: {exc}\n"
            )
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "error": str(exc),
                            "results": [],
                        }),
                    }],
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result),
                    }
                ]
            },
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

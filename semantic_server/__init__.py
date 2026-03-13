"""Minimal MCP server for semantic memory search.

Pure Python — zero external dependencies.
Uses TF-IDF cosine similarity over the knowledge graph.
Communicates via JSON-RPC 2.0 over stdio (MCP stdio transport).

Usage:
    MEMORY_DIR=/path/to/.memory python3 -m semantic_server
"""
from .server import main

__all__ = ["main"]

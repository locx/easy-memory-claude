#!/usr/bin/env python3
"""Backwards-compatible shim — delegates to semantic_server package.

Existing .mcp.json configs may reference this file directly.
New installs use: python3 -m semantic_server
"""
import os
import sys

# Ensure the package is importable from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from semantic_server import main
except ImportError as exc:
    print(
        f"Error: cannot import semantic_server package: {exc}\n"
        f"Ensure the semantic_server/ directory exists at: "
        f"{os.path.dirname(os.path.abspath(__file__))}",
        file=sys.stderr,
    )
    sys.exit(1)

main()

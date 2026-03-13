#!/usr/bin/env python3
"""Backwards-compatible shim — delegates to semantic_server package.

Existing .mcp.json configs may reference this file directly.
New installs use: python3 -m semantic_server
"""
import os
import sys

# Ensure the package is importable from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from semantic_server import main

main()

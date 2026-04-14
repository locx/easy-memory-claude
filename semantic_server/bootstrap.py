"""Shared startup wiring used by both CLI and MCP server paths.

Keeps CLI (memory-cli.py) and server (semantic_server.server) in sync —
a bug fixed in one path won't drift in the other.
"""
import os
import sys

from .config import SERVER_NAME, init_branch


def ensure_memory_dir(memory_dir):
    """Create memory_dir if missing. Returns True on success."""
    if os.path.isdir(memory_dir):
        return True
    try:
        os.makedirs(memory_dir, exist_ok=True)
        # "a" mode creates if not exists, no-op if exists
        graph_path = os.path.join(memory_dir, "graph.jsonl")
        with open(graph_path, "a"):
            pass
        return True
    except OSError as exc:
        sys.stderr.write(
            f"{SERVER_NAME}: warning: MEMORY_DIR "
            f"'{memory_dir}' could not be created: {exc}\n"
        )
        return False


def bootstrap(memory_dir, load_index_on_start=True):
    """Initialize branch + recall + index for either entry point.

    Args:
        memory_dir: path to .memory directory
        load_index_on_start: if True, also warm-load the TF-IDF index

    Returns True on successful bootstrap; False otherwise.
    """
    if not ensure_memory_dir(memory_dir):
        return False

    # MEMORY_DIR convention: <project>/.memory
    project_dir = os.path.dirname(memory_dir)
    init_branch(project_dir)

    # Lazy imports — recall and graph import config, so bootstrap
    # must not be imported by config to avoid cycles.
    from .recall import init_recall_state
    try:
        init_recall_state(memory_dir)
    except Exception as exc:
        sys.stderr.write(
            f"{SERVER_NAME}: warning: recall init failed: {exc}\n"
        )

    if load_index_on_start:
        from .graph import load_index
        try:
            load_index(memory_dir)
        except Exception as exc:
            sys.stderr.write(
                f"{SERVER_NAME}: warning: index load failed: {exc}\n"
            )

    return True

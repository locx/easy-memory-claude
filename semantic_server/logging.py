"""Event logging and session stats for the MCP server.

Separated from server.py to avoid circular imports.
All modules can import from here safely.
"""
import sys

# Per-session activity counters (reset on initialize)
session_stats = {
    "searches": 0,
    "entities_created": 0,
    "relations_created": 0,
    "observations_added": 0,
    "entities_deleted": 0,
    "warnings_surfaced": 0,
    "pending_merged": 0,
}


def reset_session_stats():
    """Reset all session counters to zero."""
    for k in session_stats:
        session_stats[k] = 0


def log_event(event_type, details=""):
    """Emit structured event to stderr for user visibility.

    Format: [memory] EVENT_TYPE details
    All MCP server logs go to stderr (stdout is JSON-RPC).
    """
    try:
        sys.stderr.write(
            f"[memory] {event_type} {details}\n"
        )
        sys.stderr.flush()
    except OSError:
        pass

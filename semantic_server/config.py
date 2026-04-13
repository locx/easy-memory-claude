"""Constants, limits, patterns, and event logging.

Safe anchor for all modules — imports nothing from the package.
"""
import os
import re
import sys
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memory-semantic-search"
SERVER_VERSION = "3.0.0"

MAX_INPUT_CHARS = 10_000_000
MAX_TOP_K = 100
MAX_QUERY_CHARS = 10_000
MAX_RECALL_ENTRIES = 10_000
MAX_ENTITY_COUNT = 100_000
MAX_CANDIDATES = 1000
MAX_CACHE_BYTES = 50_000_000

RECALL_CHECK_INTERVAL = 60
RECALL_FLUSH_INTERVAL = 60
GRAPH_LOCK_TIMEOUT = 5.0
PARSE_TIME_BUDGET = 10.0
INDEX_CHECK_INTERVAL = 5.0

MAX_ENTITIES_PER_CALL = 50
MAX_RELATIONS_PER_CALL = 100
MAX_OBS_PER_CALL = 50
MAX_OBS_LENGTH = 5000
MAX_GRAPH_BYTES = 50_000_000
MAX_CACHED_OBS = 20

RE_WORDS = re.compile(r'\w+')


def now_iso():
    """Current UTC timestamp in ISO format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_iso_ts(ts):
    """Normalize ISO timestamp for safe lexicographic sort.

    Fast path for well-formed timestamps (>99% case).
    """
    if not ts or not isinstance(ts, str):
        return ""
    if (len(ts) >= 10 and ts[4] == '-' and ts[7] == '-'
            and ts[:4].isdigit() and ts[5:7].isdigit()
            and ts[8:10].isdigit()):
        month = int(ts[5:7])
        day = int(ts[8:10])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return ts
    try:
        parts = ts.split('T', 1)
        dp = parts[0].split('-')
        if len(dp) == 3:
            fixed = (
                f"{int(dp[0]):04d}-"
                f"{int(dp[1]):02d}-"
                f"{int(dp[2]):02d}"
            )
            if len(parts) > 1:
                return fixed + 'T' + parts[1]
            return fixed
    except (ValueError, IndexError):
        pass
    return ts


# --- Event logging ---

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
    """Emit [memory] EVENT_TYPE details to stderr."""
    try:
        sys.stderr.write(
            f"[memory] {event_type} {details}\n"
        )
        sys.stderr.flush()
    except OSError:
        pass


# --- Branch detection ---

MAIN_BRANCHES = frozenset({
    "main", "master", "trunk", "develop",
})
_BRANCH_CHECK_INTERVAL = 60.0

_current_branch = ""
_branch_check_mono = 0.0
_project_dir = ""


def _read_git_head(project_dir):
    """Read branch from .git/HEAD (<0.1ms)."""
    git_head = os.path.join(project_dir, ".git", "HEAD")
    try:
        with open(git_head) as f:
            content = f.read(256).strip()
        if content.startswith("ref: refs/heads/"):
            return content[16:]
        if content.startswith("ref: "):
            return content[5:].rsplit("/", 1)[-1]
        if len(content) >= 8:
            return content[:12]
        return ""
    except OSError:
        return ""


def init_branch(project_dir):
    """Seed branch state at startup."""
    global _project_dir, _current_branch
    global _branch_check_mono
    _project_dir = project_dir
    _current_branch = (
        _read_git_head(project_dir) or "unknown"
    )
    _branch_check_mono = time.monotonic()


def refresh_branch():
    """Re-read branch if interval expired. Returns (branch, changed)."""
    global _current_branch, _branch_check_mono
    now = time.monotonic()
    if now - _branch_check_mono < _BRANCH_CHECK_INTERVAL:
        return _current_branch, False
    _branch_check_mono = now
    branch = _read_git_head(_project_dir) or "unknown"
    changed = branch != _current_branch
    if changed:
        _current_branch = branch
    return _current_branch, changed


def get_current_branch():
    """Return cached current branch name."""
    return _current_branch

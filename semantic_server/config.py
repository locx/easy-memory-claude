"""Constants, limits, and pre-compiled patterns for the MCP server."""
import re
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memory-semantic-search"
SERVER_VERSION = "3.0.0"

MAX_INPUT_CHARS = 10_000_000  # 10 MB — reject oversized lines
MAX_TOP_K = 100               # cap result count
MAX_QUERY_CHARS = 10_000      # cap query length
MAX_RECALL_ENTRIES = 10_000   # cap recall dict size
MAX_ENTITY_COUNT = 100_000    # cap parsed entity count
MAX_CANDIDATES = 1000         # cap search candidate set
MAX_CACHE_BYTES = 50_000_000  # 50 MB combined cache cap

RECALL_CHECK_INTERVAL = 60    # seconds between recall mtime checks
RECALL_FLUSH_INTERVAL = 60    # seconds between recall flushes
GRAPH_LOCK_TIMEOUT = 5.0      # seconds before lock acquisition fails
GRAPH_LOCK_RETRIES = 50       # attempts (100ms sleep between)
PARSE_TIME_BUDGET = 10.0      # max seconds for graph file parse
INDEX_CHECK_INTERVAL = 5.0    # seconds between stat() checks

MAX_ENTITIES_PER_CALL = 50
MAX_RELATIONS_PER_CALL = 100
MAX_OBS_PER_CALL = 50
MAX_OBS_LENGTH = 5000
MAX_GRAPH_BYTES = 50_000_000  # 50 MB write guard
MAX_CACHED_OBS = 3            # obs kept per entity in read cache

# Pre-compiled regex
RE_WORDS = re.compile(r'\w+')


def now_iso():
    """Current UTC timestamp in ISO format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_iso_ts(ts):
    """Normalize ISO timestamp for safe lexicographic sort.

    Canonical implementation — imported by graph.py and
    search.py. Fast path for well-formed timestamps
    (>99% case).
    """
    if not ts or not isinstance(ts, str):
        return ""
    if (len(ts) >= 10 and ts[4] == '-' and ts[7] == '-'
            and ts[:4].isdigit() and ts[5:7].isdigit()
            and ts[8:10].isdigit()):
        # Validate month (01-12) and day (01-31) bounds
        month = int(ts[5:7])
        day = int(ts[8:10])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return ts
        # Fall through to normalization path
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

"""Constants, limits, and pre-compiled patterns for the MCP server."""
import re
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memory-semantic-search"
SERVER_VERSION = "2.3.0"

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

"""Main MCP server loop: stdio transport, signal handling."""
import atexit
import json
import os
import signal
import sys
import time

from .config import (
    INDEX_CHECK_INTERVAL,
    MAX_INPUT_CHARS,
    RECALL_FLUSH_INTERVAL,
    SERVER_NAME,
    SERVER_VERSION,
)
from .cache import index_cache
from .graph import load_index
from .protocol import handle_message
from . import recall as _recall_mod
from .recall import flush_recall_counts

_shutdown_requested = False


def _shutdown_handler(signum, frame):
    """Signal handler — set flag for cooperative shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


def main():
    """Run MCP server on stdio."""
    import semantic_server.cache as _cache_mod

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    memory_dir = os.environ.get(
        "MEMORY_DIR",
        os.path.join(os.getcwd(), ".memory"),
    )

    if not os.path.isdir(memory_dir):
        sys.stderr.write(
            f"{SERVER_NAME}: warning: MEMORY_DIR "
            f"'{memory_dir}' does not exist\n"
        )
        sys.stderr.flush()

    # atexit for recall persistence
    atexit.register(flush_recall_counts)

    sys.stderr.write(
        f"{SERVER_NAME} v{SERVER_VERSION} "
        f"ready (memory_dir={memory_dir})\n"
    )
    sys.stderr.flush()

    try:
        while not _shutdown_requested:
            # Cooperative index reload throttled to
            # 1 stat() per INDEX_CHECK_INTERVAL
            _now_mono = time.monotonic()
            if (_now_mono - _cache_mod.last_index_check
                    >= INDEX_CHECK_INTERVAL):
                _cache_mod.last_index_check = _now_mono
                idx_path = os.path.join(
                    memory_dir, "tfidf_index.json"
                )
                try:
                    idx_mtime = os.path.getmtime(idx_path)
                    if (index_cache["data"] is not None
                            and index_cache["path"]
                            == idx_path
                            and index_cache["mtime"]
                            != idx_mtime):
                        load_index(memory_dir)
                except OSError:
                    pass

            # Flush recall counts between requests
            if _recall_mod.recall_dirty:
                now = time.monotonic()
                if (now - _recall_mod.recall_last_flush
                        > RECALL_FLUSH_INTERVAL):
                    flush_recall_counts()

            try:
                line = sys.stdin.readline()
            except (EOFError, UnicodeDecodeError, OSError):
                break
            if not line:
                break
            if _shutdown_requested:
                break

            if len(line) > MAX_INPUT_CHARS:
                sys.stderr.write(
                    "warn: oversized input dropped "
                    f"({len(line)} chars)\n"
                )
                continue

            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                sys.stderr.write(
                    f"warn: malformed input: "
                    f"{line[:100]}\n"
                )
                continue

            response = handle_message(msg, memory_dir)
            if response is not None:
                try:
                    sys.stdout.write(
                        json.dumps(response) + "\n"
                    )
                    sys.stdout.flush()
                except BrokenPipeError:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        flush_recall_counts()
        try:
            sys.stderr.write(
                "semantic_server: shutting down\n"
            )
            sys.stderr.flush()
        except OSError:
            pass

"""Main MCP server loop: stdio transport, signal handling."""
import atexit
import json
import logging
import os
import select
import signal
import sys
import time
import threading

from . import cache as _cache_mod
from ._json import loads as _fast_loads
from ._json import dumps as _fast_dumps
from .io_utils import merge_pending as _merge_pending_impl

from .config import (
    INDEX_CHECK_INTERVAL,
    MAX_INPUT_CHARS,
    RECALL_FLUSH_INTERVAL,
    SERVER_NAME,
    SERVER_VERSION,
    refresh_branch,
    log_event,
    session_stats,
)
from .bootstrap import bootstrap
from .cache import index_cache
from .graph import (
    load_index,
    append_jsonl,
    invalidate_entity_cache_only,
    invalidate_relation_cache_only,
)
from .protocol import handle_message
from . import recall as _recall_mod
from .recall import flush_recall_counts

_log = logging.getLogger(__name__)

_shutdown_requested = False

_PENDING_CHECK_INTERVAL = 5.0
_last_pending_check = 0.0

_EVICT_TICK_INTERVAL = 30.0
_last_evict_tick = 0.0

_MERGE_TIME_BUDGET = 0.1
_INDEX_DEBOUNCE_SECS = 0.5
_last_mtime_seen = 0.0
_last_mtime_time = 0.0

_graph_lock = threading.Lock()


def _shutdown_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def _invalidate_both():
    invalidate_entity_cache_only()
    invalidate_relation_cache_only()


def _merge_pending(memory_dir):
    """Thin wrapper: delegate to io_utils.merge_pending under graph lock."""
    global _last_pending_check
    _last_pending_check = time.monotonic()

    pending_path = os.path.join(memory_dir, "graph.jsonl.pending")
    graph_path = os.path.join(memory_dir, "graph.jsonl")

    t0 = time.monotonic()
    lines, _bytes = _merge_pending_impl(
        memory_dir,
        graph_path,
        pending_path,
        lock=_graph_lock,
        invalidate_cb=_invalidate_both,
    )
    elapsed = time.monotonic() - t0
    if elapsed > _MERGE_TIME_BUDGET:
        _log.debug(
            "merge_pending over budget: %.3fs (lines=%d)", elapsed, lines
        )
    if lines:
        session_stats["pending_merged"] += lines
        log_event("MERGE_PENDING", f"{lines} entries from hook sidecar")


def _run_periodic_tasks(now_mono, memory_dir, idx_path):
    global _last_mtime_seen, _last_mtime_time, _last_evict_tick

    if (now_mono - _cache_mod.last_index_check
            >= INDEX_CHECK_INTERVAL):
        _cache_mod.last_index_check = now_mono
        try:
            idx_mtime = os.path.getmtime(idx_path)
            if (index_cache["data"] is not None
                    and index_cache["path"] == idx_path
                    and index_cache["mtime"] != idx_mtime):
                if idx_mtime != _last_mtime_seen:
                    _last_mtime_seen = idx_mtime
                    _last_mtime_time = now_mono
                elif now_mono - _last_mtime_time >= _INDEX_DEBOUNCE_SECS:
                    load_index(memory_dir)
                    _last_mtime_seen = 0.0
        except OSError:
            pass

    if (now_mono - _last_pending_check
            >= _PENDING_CHECK_INTERVAL):
        _merge_pending(memory_dir)

    if _recall_mod.recall_dirty:
        if (now_mono - _recall_mod.recall_last_flush
                > RECALL_FLUSH_INTERVAL):
            flush_recall_counts()

    if now_mono - _last_evict_tick >= _EVICT_TICK_INTERVAL:
        _last_evict_tick = now_mono
        _cache_mod.maybe_evict_caches()

    branch, changed = refresh_branch()
    if changed:
        log_event("BRANCH_SWITCH", f"now on {branch}")


def main():
    """Run MCP server on stdio with select-based I/O."""

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    memory_dir = os.environ.get(
        "MEMORY_DIR",
        os.path.join(os.getcwd(), ".memory"),
    )

    bootstrap(memory_dir, load_index_on_start=False)
    sys.stderr.flush()

    atexit.register(flush_recall_counts)

    sys.stderr.write(
        f"{SERVER_NAME} v{SERVER_VERSION} "
        f"ready (memory_dir={memory_dir})\n"
    )
    sys.stderr.flush()

    idx_path = os.path.join(
        memory_dir, "tfidf_index.json"
    )

    buf = b""
    stdin_raw = sys.stdin.buffer

    try:
        while not _shutdown_requested:
            _now_mono = time.monotonic()
            _run_periodic_tasks(_now_mono, memory_dir, idx_path)

            try:
                ready, _, _ = select.select(
                    [stdin_raw], [], [], 1.0
                )
            except (ValueError, OSError):
                break

            if not ready:
                continue

            try:
                chunk = stdin_raw.read1(65536)
            except (EOFError, OSError):
                break
            if not chunk:
                break
            if _shutdown_requested:
                break

            buf += chunk

            while b"\n" in buf:
                nl = buf.index(b"\n")
                raw_line = buf[:nl]
                buf = buf[nl + 1:]

                if len(raw_line) > MAX_INPUT_CHARS:
                    sys.stderr.write(
                        "warn: oversized input dropped "
                        f"({len(raw_line)} bytes)\n"
                    )
                    continue

                try:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue
                if not line:
                    continue

                try:
                    msg = _fast_loads(line)
                except (json.JSONDecodeError, ValueError):
                    sys.stderr.write(
                        f"warn: malformed input: "
                        f"{line[:100]}\n"
                    )
                    continue
                if not isinstance(msg, dict):
                    continue

                try:
                    response = handle_message(
                        msg, memory_dir,
                    )
                except Exception as exc:
                    sys.stderr.write(
                        f"error: handle_message: {exc}\n"
                    )
                    msg_id = msg.get("id")
                    if msg_id is not None:
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {
                                "code": -32603,
                                "message": str(exc),
                            },
                        }
                    else:
                        response = None
                if response is not None:
                    try:
                        sys.stdout.write(
                            _fast_dumps(response) + "\n"
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

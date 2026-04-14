"""Main MCP server loop: stdio transport, signal handling."""
import atexit
import json
import os
import select
import signal
import sys
import time

from ._json import loads as _fast_loads
from ._json import dumps as _fast_dumps

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

_shutdown_requested = False

_PENDING_CHECK_INTERVAL = 5.0
_last_pending_check = 0.0


def _shutdown_handler(signum, frame):
    """Set flag for cooperative shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


def _merge_pending(memory_dir):
    """Merge hook sidecar buffer into graph.jsonl.

    Rename-before-read for crash safety: pending →
    .processing → read → append → delete.
    """
    global _last_pending_check
    _last_pending_check = time.monotonic()

    pending_path = os.path.join(
        memory_dir, "graph.jsonl.pending"
    )
    processing_path = pending_path + ".processing"

    have_processing = os.path.exists(processing_path)
    if not have_processing:
        try:
            size = os.path.getsize(pending_path)
        except OSError:
            return
        if size == 0:
            return
        try:
            os.rename(pending_path, processing_path)
        except OSError:
            return

    try:
        entries = []
        with open(
            processing_path, encoding="utf-8",
            errors="replace",
        ) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _fast_loads(line)
                    if isinstance(obj, dict):
                        entries.append(obj)
                except (json.JSONDecodeError, ValueError):
                    continue
        if entries:
            ok = append_jsonl(
                memory_dir, entries,
            )
            if not ok:
                log_event(
                    "MERGE_PENDING_FAIL",
                    f"{len(entries)} entries deferred "
                    f"(lock timeout)",
                )
                return
            invalidate_entity_cache_only()
            invalidate_relation_cache_only()
            session_stats["pending_merged"] += len(entries)
            log_event(
                "MERGE_PENDING",
                f"{len(entries)} entries from hook sidecar",
            )
        try:
            os.unlink(processing_path)
        except OSError:
            pass
    except OSError:
        pass


def _run_periodic_tasks(now_mono, memory_dir, idx_path):
    import semantic_server.cache as _cache_mod
    global _last_pending_check

    if (now_mono - _cache_mod.last_index_check
            >= INDEX_CHECK_INTERVAL):
        _cache_mod.last_index_check = now_mono
        try:
            idx_mtime = os.path.getmtime(idx_path)
            if (index_cache["data"] is not None
                    and index_cache["path"] == idx_path
                    and index_cache["mtime"] != idx_mtime):
                load_index(memory_dir)
        except OSError:
            pass

    if (now_mono - _last_pending_check
            >= _PENDING_CHECK_INTERVAL):
        _merge_pending(memory_dir)

    if _recall_mod.recall_dirty:
        if (now_mono - _recall_mod.recall_last_flush
                > RECALL_FLUSH_INTERVAL):
            flush_recall_counts()

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

    try:
        while not _shutdown_requested:
            _now_mono = time.monotonic()
            _run_periodic_tasks(_now_mono, memory_dir, idx_path)

            # --- Non-blocking stdin (1s timeout for tasks) ---
            try:
                ready, _, _ = select.select(
                    [sys.stdin], [], [], 1.0
                )
            except (ValueError, OSError):
                break

            if not ready:
                continue

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

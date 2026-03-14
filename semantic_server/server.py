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
)
from .cache import index_cache
from .graph import load_index, append_jsonl
from .protocol import handle_message
from . import recall as _recall_mod
from .recall import flush_recall_counts

from .logging import log_event, session_stats

_shutdown_requested = False

# Interval for merging hook sidecar buffer
_PENDING_CHECK_INTERVAL = 5.0
_last_pending_check = 0.0


def _shutdown_handler(signum, frame):
    """Signal handler — set flag for cooperative shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


def _merge_pending(memory_dir):
    """Merge hook sidecar buffer into graph.jsonl.

    Uses rename-before-read pattern for crash safety:
    pending → .processing → read → append → delete.
    If .processing exists on next call, it's a leftover
    from a prior crash and gets reprocessed.
    """
    global _last_pending_check
    _last_pending_check = time.monotonic()

    pending_path = os.path.join(
        memory_dir, "graph.jsonl.pending"
    )
    processing_path = pending_path + ".processing"

    # Check for leftover from prior crash first
    have_processing = os.path.exists(processing_path)
    if not have_processing:
        try:
            size = os.path.getsize(pending_path)
        except OSError:
            return
        if size == 0:
            return
        # Atomic rename — prevents duplicate processing
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
                # Lock timeout — keep .processing for retry
                log_event(
                    "MERGE_PENDING_FAIL",
                    f"{len(entries)} entries deferred "
                    f"(lock timeout)",
                )
                return
            session_stats["pending_merged"] += len(entries)
            log_event(
                "MERGE_PENDING",
                f"{len(entries)} entries from hook sidecar",
            )
        # Delete only after successful merge
        try:
            os.unlink(processing_path)
        except OSError:
            pass
    except OSError:
        pass


def main():
    """Run MCP server on stdio with select-based I/O."""
    import semantic_server.cache as _cache_mod
    global _last_pending_check

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    memory_dir = os.environ.get(
        "MEMORY_DIR",
        os.path.join(os.getcwd(), ".memory"),
    )

    if not os.path.isdir(memory_dir):
        try:
            os.makedirs(memory_dir, exist_ok=True)
            sys.stderr.write(
                f"{SERVER_NAME}: created MEMORY_DIR "
                f"'{memory_dir}'\n"
            )
        except OSError as exc:
            sys.stderr.write(
                f"{SERVER_NAME}: warning: MEMORY_DIR "
                f"'{memory_dir}' does not exist and "
                f"could not be created: {exc}\n"
            )
        sys.stderr.flush()

    # atexit for recall persistence
    atexit.register(flush_recall_counts)

    sys.stderr.write(
        f"{SERVER_NAME} v{SERVER_VERSION} "
        f"ready (memory_dir={memory_dir})\n"
    )
    sys.stderr.flush()

    # Pre-compute paths for consolidated stat checks
    idx_path = os.path.join(
        memory_dir, "tfidf_index.json"
    )

    try:
        while not _shutdown_requested:
            _now_mono = time.monotonic()

            # --- Periodic tasks (run during idle) ---

            # Cooperative index reload — single stat()
            if (_now_mono - _cache_mod.last_index_check
                    >= INDEX_CHECK_INTERVAL):
                _cache_mod.last_index_check = _now_mono
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

            # Merge hook sidecar buffer
            if (_now_mono - _last_pending_check
                    >= _PENDING_CHECK_INTERVAL):
                _merge_pending(memory_dir)

            # Flush recall counts
            if _recall_mod.recall_dirty:
                if (_now_mono - _recall_mod.recall_last_flush
                        > RECALL_FLUSH_INTERVAL):
                    flush_recall_counts()

            # --- Non-blocking stdin via select ---
            # Timeout allows periodic tasks to run
            # even when client is idle.
            try:
                ready, _, _ = select.select(
                    [sys.stdin], [], [], 1.0
                )
            except (ValueError, OSError):
                # stdin closed or invalid fd
                break

            if not ready:
                continue  # timeout — loop back for tasks

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
                # Return JSON-RPC internal error
                msg_id = (
                    msg.get("id")
                    if isinstance(msg, dict) else None
                )
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

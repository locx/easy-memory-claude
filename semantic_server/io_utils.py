"""JSONL I/O and graph partitioning utilities.

Extracted from maintenance.py to standardize I/O across the codebase.
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional, Callable, Tuple

from ._json import loads as _loads, dumps as _dumps

_log = logging.getLogger(__name__)


def iter_jsonl(path):
    """Yield dicts from JSONL file, skip malformed lines."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        obj = _loads(line)
                        if isinstance(obj, dict):
                            yield obj
                    except (ValueError, OverflowError):
                        continue
    except OSError:
        return


def partition_graph(path):
    """Single-pass JSONL partition into (entities, relations, others)."""
    entities, relations, others = [], [], []
    for e in iter_jsonl(path):
        t = e.get("type")
        if t == "entity":
            entities.append(e)
        elif t == "relation":
            relations.append(e)
        else:
            others.append(e)
    return entities, relations, others


def _safe_jsonl_lines(entries):
    """Yield JSONL lines, skipping unserializable entries."""
    for e in entries:
        try:
            yield _dumps(e) + "\n"
        except (TypeError, ValueError, OverflowError):
            continue


def write_jsonl(path, entries):
    """Atomic write via .new + os.replace. Skips unserializable."""
    tmp = path + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(_safe_jsonl_lines(entries))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_pending(
    memory_dir: Path,
    graph_file: Path,
    pending_file: Path,
    *,
    lock=None,
    invalidate_cb: Optional[Callable[[], None]] = None,
) -> Tuple[int, int]:
    """Merge .pending sidecar into graph_file atomically.

    Uses O_EXCL rotation: pending -> pending.processing -> read ->
    append -> unlink processing. Recovers existing .processing on
    crash restart. Lock is acquired BEFORE size check to eliminate
    TOCTOU race. Returns (lines_merged, bytes_merged).
    """
    memory_dir = Path(memory_dir)
    pending_path = Path(pending_file)
    processing_path = Path(str(pending_file) + ".processing")
    graph_path = Path(graph_file)

    def _do_merge() -> Tuple[int, int]:
        have_processing = processing_path.exists()
        if not have_processing:
            try:
                size = pending_path.stat().st_size
            except OSError:
                return (0, 0)
            if size == 0:
                return (0, 0)
            try:
                os.rename(pending_path, processing_path)
            except OSError:
                return (0, 0)

        entries = []
        bytes_read = 0
        try:
            with open(
                processing_path, encoding="utf-8", errors="replace"
            ) as f:
                for line in f:
                    bytes_read += len(line.encode("utf-8", errors="replace"))
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = _loads(line)
                        if isinstance(obj, dict):
                            entries.append(obj)
                    except (ValueError, OverflowError):
                        continue
        except OSError:
            return (0, 0)

        if not entries:
            try:
                processing_path.unlink()
            except OSError:
                pass
            return (0, 0)

        try:
            with open(graph_path, "a", encoding="utf-8") as gf:
                for entry in entries:
                    try:
                        gf.write(_dumps(entry) + "\n")
                    except (TypeError, ValueError, OverflowError):
                        continue
                gf.flush()
        except OSError as exc:
            _log.warning("merge_pending: append failed: %s", exc)
            return (0, 0)

        try:
            processing_path.unlink()
        except OSError as exc:
            _log.warning(
                "merge_pending: unlink processing failed: %s", exc
            )

        if invalidate_cb is not None:
            try:
                invalidate_cb()
            except Exception:
                pass

        return (len(entries), bytes_read)

    if lock is not None:
        with lock:
            return _do_merge()
    return _do_merge()

"""Recall tracking — Hebbian reinforcement for search results.

Tracks entity recall frequency to boost relevance scoring.
OrderedDict for O(1) LRU eviction. Thread-safe via lock.
"""
import json
import os
import threading
import time
from collections import OrderedDict

from .config import (
    MAX_RECALL_ENTRIES,
    RECALL_CHECK_INTERVAL,
    RECALL_FLUSH_INTERVAL,
)

recall_counts = OrderedDict()
recall_dirty = False
recall_last_flush = 0.0
recall_path = ""
recall_mtime = 0.0
_last_recall_check = 0.0
_recall_lock = threading.Lock()


def init_recall_state(memory_dir):
    """Load recall counts from sidecar file."""
    global recall_counts, recall_path, recall_mtime
    recall_path = os.path.join(
        memory_dir, "recall_counts.json"
    )
    with _recall_lock:
        try:
            recall_mtime = os.path.getmtime(recall_path)
            with open(recall_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                recall_counts = OrderedDict(
                    (k, v) for k, v in data.items()
                    if isinstance(k, str)
                    and isinstance(v, (int, float))
                )
        except (OSError, json.JSONDecodeError, ValueError):
            recall_counts = OrderedDict()
            recall_mtime = 0.0


def maybe_reload_recall_counts():
    """Reload if file changed (throttled to 1 stat/interval)."""
    global recall_mtime, _last_recall_check
    if not recall_path:
        return
    now = time.monotonic()
    if now - _last_recall_check < RECALL_CHECK_INTERVAL:
        return
    _last_recall_check = now
    try:
        mtime = os.path.getmtime(recall_path)
    except OSError:
        return
    if mtime == recall_mtime:
        return
    with _recall_lock:
        try:
            with open(recall_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if not isinstance(k, str):
                        continue
                    if not isinstance(v, (int, float)):
                        continue
                    cur = recall_counts.get(k, 0)
                    if v > cur:
                        recall_counts[k] = v
            recall_mtime = mtime
        except (OSError, json.JSONDecodeError, ValueError):
            pass


def record_recalls(entity_names):
    """Increment recall counts (no I/O — flush is deferred)."""
    global recall_dirty
    with _recall_lock:
        for name in entity_names:
            recall_counts[name] = (
                recall_counts.get(name, 0) + 1
            )
            recall_counts.move_to_end(name)
        while len(recall_counts) > MAX_RECALL_ENTRIES:
            recall_counts.popitem(last=False)
        recall_dirty = True


def flush_recall_counts():
    """Atomic write of recall counts to disk (no fsync — non-critical)."""
    global recall_dirty, recall_last_flush, recall_mtime
    if not recall_path:
        with _recall_lock:
            recall_dirty = False
        return
    if not recall_dirty:
        return
    with _recall_lock:
        if not recall_dirty:
            return
        recall_last_flush = time.monotonic()
        tmp = recall_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    recall_counts, f,
                    separators=(",", ":"),
                )
                f.flush()
            os.replace(tmp, recall_path)
            recall_dirty = False
            try:
                recall_mtime = os.path.getmtime(recall_path)
            except OSError:
                pass
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

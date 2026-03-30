"""JSONL I/O and graph partitioning utilities.

Extracted from maintenance.py to standardize I/O across the codebase.
"""
import os
import json
from ._json import loads as _loads, dumps as _dumps

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
                    except (json.JSONDecodeError, ValueError, OverflowError):
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

#!/usr/bin/env python3
"""PostToolUse hook: capture observations + surface warnings.

On Edit/Write: checks graph for file-specific warnings and
related entities, outputs them to stdout for the agent.
Also logs activity to graph.pending sidecar.

On Bash: logs command to activity sidecar.

Usage: python3 capture_tool_context.py <input_json> <graph_path>
"""
import json
import os
import sys
import time


def _check_file_warnings(graph_path, filename, session_id):
    """Check graph for warnings/decisions about a file.

    Scans graph.jsonl for entities matching the filename
    with entityType 'file-warning' or observations
    containing [WARNING]. Also finds related decisions
    and dependencies via relations.

    Returns formatted warning string or empty string.
    Uses session marker to avoid repeating warnings.
    """
    if not filename or filename == '?':
        return ""

    # Per-file per-session dedup — use basename only
    # to avoid path traversal in marker filename
    safe_sid = "".join(
        c if c.isalnum() or c in ('_', '-')
        else '_' for c in session_id
    )[:64]
    safe_file = os.path.basename(filename)[:64]
    marker = (
        f"/tmp/.claude-mem-warned-"
        f"{safe_sid}-{safe_file}"
    )
    if os.path.exists(marker):
        return ""

    basename = os.path.basename(filename)
    # Also try relative path matching
    match_names = {basename, filename}

    warnings = []
    decisions = []
    relations_out = []

    try:
        with open(
            graph_path, encoding="utf-8",
            errors="replace",
        ) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "entity":
                        name = obj.get("name", "")
                        etype = obj.get("entityType", "")
                        obs = obj.get("observations", [])
                        # File warnings
                        if name in match_names \
                                and etype == "file-warning":
                            for o in obs:
                                if isinstance(o, str):
                                    warnings.append(o)
                        # Any entity with WARNING obs
                        # about this file
                        elif name in match_names:
                            for o in obs:
                                if isinstance(o, str) \
                                        and "[WARNING]" in o:
                                    warnings.append(o)
                        # Decisions scoped to this file
                        elif etype == "decision":
                            for o in obs:
                                if isinstance(o, str) \
                                        and basename in o:
                                    short = name
                                    if short.startswith(
                                        "decision: "
                                    ):
                                        short = short[10:]
                                    decisions.append(short)
                                    break
                    elif t == "relation":
                        fr = obj.get("from", "")
                        to = obj.get("to", "")
                        rt = obj.get(
                            "relationType", ""
                        )
                        if fr in match_names:
                            relations_out.append(
                                f"{rt} -> {to}"
                            )
                        elif to in match_names:
                            relations_out.append(
                                f"{fr} -{rt}-> "
                                f"{basename}"
                            )
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return ""

    if not warnings and not decisions \
            and not relations_out:
        return ""

    # Mark as shown for this session
    try:
        with open(marker, 'w') as f:
            f.write('1')
    except OSError:
        pass

    parts = []
    if warnings:
        parts.append(f"Warnings for {basename}:")
        for w in warnings[:5]:
            parts.append(f"  - {w[:200]}")
    if decisions:
        parts.append(f"Related decisions:")
        for d in decisions[:3]:
            parts.append(f"  - {d}")
    if relations_out:
        parts.append(f"Relations:")
        for r in relations_out[:5]:
            parts.append(f"  - {r}")

    return "\n".join(parts)


def main():
    if len(sys.argv) < 3:
        sys.exit(2)
    input_path = sys.argv[1]
    graph_path = sys.argv[2]

    try:
        with open(input_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        sys.exit(0)

    tool = data.get('tool_name', '')
    if tool not in ('Edit', 'Write', 'Bash', 'NotebookEdit'):
        sys.exit(2)  # non-matching — don't update marker

    # --- File warnings (Edit/Write only) ---
    file_path = None
    if tool in ('Edit', 'Write'):
        file_path = data.get('tool_input', {}).get(
            'file_path', '?'
        )
        session_id = os.environ.get(
            'CLAUDE_SESSION_ID', 'unknown'
        )
        warning_text = _check_file_warnings(
            graph_path, file_path, session_id
        )
        if warning_text:
            print(warning_text)

    # --- Activity logging ---
    if tool in ('Edit', 'Write'):
        base = os.path.basename(file_path or '?')
        verb = 'Edited' if tool == 'Edit' \
            else 'Created/wrote'
        obs = f'{verb} {base}'
    elif tool == 'Bash':
        cmd = str(
            data.get('tool_input', {}).get('command', '')
        )[:80]
        obs = f'Ran: {cmd}'
    else:
        obs = f'{tool} used'

    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    observation = f'[{ts}] {obs}'

    entry = json.dumps({
        'type': 'entity',
        'name': 'session-activity',
        'entityType': 'activity-log',
        'observations': [observation],
        '_created': ts,
        '_updated': ts,
    }, separators=(',', ':'))

    # Write to sidecar buffer — no lock needed.
    pending_path = graph_path + ".pending"
    try:
        with open(pending_path, 'a', encoding="utf-8") as f:
            f.write(entry + '\n')
            f.flush()
    except OSError:
        _write_direct(graph_path, entry)


def _write_direct(graph_path, entry):
    """Fallback: locked append to graph.jsonl."""
    try:
        import fcntl
        lock_path = os.path.join(
            os.path.dirname(graph_path), '.graph.lock'
        )
        lock_fd = open(lock_path, 'a')
        try:
            acquired = False
            delay = 0.025
            for _ in range(5):
                try:
                    fcntl.flock(
                        lock_fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    acquired = True
                    break
                except (IOError, OSError):
                    time.sleep(delay)
                    delay *= 2
            if not acquired:
                lock_fd.close()
                return
            with open(
                graph_path, 'a', encoding="utf-8"
            ) as f:
                f.write(entry + '\n')
                f.flush()
                os.fsync(f.fileno())
        finally:
            if acquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            lock_fd.close()
    except ImportError:
        try:
            with open(
                graph_path, 'a', encoding="utf-8"
            ) as f:
                f.write(entry + '\n')
        except OSError:
            pass
    except OSError:
        pass


if __name__ == '__main__':
    main()

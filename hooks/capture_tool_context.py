#!/usr/bin/env python3
"""PostToolUse hook: capture observations + surface warnings.

Usage: python3 capture_tool_context.py <input_json> <graph_path>
"""
import json
import os
import sys
import time

_MAX_PENDING_BYTES = 1_000_000
_WARN_SCAN_LINE_BUDGET = 5_000


def _check_file_warnings(graph_path, filename, session_id):
    """Check graph for warnings/decisions about a file."""
    if not filename or filename == '?':
        return ""

    safe_sid = "".join(
        c if c.isalnum() or c in ('_', '-')
        else '_' for c in session_id
    )[:64]
    safe_file = "".join(
        c if c.isalnum() or c in ('_', '-')
        else '_' for c in os.path.basename(filename)
    )[:64]
    marker = (
        f"/tmp/.claude-mem-warned-{safe_sid}-{safe_file}"
    )
    try:
        marker_age = time.time() - os.path.getmtime(marker)
        if 0 <= marker_age < 86400:  # suppress for 24h only
            return ""
        os.unlink(marker)  # expired — re-surface warning
    except OSError:
        pass  # marker doesn't exist — proceed

    basename = os.path.basename(filename)
    match_names = {basename, filename}

    warnings = []
    decisions = []
    relations_out = []
    line_count = 0

    try:
        with open(
            graph_path, encoding="utf-8",
            errors="replace",
        ) as f:
            for line in f:
                line_count += 1
                if line_count > _WARN_SCAN_LINE_BUDGET:
                    break
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
                        if name in match_names:
                            for o in obs:
                                if not isinstance(o, str):
                                    continue
                                if (etype == "file-warning"
                                        or "[WARNING]" in o):
                                    warnings.append(o)
                        elif etype == "decision":
                            for o in obs:
                                if (isinstance(o, str)
                                        and basename in o):
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
                        rt = obj.get("relationType", "")
                        if fr in match_names:
                            relations_out.append(
                                f"{rt} -> {to}"
                            )
                        elif to in match_names:
                            relations_out.append(
                                f"{fr} -{rt}-> {basename}"
                            )
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return ""

    if not warnings and not decisions \
            and not relations_out:
        return ""

    try:
        with open(marker, 'w') as f:
            f.write('1')
    except OSError:
        pass

    parts = []
    for items, header, limit in (
        (warnings, f"Warnings for {basename}:", 5),
        (decisions, "Related decisions:", 3),
        (relations_out, "Relations:", 5),
    ):
        if items:
            parts.append(header)
            for item in items[:limit]:
                parts.append(f"  - {item[:200]}")

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
        sys.exit(1)

    tool = data.get('tool_name', '')
    if tool not in ('Edit', 'Write', 'Bash', 'NotebookEdit'):
        sys.exit(2)

    # File warnings (Edit/Write only)
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

    # Activity logging
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

    pending_path = graph_path + ".pending"
    try:
        if os.path.getsize(pending_path) \
                >= _MAX_PENDING_BYTES:
            print(
                "Warning: pending sidecar full, "
                "observation skipped",
                file=sys.stderr,
            )
            return
    except OSError:
        pass
    try:
        with open(pending_path, 'a', encoding="utf-8") as f:
            f.write(entry + '\n')
            f.flush()
    except OSError:
        if not _write_direct(graph_path, entry):
            print(
                "Warning: observation lost — both sidecar "
                "and direct write failed",
                file=sys.stderr,
            )


def _write_direct(graph_path, entry):
    """Fallback: locked append to graph.jsonl."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    lock_path = os.path.join(
        os.path.dirname(graph_path), '.graph.lock'
    )
    acquired = False
    lock_fd = None
    if fcntl is not None:
        try:
            lock_fd = open(lock_path, 'a')
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
                lock_fd = None
                return False
        except OSError:
            if lock_fd is not None:
                lock_fd.close()
            return False
    try:
        with open(
            graph_path, 'a', encoding="utf-8"
        ) as f:
            f.write(entry + '\n')
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError:
        return False
    finally:
        if acquired and fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except OSError:
                pass


if __name__ == '__main__':
    main()

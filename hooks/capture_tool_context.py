#!/usr/bin/env python3
"""PostToolUse hook: capture observations from tool calls.

Standalone .py for bytecode caching (.pyc) — avoids re-parsing
the shell heredoc on every invocation.

Usage: python3 capture_tool_context.py <input_json> <graph_path>
"""
import json
import os
import sys
import time

def main():
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

    # Build a terse observation
    if tool == 'Edit':
        path = data.get('tool_input', {}).get('file_path', '?')
        obs = f'Edited {os.path.basename(path)}'
    elif tool == 'Write':
        path = data.get('tool_input', {}).get('file_path', '?')
        obs = f'Created/wrote {os.path.basename(path)}'
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

    try:
        import fcntl
        lock_path = os.path.join(
            os.path.dirname(graph_path), '.graph.lock'
        )
        lock_fd = open(lock_path, 'a')
        try:
            # Retry with short timeout — 5×50ms covers
            # typical graph rewrites (~100–500ms)
            acquired = False
            for _ in range(5):
                try:
                    fcntl.flock(
                        lock_fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    acquired = True
                    break
                except (IOError, OSError):
                    time.sleep(0.050)
            # Skip write if lock not acquired
            # to prevent JSONL corruption under contention
            if not acquired:
                sys.stderr.write(
                    'capture_tool_context: lock miss '
                    'after 250ms — skipping write\n'
                )
                sys.exit(2)
            with open(graph_path, 'a', encoding="utf-8") as f:
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
        # Windows: no fcntl — append without lock
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

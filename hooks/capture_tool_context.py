#!/usr/bin/env python3
"""PostToolUse hook: surface file warnings from the knowledge graph.

Usage: python3 capture_tool_context.py <input_json> <graph_path>
"""
import json
import os
import sys
import time

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
    if tool not in ('Edit', 'Write'):
        sys.exit(0)

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


if __name__ == '__main__':
    main()

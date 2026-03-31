#!/usr/bin/env python3
"""CLI bridge for memory tools — unified gateway.

Usage: python3 memory-cli.py [--memory-dir DIR] <tool> [json_args]

Gateway for both knowledge graph (.memory/) and Claude Code's
native auto-memory (~/.claude/projects/.../memory/).
"""
import json
import os
import re
import sys

try:
    import fcntl
    _HAS_FLOCK = True
except ImportError:
    _HAS_FLOCK = False

# Add script's own directory to path
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)


def _resolve_memory_dir(argv):
    """Extract memory_dir from --memory-dir flag, env, or cwd."""
    md = None
    cleaned = []
    i = 0
    while i < len(argv):
        if argv[i] == "--memory-dir" and i + 1 < len(argv):
            md = argv[i + 1]
            i += 2
        elif argv[i].startswith("--memory-dir="):
            md = argv[i].split("=", 1)[1]
            i += 1
        else:
            cleaned.append(argv[i])
            i += 1
    if md is None:
        md = os.environ.get("MEMORY_DIR")
    if md is None:
        md = os.path.join(os.getcwd(), ".memory")
    return md, cleaned


def _resolve_native_memory_dir(memory_dir=None):
    """Derive Claude Code's native auto-memory directory from project path.

    Path format: ~/.claude/projects/-<path-with-slashes-as-dashes>/memory/
    Example: /Users/foo/projects/bar -> ~/.claude/projects/-Users-foo-projects-bar/memory/

    SYNC NOTE: This derivation is duplicated in hooks/smart_recall.py
    (_count_native_memories). Keep both in sync if the path scheme changes.
    """
    if memory_dir:
        project_dir = os.path.dirname(os.path.abspath(memory_dir))
    else:
        project_dir = os.getcwd()
    key = project_dir.lstrip("/").replace("/", "-")
    return os.path.join(
        os.path.expanduser("~"), ".claude", "projects",
        f"-{key}", "memory",
    )


def _parse_frontmatter(content):
    """Parse YAML frontmatter from markdown content."""
    fm = {}
    body = content
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if m:
        body = content[m.end():]
        for line in m.group(1).splitlines():
            k, _, v = line.partition(':')
            k, v = k.strip(), v.strip()
            if k and v:
                fm[k] = v
    return fm, body


def _search_native_memories(query, native_dir):
    """Keyword search across native auto-memory .md files."""
    if not os.path.isdir(native_dir):
        return []
    results = []
    query_terms = set(query.lower().split())
    if not query_terms:
        return []
    for fname in os.listdir(native_dir):
        if not fname.endswith('.md') or fname == 'MEMORY.md':
            continue
        fpath = os.path.join(native_dir, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                content = f.read()
        except OSError:
            continue
        fm, body = _parse_frontmatter(content)
        text = ' '.join([
            fm.get('name', ''), fm.get('description', ''), body,
        ]).lower()
        hits = sum(1 for t in query_terms if t in text)
        if hits > 0:
            results.append({
                "entity": fm.get('name', os.path.splitext(fname)[0]),
                "entityType": f"native:{fm.get('type', 'unknown')}",
                "score": round(hits / len(query_terms), 2),
                "observations": (
                    [fm.get('description', '')]
                    if fm.get('description') else []
                ),
                "source": "native",
                "file": fname,
            })
    results.sort(key=lambda r: r['score'], reverse=True)
    return results


def _get_native_memory_stats(native_dir):
    """Count native auto-memory files by type."""
    if not os.path.isdir(native_dir):
        return None
    counts = {}
    total = 0
    for fname in os.listdir(native_dir):
        if not fname.endswith('.md') or fname == 'MEMORY.md':
            continue
        fpath = os.path.join(native_dir, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                content = f.read(500)
        except OSError:
            continue
        fm, _ = _parse_frontmatter(content)
        mem_type = fm.get('type', 'unknown')
        counts[mem_type] = counts.get(mem_type, 0) + 1
        total += 1
    return {"total": total, "by_type": counts} if total else None


def _update_memory_index(native_dir, filename, name, description):
    """Add or update entry in MEMORY.md index."""
    index_path = os.path.join(native_dir, "MEMORY.md")
    lines = []
    if os.path.isfile(index_path):
        with open(index_path, encoding='utf-8') as f:
            lines = f.readlines()
    entry_line = f"- [{name}]({filename}) — {description}\n"
    found = False
    for i, line in enumerate(lines):
        if f"]({filename})" in line:
            lines[i] = entry_line
            found = True
            break
    if not found:
        if not lines:
            lines = ["# Memory Index\n", "\n"]
        lines.append(entry_line)
    tmp = index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, index_path)


def _rebuild_memory_index(native_dir):
    """Rebuild MEMORY.md from all .md files in native dir."""
    index_path = os.path.join(native_dir, "MEMORY.md")
    lines = ["# Memory Index\n", "\n"]
    for fname in sorted(os.listdir(native_dir)):
        if not fname.endswith('.md') or fname == 'MEMORY.md':
            continue
        fpath = os.path.join(native_dir, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                content = f.read(500)
        except OSError:
            continue
        fm, _ = _parse_frontmatter(content)
        name = fm.get('name', os.path.splitext(fname)[0])
        desc = fm.get('description', '')
        lines.append(f"- [{name}]({fname}) — {desc}\n")
    tmp = index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, index_path)


def _flock_native(native_dir):
    """Acquire exclusive lock on native memory directory.

    Returns (lock_fd, lock_path) or (None, None) if flock unavailable.
    Caller must call _funlock_native(lock_fd) when done.
    """
    if not _HAS_FLOCK:
        return None, None
    lock_path = os.path.join(native_dir, ".memory.lock")
    lock_fd = open(lock_path, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd, lock_path


def _funlock_native(lock_fd):
    """Release native memory directory lock."""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except OSError:
            pass


def _usage():
    print(
        "Usage: mem [--memory-dir DIR] "
        "<command> [args]\n"
        "\nUnified Commands:\n"
        "  search <query>          "
        "Search both graph + native memory\n"
        "  recall <query>          "
        "Search + 1-hop graph neighbors\n"
        "  write  '<json>'         "
        "Create graph entities/relations/obs\n"
        "  decide '<json>'         "
        "Create or resolve a decision\n"
        "  remember '<text>'       "
        "Write to native auto-memory\n"
        "  forget  '<name>'        "
        "Remove from native auto-memory\n"
        "  sync                    "
        "Sync native memory into graph\n"
        "  remove '<json>'         "
        "Delete graph entities/observations\n"
        "  status                  "
        "Health of both memory systems\n"
        "  doctor                  "
        "Diagnostics across both stores\n"
        "  rebuild                 "
        "Rebuild TF-IDF index\n"
        "  viz [entity]            "
        "Graph visualization (DOT format)\n"
        "  timeline [--global]     "
        "Recent activity across projects\n"
        "\nLegacy tool names also work for "
        "backward compatibility.",
        file=sys.stderr,
    )
    sys.exit(1)


def _parse_positional(args):
    """Parse positional args for unified tools.

    Supports flags before or after positional values:
      search auth service     -> {"query": "auth service"}
      search auth --compact   -> {"query": "auth", "compact": true}
      remember --type feedback "text" -> {"entity_type": "feedback", "query": "text"}
      status                  -> {}
    Falls back to JSON parsing for complex args.
    """
    if not args:
        return {}
    first = args[0]
    if first.startswith('{') or first.startswith('['):
        try:
            parsed = json.loads(first)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    result = {}
    positionals = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--compact":
            result["compact"] = True
        elif arg == "--top-k" and i + 1 < len(args):
            i += 1
            try:
                result["top_k"] = int(args[i])
            except ValueError:
                pass
        elif arg == "--mode" and i + 1 < len(args):
            i += 1
            result["mode"] = args[i]
        elif arg == "--since" and i + 1 < len(args):
            i += 1
            result["since"] = args[i]
        elif arg == "--type" and i + 1 < len(args):
            i += 1
            result["entity_type"] = args[i]
        elif arg == "--name" and i + 1 < len(args):
            i += 1
            result["name"] = args[i]
        elif arg == "--global":
            result["global"] = True
        elif not arg.startswith("--"):
            positionals.append(arg)
        i += 1
    if positionals:
        result["query"] = " ".join(positionals)
    return result


def _do_merge(merging, graph):
    """Read merging file, append to graph, delete merging.

    If append succeeds but unlink fails, truncate the
    file to prevent re-appending the same data.
    """
    try:
        if (os.path.exists(merging)
                and os.path.getsize(merging) > 50 * 1024 * 1024):
            sys.stderr.write(
                f"Error: Pending merge file {merging} "
                f"exceeds 50MB limit. Skipping.\n"
            )
            try:
                os.unlink(merging)
            except OSError:
                pass
            return
    except OSError:
        pass

    try:
        with open(merging, "rb") as src:
            data = src.read()
        if not data:
            os.unlink(merging)
            return
        if not data.endswith(b"\n"):
            data += b"\n"
        with open(graph, "ab") as dst:
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
        try:
            os.unlink(merging)
        except OSError:
            try:
                with open(merging, "w"):
                    pass
            except OSError:
                pass
    except OSError:
        pass


def _merge_pending(memory_dir):
    """Merge .pending sidecar into graph.jsonl.

    Atomic rename prevents TOCTOU with concurrent hook
    writers — new writes go to a fresh .pending file.
    Recovers orphaned .merging/.processing from crash.
    """
    pending = os.path.join(
        memory_dir, "graph.jsonl.pending"
    )
    merging = pending + ".merging"
    graph = os.path.join(memory_dir, "graph.jsonl")

    if os.path.exists(merging):
        _do_merge(merging, graph)

    try:
        os.rename(pending, merging)
    except OSError:
        return
    _do_merge(merging, graph)


def _load_graph_doctor(graph_path, issues):
    entities = {}
    relations = []
    try:
        with open(graph_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    issues.append("Corrupt JSONL line")
                    continue
                if "name" in row:
                    entities[row["name"]] = row
                elif "from" in row and "to" in row:
                    relations.append(row)
    except OSError as exc:
        issues.append(f"Cannot read graph: {exc}")
    return entities, relations


def _check_stale_decisions(entities, now, issues):
    from datetime import datetime
    for name, ent in entities.items():
        if ent.get("entityType") == "decision":
            for obs in ent.get("observations", []):
                obs_s = obs if isinstance(obs, str) else ""
                if "pending" in obs_s.lower():
                    updated = ent.get("_updated", "")
                    if updated:
                        try:
                            dt = datetime.fromisoformat(
                                updated.replace("Z", "+00:00")
                            )
                            age_days = (now - dt.timestamp()) / 86400
                            if age_days > 30:
                                issues.append(
                                    f"Stale decision: {name} "
                                    f"({int(age_days)}d pending)"
                                )
                        except (ValueError, OSError):
                            pass
                    break


def _check_orphan_relations(entities, relations, issues):
    entity_names = set(entities.keys())
    orphan_count = sum(
        1 for r in relations
        if r.get("from") not in entity_names
        or r.get("to") not in entity_names
    )
    if orphan_count:
        issues.append(
            f"Orphan relations: {orphan_count} "
            f"reference non-existent entities"
        )


def _check_oversized_entities(entities, issues):
    oversized = []
    for name, ent in entities.items():
        n_obs = len(ent.get("observations", []))
        if n_obs > 100:
            oversized.append(f"{name} ({n_obs} obs)")
    if oversized:
        issues.append(
            f"Oversized entities: "
            f"{', '.join(oversized[:5])}"
        )


def _check_native_memory_health(memory_dir, issues,
                                graph_entities=None):
    """Check native auto-memory health.

    If graph_entities is provided, uses it instead of
    re-reading graph.jsonl (avoids double-read in doctor).
    """
    native_dir = _resolve_native_memory_dir(memory_dir)
    if not os.path.isdir(native_dir):
        return

    index_path = os.path.join(native_dir, "MEMORY.md")
    index_files = set()
    if os.path.isfile(index_path):
        with open(index_path, encoding='utf-8') as f:
            for line in f:
                m = re.search(r'\]\(([^)]+\.md)\)', line)
                if m:
                    index_files.add(m.group(1))

    actual_files = set()
    native_names = set()
    for fname in os.listdir(native_dir):
        if fname.endswith('.md') and fname != 'MEMORY.md':
            actual_files.add(fname)
            fpath = os.path.join(native_dir, fname)
            try:
                with open(fpath, encoding='utf-8') as f:
                    content = f.read(500)
                fm, _ = _parse_frontmatter(content)
                native_names.add(
                    fm.get('name', os.path.splitext(fname)[0])
                )
            except OSError:
                continue

    orphaned = actual_files - index_files
    if orphaned:
        issues.append(
            f"Native: {len(orphaned)} files not in "
            f"MEMORY.md index"
        )
    dangling = index_files - actual_files
    if dangling:
        issues.append(
            f"Native: {len(dangling)} MEMORY.md entries "
            f"point to missing files"
        )

    # Check for stale graph mirrors using provided entities
    # or falling back to graph file scan
    stale = 0
    if graph_entities is not None:
        for name, ent in graph_entities.items():
            if ent.get('_migrated_from') == 'auto-memory':
                if name.startswith('auto-memory: '):
                    orig = name[len('auto-memory: '):]
                    if orig not in native_names:
                        stale += 1
    else:
        graph_path = os.path.join(memory_dir, "graph.jsonl")
        if os.path.isfile(graph_path):
            with open(graph_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get('_migrated_from') == 'auto-memory':
                            name = rec.get('name', '')
                            if name.startswith('auto-memory: '):
                                orig = name[len('auto-memory: '):]
                                if orig not in native_names:
                                    stale += 1
                    except (json.JSONDecodeError, KeyError):
                        pass
    if stale:
        issues.append(
            f"Graph: {stale} migrated entities whose "
            f"native source was removed"
        )


def _run_doctor(memory_dir):
    """Health check across both memory stores."""
    import time
    from collections import Counter
    issues = []
    graph_path = os.path.join(memory_dir, "graph.jsonl")

    if os.path.exists(graph_path):
        entities, relations = _load_graph_doctor(
            graph_path, issues
        )
        now = time.time()
        _check_stale_decisions(entities, now, issues)
        _check_orphan_relations(entities, relations, issues)
        _check_oversized_entities(entities, issues)

        idx_path = os.path.join(memory_dir, "tfidf_index.json")
        if os.path.exists(idx_path):
            idx_age_h = (
                (now - os.path.getmtime(idx_path)) / 3600
            )
            if idx_age_h > 24:
                issues.append(
                    f"Stale index: {idx_age_h:.0f}h old "
                    f"(run: mem rebuild)"
                )
        elif entities:
            issues.append(
                "No TF-IDF index found (run: mem rebuild)"
            )

        type_counts = Counter(
            ent.get("entityType", "unknown")
            for ent in entities.values()
        )
    else:
        entities = {}
        relations = []
        type_counts = Counter()
        issues.append("No graph.jsonl found")

    # Native memory health — pass loaded entities to avoid
    # re-reading graph.jsonl
    _check_native_memory_health(
        memory_dir, issues, graph_entities=entities
    )

    native_dir = _resolve_native_memory_dir(memory_dir)
    native_stats = _get_native_memory_stats(native_dir)

    status = "healthy" if not issues else "issues_found"
    result = {
        "status": status,
        "graph": {
            "entities": len(entities),
            "relations": len(relations),
            "type_distribution": dict(
                type_counts.most_common(10)
            ),
        },
        "issues": issues,
        "issue_count": len(issues),
    }
    if native_stats:
        result["native_memory"] = native_stats

    if sys.stdout.isatty():
        print(f"\n\033[1mMemory Doctor\033[0m — {status}")
        g = result["graph"]
        print(
            f"  Graph: {g['entities']}e "
            f"{g['relations']}r"
        )
        if native_stats:
            ns = native_stats
            type_str = ", ".join(
                f"{c} {t}" for t, c in ns["by_type"].items()
            )
            print(f"  Native: {ns['total']} ({type_str})")
        if issues:
            print(f"\n  Issues ({len(issues)}):")
            for issue in issues:
                print(f"    \033[33m!\033[0m {issue}")
        else:
            print("  \033[32mNo issues found\033[0m")
        print()
    else:
        print(json.dumps(result, indent=2))


_WRITE_TOOLS = frozenset({
    "create_entities", "create_relations",
    "add_observations", "remove_observations",
    "delete_entities", "rename_entity",
    "create_decision", "update_decision_outcome",
})


# --- Native Memory Handlers ---

def _mem_remember(a, memory_dir):
    """Write to native auto-memory system.

    Uses flock to prevent TOCTOU races between concurrent
    remember calls with the same name.
    """
    body = a.get("body", a.get("query", ""))
    mem_type = a.get("type", a.get("entity_type", "project"))
    name = a.get("name", "")
    description = a.get("description", "")

    if not body and not description:
        return {
            "error": "body or description required — "
            "usage: mem remember 'text'"
        }

    if not name:
        words = re.sub(
            r'[^a-zA-Z0-9\s]', '', body or description
        ).split()[:5]
        name = " ".join(words) if words else "unnamed"

    if not description:
        description = (body[:120] if body else name)

    native_dir = _resolve_native_memory_dir(memory_dir)
    os.makedirs(native_dir, exist_ok=True)

    slug = re.sub(
        r'[^a-z0-9_-]', '_', name.lower().strip()
    )[:60].strip('_')
    filename = f"{slug}.md"
    filepath = os.path.join(native_dir, filename)

    lock_fd, _ = _flock_native(native_dir)
    try:
        # Check for existing file with same name — update it
        existing_file = None
        for fname in os.listdir(native_dir):
            if not fname.endswith('.md') or fname == 'MEMORY.md':
                continue
            fpath = os.path.join(native_dir, fname)
            try:
                with open(fpath, encoding='utf-8') as f:
                    content = f.read(500)
                fm, _ = _parse_frontmatter(content)
                if fm.get('name', '').lower() == name.lower():
                    existing_file = fpath
                    filename = fname
                    filepath = fpath
                    break
            except OSError:
                continue

        content = (
            f"---\nname: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n---\n\n{body}\n"
        )
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, filepath)

        _update_memory_index(
            native_dir, filename, name, description
        )
    finally:
        _funlock_native(lock_fd)

    action = "updated" if existing_file else "created"
    return {
        "status": "ok", "action": action,
        "file": filepath, "type": mem_type, "name": name,
    }


def _mem_forget(a, memory_dir):
    """Remove from native auto-memory system.

    Uses rename-to-trash then rebuild-index then unlink
    to prevent crash leaving dangling MEMORY.md entries.
    """
    name = a.get("name", a.get("query", ""))
    if not name:
        return {
            "error": "name required — "
            "usage: mem forget 'name'"
        }

    native_dir = _resolve_native_memory_dir(memory_dir)
    if not os.path.isdir(native_dir):
        return {"error": "no native memory directory found"}

    lock_fd, _ = _flock_native(native_dir)
    try:
        # Reclaim orphaned .trash files from prior crashed runs
        for fname in os.listdir(native_dir):
            if fname.endswith('.md.trash'):
                try:
                    os.unlink(os.path.join(native_dir, fname))
                except OSError:
                    pass

        removed = []
        trash_files = []
        for fname in os.listdir(native_dir):
            if not fname.endswith('.md') or fname == 'MEMORY.md':
                continue
            fpath = os.path.join(native_dir, fname)
            try:
                with open(fpath, encoding='utf-8') as f:
                    content = f.read(500)
            except OSError:
                continue
            fm, _ = _parse_frontmatter(content)
            if (fm.get('name', '').lower() == name.lower()
                    or os.path.splitext(fname)[0].lower()
                    == name.lower()):
                # Rename to .trash first (crash-safe)
                trash = fpath + ".trash"
                os.rename(fpath, trash)
                removed.append(fname)
                trash_files.append(trash)

        if removed:
            # Rebuild index while files are gone
            _rebuild_memory_index(native_dir)
            # Now safe to delete trash
            for tf in trash_files:
                try:
                    os.unlink(tf)
                except OSError:
                    pass
            return {"status": "ok", "removed": removed}
    finally:
        _funlock_native(lock_fd)

    return {"error": f"no memory found matching '{name}'"}


def _mem_sync(a, memory_dir):
    """Sync native auto-memory entries into the knowledge graph.

    Writes through the .pending sidecar to avoid contention
    with concurrent hook writers.
    """
    import time

    native_dir = _resolve_native_memory_dir(memory_dir)
    if not os.path.isdir(native_dir):
        return {
            "status": "ok", "migrated": 0,
            "message": "No native memory directory",
        }

    graph_path = os.path.join(memory_dir, "graph.jsonl")
    pending_path = os.path.join(
        memory_dir, "graph.jsonl.pending"
    )
    existing = set()
    # Read both graph and pending to check for existing entities
    for fpath in (graph_path, pending_path):
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get('type') == 'entity':
                            existing.add(rec.get('name', ''))
                    except (json.JSONDecodeError, KeyError):
                        pass
        except OSError:
            pass

    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    migrated = 0

    for fname in sorted(os.listdir(native_dir)):
        if not fname.endswith('.md') or fname == 'MEMORY.md':
            continue
        fpath = os.path.join(native_dir, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                content = f.read()
        except OSError:
            continue

        fm, body = _parse_frontmatter(content)
        name = fm.get('name', os.path.splitext(fname)[0])
        entity_name = f'auto-memory: {name}'

        if entity_name in existing:
            continue

        desc = fm.get('description', '')
        observations = []
        if desc:
            observations.append(desc)
        for bline in body.strip().splitlines():
            bline = bline.strip()
            if bline and len(bline) > 3:
                observations.append(bline[:200])

        if not observations:
            continue

        entity = {
            'type': 'entity',
            'name': entity_name,
            'entityType': fm.get('type', 'reference'),
            'observations': observations,
            '_created': now,
            '_updated': now,
            '_migrated_from': 'auto-memory',
        }
        # Write to sidecar, not graph directly
        with open(pending_path, 'a', encoding='utf-8') as f:
            f.write(
                json.dumps(entity, separators=(',', ':'))
                + '\n'
            )
            f.flush()
            os.fsync(f.fileno())
        existing.add(entity_name)
        migrated += 1

    # Merge sidecar into graph
    if migrated:
        _merge_pending(memory_dir)

    return {"status": "ok", "migrated": migrated}


# --- Unified Tool Handlers ---

def _unified_search(a, memory_dir):
    from semantic_server.search import search, search_by_time
    from semantic_server.traverse import traverse_relations

    mode = a.get("mode", "semantic")
    if mode == "temporal":
        return search_by_time(
            memory_dir, a.get("since"), a.get("until"),
            a.get("limit", 20),
            branch_filter=a.get("branch_filter"),
            entity_type=a.get("entity_type"),
        )
    elif mode == "graph":
        return traverse_relations(
            a.get("entity", a.get("query", "")), memory_dir,
            a.get("direction", "both"), a.get("max_depth", 2),
        )

    query = a.get("query", "")
    top_k = a.get("top_k", 5)

    # Graph search
    graph_result = search(
        query, memory_dir, top_k=top_k * 2,
        branch=a.get("branch"),
        compact=a.get("compact", False),
    )

    # Native memory search
    native_dir = _resolve_native_memory_dir(memory_dir)
    native_results = _search_native_memories(query, native_dir)

    # Merge: graph first, then enrich or append native
    merged = list(graph_result.get("results", []))
    graph_names = {r.get("entity", "").lower() for r in merged}
    for nr in native_results:
        nr_name = nr["entity"].lower()
        if nr_name not in graph_names:
            merged.append(nr)
        else:
            # Merge supplementary observations from native
            # into the matching graph result
            for gr in merged:
                if gr.get("entity", "").lower() == nr_name:
                    existing_obs = set(
                        gr.get("observations", [])
                    )
                    for obs in nr.get("observations", []):
                        if obs and obs not in existing_obs:
                            gr.setdefault(
                                "observations", []
                            ).append(obs)
                    break

    return {
        "results": merged[:top_k],
        "total_indexed": graph_result.get("total_indexed", 0),
        "native_matches": len(native_results),
        "sources": {
            "graph": len(graph_result.get("results", [])),
            "native": len(native_results),
        },
    }


def _auto_create_relation_entities(rels, ents, memory_dir,
                                   results):
    existing = (
        {e.get("name", "") for e in ents} if ents else set()
    )
    from semantic_server.graph import load_graph_entities
    existing.update(load_graph_entities(memory_dir).keys())
    auto_ents = []
    for r in rels:
        for key in ("from", "to"):
            name = r.get(key, "")
            if name and name not in existing:
                auto_ents.append({
                    "name": name, "entityType": "unknown",
                    "observations": [
                        "Auto-created from relation reference"
                    ],
                })
                existing.add(name)
    if auto_ents:
        results["auto_created"] = [
            e["name"] for e in auto_ents
        ]
        return ents + auto_ents
    return ents


def _handle_obs_map(obs_map, memory_dir, results):
    from semantic_server.tools import add_observations
    obs_results = {}
    for entity, obs_list in obs_map.items():
        if isinstance(obs_list, list):
            obs_results[entity] = add_observations(
                entity, obs_list, memory_dir
            )
    results["observations"] = obs_results


def _unified_write(a, memory_dir):
    from semantic_server.tools import (
        create_entities, create_relations, add_observations,
    )
    results = {}
    ents = a.get("entities", [])
    rels = a.get("relations", [])
    obs_map = a.get("observations", {})

    if rels:
        ents = _auto_create_relation_entities(
            rels, ents, memory_dir, results
        )

    if ents:
        results["entities"] = create_entities(ents, memory_dir)
    if rels:
        results["relations"] = create_relations(
            rels, memory_dir
        )
    if obs_map and isinstance(obs_map, dict):
        _handle_obs_map(obs_map, memory_dir, results)

    if not ents and not rels and not obs_map:
        entity_name = a.get("entity", "")
        observation = a.get("observation", "")
        if entity_name and observation:
            results = add_observations(
                entity_name, [observation], memory_dir
            )
    return results or {"error": "Nothing to write"}


def _unified_recall(a, memory_dir):
    query = a.get("query", "")
    if not query:
        return {"error": "query required"}
    from semantic_server.search import search
    from semantic_server.traverse import traverse_relations

    sr = search(
        query, memory_dir, top_k=a.get("top_k", 3),
        branch=a.get("branch"), compact=True,
    )
    results = sr.get("results", [])
    if not results:
        return sr
    enriched = []
    for r in results[:3]:
        entity = r.get("entity", "")
        tr = traverse_relations(entity, memory_dir, "both", 1)
        connected = [
            {
                "name": n.get("name", ""),
                "type": n.get("entityType", ""),
                "relation": n.get("_relation", ""),
            }
            for n in tr.get("nodes", [])
            if n.get("name") != entity
        ][:5]
        enriched.append({
            "entity": entity,
            "score": r.get("score", 0),
            "entityType": r.get("entityType", ""),
            "connected": connected,
        })
    return {
        "results": enriched,
        "total_indexed": sr.get("total_indexed", 0),
    }


def _unified_decide(a, memory_dir):
    from semantic_server.tools import (
        create_decision, update_decision_outcome,
    )
    if a.get("action", "create") == "resolve":
        return update_decision_outcome(a, memory_dir)
    return create_decision(a, memory_dir)


def _unified_remove(a, memory_dir):
    from semantic_server.tools import (
        rename_entity, remove_observations, delete_entities,
    )
    action = a.get("action", "")
    if action == "rename":
        return rename_entity(
            a.get("old_name", ""), a.get("new_name", ""),
            memory_dir,
        )
    if action == "remove_observations":
        return remove_observations(
            a.get("entity", ""), a.get("observations", []),
            memory_dir,
        )
    names = (
        a.get("entity_names", [])
        or ([a.get("entity")] if a.get("entity") else [])
    )
    return delete_entities(names, memory_dir)


def _unified_status(a, memory_dir):
    from semantic_server.tools import graph_stats, list_decisions
    stats = graph_stats(memory_dir)
    pending = list_decisions(
        memory_dir, stale_days=2
    ).get("decisions", [])
    if pending:
        stats["decision_nudge"] = {
            "pending_count": len(pending),
            "message": (
                f"{len(pending)} decisions pending > 2 days"
            ),
            "oldest": [
                d.get("title", "") for d in pending[:5]
            ],
        }

    # Native memory stats
    native_dir = _resolve_native_memory_dir(memory_dir)
    native_stats = _get_native_memory_stats(native_dir)
    if native_stats:
        stats["native_memory"] = native_stats
    return stats


def _run_viz(memory_dir, entity_name=None):
    """Output DOT graph for visualization."""
    from semantic_server.graph import (
        load_graph_entities, load_graph_relations,
    )
    entities = load_graph_entities(memory_dir)
    relations = load_graph_relations(memory_dir)
    if not entities:
        print("digraph memory { label=\"Empty graph\"; }")
        return

    if entity_name and entity_name not in entities:
        print(
            f"digraph memory {{ label=\"Entity "
            f"'{entity_name}' not found\"; }}",
        )
        return
    if entity_name and entity_name in entities:
        relevant = {entity_name}
        for r in relations:
            if r.get("from") == entity_name:
                relevant.add(r["to"])
            elif r.get("to") == entity_name:
                relevant.add(r["from"])
        hop2 = set()
        for r in relations:
            if r.get("from") in relevant:
                hop2.add(r["to"])
            elif r.get("to") in relevant:
                hop2.add(r["from"])
        relevant |= hop2
    else:
        relevant = set(entities.keys())

    _TYPE_COLORS = {
        "component": "#4A90D9", "service": "#7B68EE",
        "decision": "#FFD700", "file-warning": "#FF6347",
        "module": "#3CB371", "function": "#20B2AA",
        "bug": "#FF4500", "activity-log": "#808080",
    }

    lines = [
        'digraph memory {', '  rankdir=LR;',
        '  node [shape=box, style="rounded,filled",'
        ' fontname="Helvetica"];',
        '  edge [fontname="Helvetica", fontsize=10];',
    ]
    for name in relevant:
        if name not in entities:
            continue
        info = entities[name]
        etype = info.get("entityType", "")
        color = _TYPE_COLORS.get(etype, "#D3D3D3")
        label = name.replace('"', '\\"')
        if etype:
            label += f"\\n({etype})"
        lines.append(
            f'  "{name}" [label="{label}",'
            f' fillcolor="{color}"];'
        )
    for r in relations:
        fr, to = r.get("from", ""), r.get("to", "")
        if fr in relevant and to in relevant:
            rt = r.get("relationType", "")
            rt_label = (
                f' [label="{rt}"]' if rt else ""
            )
            lines.append(f'  "{fr}" -> "{to}"{rt_label};')
    lines.append("}")
    print("\n".join(lines))


def _run_timeline(memory_dir, global_mode=False):
    """Show recent activity across one or all projects."""
    import glob as _glob
    from datetime import datetime, timezone

    dirs = []
    if global_mode:
        home = os.path.expanduser("~")
        for pattern in [
            os.path.join(home, "*", ".memory"),
            os.path.join(home, "*", "*", ".memory"),
            os.path.join(home, "projects", "*", ".memory"),
            os.path.join(home, "code", "*", ".memory"),
        ]:
            dirs.extend(_glob.glob(pattern))
        seen = set()
        unique = []
        for d in dirs:
            rp = os.path.realpath(d)
            if rp not in seen:
                seen.add(rp)
                unique.append(d)
        dirs = unique
    else:
        dirs = [memory_dir]

    entries = []
    for md in dirs:
        graph_path = os.path.join(md, "graph.jsonl")
        project = os.path.basename(os.path.dirname(md))
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
                        if obj.get("type") != "entity":
                            continue
                        updated = obj.get("_updated", "")
                        if not updated:
                            continue
                        entries.append({
                            "project": project,
                            "name": obj.get("name", ""),
                            "type": obj.get("entityType", ""),
                            "updated": updated,
                        })
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            continue

    entries.sort(key=lambda e: e["updated"], reverse=True)

    if sys.stdout.isatty():
        print(
            f"\nTimeline ({len(entries)} entities"
            + (
                " across projects" if global_mode else ""
            )
            + "):\n"
        )
        for e in entries[:30]:
            proj = (
                f"[{e['project']}] " if global_mode else ""
            )
            etype = (
                f"({e['type']})" if e['type'] else ""
            )
            print(
                f"  {e['updated'][:16]}  "
                f"{proj}{e['name']} {etype}"
            )
        if len(entries) > 30:
            print(f"\n  ... +{len(entries) - 30} more")
        print()
    else:
        print(json.dumps({"entries": entries[:50]}, indent=2))


def _format_tty_output(tool_name, result):
    """Format result for human-readable TTY output."""
    if tool_name in ("search", "recall",
                     "memory_search", "memory_recall"):
        res_list = result.get("results", [])
        sources = result.get("sources", {})
        source_info = ""
        if sources:
            source_info = (
                f" (graph:{sources.get('graph', 0)}"
                f" native:{sources.get('native', 0)})"
            )
        print(
            f"Search{source_info} "
            f"({result.get('total_indexed', 0)} indexed):"
        )
        for r in res_list:
            name = r.get("entity", "")
            etype = r.get("entityType", "")
            score = r.get("score", 0.0)
            tag = (
                " \033[35m[native]\033[0m"
                if r.get("source") == "native" else ""
            )
            print(
                f"\n- \033[1;36m{name}\033[0m "
                f"({etype}) [score: {score:.2f}]{tag}"
            )
            if "observations" in r:
                for obs in r["observations"]:
                    print(f"    \u2022 {obs}")
            if "connected" in r:
                conns = [
                    f"{c.get('relation', '--')}->"
                    f"{c.get('name', '')}"
                    for c in r["connected"]
                ]
                if conns:
                    print(
                        f"    \u21b3 {', '.join(conns)}"
                    )
    elif tool_name in ("status", "memory_status",
                       "graph_stats"):
        print("\n\033[1mMemory Diagnostics\033[0m")
        for k, v in result.items():
            if k == "native_memory" and isinstance(v, dict):
                total = v.get("total", 0)
                by_type = v.get("by_type", {})
                type_str = ", ".join(
                    f"{c} {t}" for t, c in by_type.items()
                )
                print(
                    f"\n  \033[1mNative Memory:\033[0m"
                    f" {total} ({type_str})"
                )
            elif (isinstance(v, dict)
                    and k == "decision_nudge"):
                print(
                    f"\n  \033[1;33m\u26a0\ufe0f  "
                    f"{v.get('message', '')}\033[0m"
                )
                for old_d in v.get("oldest", []):
                    print(f"      - {old_d}")
            elif isinstance(v, dict):
                print(f"\n  {k}:")
                for subk, subv in v.items():
                    print(f"    {subk}: {subv}")
            else:
                print(f"  {k}: {v}")
        print("")
    else:
        print(json.dumps(result, indent=2))


def _parse_tool_args(tool_name, extra_args):
    """Parse CLI arguments into a tool_args dict."""
    _POSITIONAL_TOOLS = {
        "search", "recall", "status",
        "doctor", "rebuild_index",
        "viz", "timeline",
        "remember", "forget", "sync",
    }
    if tool_name in _POSITIONAL_TOOLS:
        return _parse_positional(extra_args)
    if extra_args:
        first = extra_args[0]
        if first.startswith('{') or first.startswith('['):
            try:
                tool_args = json.loads(first)
            except (json.JSONDecodeError, ValueError) as e:
                print(
                    f"Error: invalid JSON: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not isinstance(tool_args, dict):
                print(
                    "Error: args must be a JSON object",
                    file=sys.stderr,
                )
                sys.exit(1)
            return tool_args
        return _parse_positional(extra_args)
    return {}


def main():
    memory_dir, args = _resolve_memory_dir(sys.argv[1:])

    if not args:
        _usage()

    tool_name = args[0]
    extra_args = args[1:]

    # --- Unified tool aliases -> legacy names ---
    _ALIASES = {
        "rebuild": "rebuild_index",
    }
    tool_name = _ALIASES.get(tool_name, tool_name)

    tool_args = _parse_tool_args(tool_name, extra_args)

    # --- Auto-Init Logic ---
    if not os.path.isdir(memory_dir):
        try:
            os.makedirs(memory_dir, exist_ok=True)
            graph_path = os.path.join(
                memory_dir, "graph.jsonl"
            )
            if not os.path.exists(graph_path):
                with open(graph_path, "a"):
                    pass
            if sys.stdout.isatty():
                print(
                    f"Initialized knowledge graph at "
                    f"{memory_dir}",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(
                f"Error: Could not initialize "
                f"MEMORY_DIR {memory_dir}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Merge pending sidecar before any reads
    _merge_pending(memory_dir)

    # --- Commands that don't need semantic_server ---
    if tool_name in ("rebuild_index", "rebuild"):
        import maintenance
        indexed = maintenance.rebuild_index(memory_dir)
        print(json.dumps({
            "rebuilt": indexed > 0,
            "indexed": indexed,
        }))
        return

    if tool_name == "doctor":
        _run_doctor(memory_dir)
        return

    if tool_name == "remember":
        result = _mem_remember(tool_args, memory_dir)
        if sys.stdout.isatty() and not result.get("error"):
            action = result.get("action", "created")
            print(
                f"\033[32m{action}\033[0m "
                f"{result.get('name', '')} "
                f"({result.get('type', '')}) "
                f"-> {result.get('file', '')}"
            )
        else:
            print(json.dumps(result, indent=2))
        return

    if tool_name == "forget":
        result = _mem_forget(tool_args, memory_dir)
        if sys.stdout.isatty() and not result.get("error"):
            removed = result.get("removed", [])
            print(
                f"\033[31mremoved\033[0m "
                f"{', '.join(removed)}"
            )
        else:
            print(json.dumps(result, indent=2))
        return

    if tool_name == "sync":
        result = _mem_sync(tool_args, memory_dir)
        if sys.stdout.isatty():
            migrated = result.get("migrated", 0)
            print(f"Synced: {migrated} native -> graph")
        else:
            print(json.dumps(result, indent=2))
        if result.get("migrated", 0) > 0:
            try:
                import maintenance
                maintenance.rebuild_index(memory_dir)
            except Exception:
                pass
        return

    if tool_name == "viz":
        entity = extra_args[0] if extra_args else None
        _run_viz(memory_dir, entity)
        return

    if tool_name == "timeline":
        global_mode = "--global" in extra_args
        _run_timeline(memory_dir, global_mode)
        return

    # --- Commands that need semantic_server ---
    from semantic_server.search import (
        search, search_by_time,
    )
    from semantic_server.tools import (
        add_observations,
        create_decision,
        create_entities,
        create_relations,
        delete_entities,
        graph_stats,
        list_decisions,
        remove_observations,
        rename_entity,
        update_decision_outcome,
    )
    from semantic_server.traverse import (
        traverse_relations,
    )
    from semantic_server.graph import load_index
    from semantic_server.recall import (
        init_recall_state,
        flush_recall_counts,
    )

    try:
        load_index(memory_dir)
        init_recall_state(memory_dir)
    except Exception as exc:
        print(
            f"Warning: index init failed ({exc}), "
            f"search may be degraded",
            file=sys.stderr,
        )

    dispatch = {
        # --- Unified tools ---
        "search": lambda a: _unified_search(a, memory_dir),
        "write": lambda a: _unified_write(a, memory_dir),
        "recall": lambda a: _unified_recall(a, memory_dir),
        "decide": lambda a: _unified_decide(a, memory_dir),
        "remove": lambda a: _unified_remove(a, memory_dir),
        "status": lambda a: _unified_status(a, memory_dir),
        # --- Legacy tools (backward compat) ---
        "semantic_search_memory": lambda a: search(
            a.get("query", ""),
            memory_dir,
            a.get("top_k", 5),
            branch=a.get("branch"),
            compact=a.get("compact", False),
        ),
        "traverse_relations": lambda a: (
            traverse_relations(
                a.get("entity", ""),
                memory_dir,
                a.get("direction", "both"),
                a.get("max_depth", 2),
            )
        ),
        "search_memory_by_time": lambda a: (
            search_by_time(
                memory_dir,
                a.get("since"),
                a.get("until"),
                a.get("limit", 20),
                branch_filter=a.get("branch_filter"),
                entity_type=a.get("entity_type"),
            )
        ),
        "create_entities": lambda a: (
            create_entities(
                a.get("entities", []),
                memory_dir,
            )
        ),
        "create_relations": lambda a: (
            create_relations(
                a.get("relations", []),
                memory_dir,
            )
        ),
        "add_observations": lambda a: (
            add_observations(
                a.get("entity", ""),
                a.get("observations", []),
                memory_dir,
            )
        ),
        "remove_observations": lambda a: (
            remove_observations(
                a.get("entity", ""),
                a.get("observations", []),
                memory_dir,
            )
        ),
        "delete_entities": lambda a: (
            delete_entities(
                a.get("entity_names", []),
                memory_dir,
            )
        ),
        "rename_entity": lambda a: (
            rename_entity(
                a.get("old_name", ""),
                a.get("new_name", ""),
                memory_dir,
            )
        ),
        "create_decision": lambda a: (
            create_decision(a, memory_dir)
        ),
        "update_decision_outcome": lambda a: (
            update_decision_outcome(a, memory_dir)
        ),
        "list_decisions": lambda a: (
            list_decisions(
                memory_dir,
                stale_days=a.get("stale_days"),
            )
        ),
        "graph_stats": lambda a: (
            graph_stats(memory_dir)
        ),
        # Aliases
        "memory_search": lambda a: (
            _unified_search(a, memory_dir)
        ),
        "memory_write": lambda a: (
            _unified_write(a, memory_dir)
        ),
        "memory_recall": lambda a: (
            _unified_recall(a, memory_dir)
        ),
        "memory_decide": lambda a: (
            _unified_decide(a, memory_dir)
        ),
        "memory_remove": lambda a: (
            _unified_remove(a, memory_dir)
        ),
        "memory_status": lambda a: (
            _unified_status(a, memory_dir)
        ),
    }

    handler = dispatch.get(tool_name)
    if handler is None:
        print(
            f"Error: unknown tool '{tool_name}'",
            file=sys.stderr,
        )
        _usage()

    try:
        result = handler(tool_args)
    except Exception as exc:
        print(
            json.dumps({"error": str(exc)}, indent=2)
        )
        sys.exit(1)

    # TTY human-readable formatting or raw JSON
    if (sys.stdout.isatty() and isinstance(result, dict)
            and not result.get("error")):
        _format_tty_output(tool_name, result)
    else:
        print(json.dumps(result, indent=2))

    # Rebuild index after write ops
    _WRITE_OPS = _WRITE_TOOLS | {
        "write", "memory_write",
        "decide", "memory_decide",
        "remove", "memory_remove",
    }
    if tool_name in _WRITE_OPS:
        try:
            import maintenance
            maintenance.rebuild_index(memory_dir)
        except Exception as exc:
            print(
                f"Warning: index rebuild failed: "
                f"{exc}",
                file=sys.stderr,
            )

    # Flush recall counts (no-op if nothing changed)
    try:
        flush_recall_counts()
    except Exception:
        pass


if __name__ == "__main__":
    main()

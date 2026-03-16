#!/usr/bin/env python3
"""Regression tests for easy-memory-claude.

Covers: score_entity signature, imports, cache helpers,
CLI bridge, smart_recall, shell scripts, maintenance,
and E2E flows against a temp project.

Usage: python3 tests/test_regression.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

# Ensure project root is on path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_passed = 0
_failed = 0


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {name}")
    else:
        _failed += 1
        print(f"  FAIL: {name} — {detail}")


def _cli(mem_dir, tool, args_dict=None):
    """Run memory-cli.py and return (exit_code, stdout, stderr)."""
    cmd = [sys.executable, os.path.join(_ROOT, "memory-cli.py"),
           "--memory-dir", mem_dir, tool]
    if args_dict:
        cmd.append(json.dumps(args_dict))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       cwd=_ROOT)
    return r.returncode, r.stdout, r.stderr


def _make_project():
    """Create temp project with .memory/ dir and empty graph."""
    d = tempfile.mkdtemp(prefix="mem-test-")
    mem = os.path.join(d, ".memory")
    os.makedirs(mem)
    open(os.path.join(mem, "graph.jsonl"), "w").close()
    return d, mem


# ---- Unit tests ----

def test_score_entity_signature():
    """score_entity takes (entity, now_ts) — no datetime obj."""
    print("=== score_entity signature ===")
    from maintenance import score_entity
    e = {"observations": ["x"],
         "_updated": "2026-03-15T00:00:00Z", "name": "a"}
    s = score_entity(e, time.time(),
                     cutoff_str="2025-12-01T00:00:00Z")
    check("positive_score_for_recent_entity", s > 0, f"got {s}")
    check("zero_score_for_no_observations",
          score_entity({"observations": []}, time.time()) == 0.0)


def test_main_branches_import():
    """_MAIN_BRANCHES imports from config with fallback."""
    print("=== MAIN_BRANCHES import ===")
    from maintenance import _MAIN_BRANCHES
    check("contains_main", "main" in _MAIN_BRANCHES)
    check("contains_all_4",
          len(_MAIN_BRANCHES) == 4, str(_MAIN_BRANCHES))


def test_read_recall_counts():
    """Returns dict from JSON file, empty dict on missing."""
    print("=== read_recall_counts ===")
    from maintenance import read_recall_counts
    d, mem = _make_project()
    with open(os.path.join(mem, "recall_counts.json"), "w") as f:
        json.dump({"foo": 3}, f)
    rc = read_recall_counts(mem)
    check("loads_existing_file", rc.get("foo") == 3, str(rc))
    check("returns_empty_dict_on_missing",
          read_recall_counts("/tmp/nonexistent") == {})


def test_write_direct():
    """_write_direct returns bool, writes data on success."""
    print("=== _write_direct ===")
    sys.path.insert(0, os.path.join(_ROOT, "hooks"))
    import capture_tool_context as ctc
    check("returns_false_on_bad_path",
          ctc._write_direct("/no/such/graph.jsonl", "{}") is False)
    fd, p = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    check("returns_true_on_good_path",
          ctc._write_direct(p, '{"ok":1}') is True)
    with open(p) as f:
        check("data_persisted", '{"ok":1}' in f.read())
    os.unlink(p)


def test_cache_total():
    """_cache_total sums all cache sizes."""
    print("=== cache._cache_total ===")
    from semantic_server.cache import _cache_total, index_cache
    check("zero_when_empty", _cache_total() == 0)
    index_cache["size"] = 5000
    check("reflects_changes", _cache_total() == 5000)
    index_cache["size"] = 0


def test_all_imports():
    """All public APIs importable without error."""
    print("=== imports ===")
    try:
        from semantic_server import main  # noqa: F401
        from semantic_server.graph import (  # noqa: F401
            load_index, load_graph_entities,
            load_graph_relations, invalidate_caches,
        )
        from semantic_server.search import search  # noqa: F401
        from semantic_server.tools import (  # noqa: F401
            create_entities, delete_entities,
            graph_stats, create_decision,
        )
        from semantic_server.traverse import (  # noqa: F401
            traverse_relations,
        )
        from semantic_server.recall import (  # noqa: F401
            init_recall_state,
        )
        from semantic_server.protocol import (  # noqa: F401
            handle_message,
        )
        from semantic_server.cache import (  # noqa: F401
            maybe_evict_caches, _cache_total,
        )
        from semantic_server.config import (  # noqa: F401
            MAIN_BRANCHES, now_iso,
        )
        check("all_imports_ok", True)
    except ImportError as exc:
        check("all_imports_ok", False, str(exc))


# ---- E2E CLI tests ----

def test_cli_graph_stats():
    """graph_stats returns entity/relation counts."""
    print("=== CLI: graph_stats ===")
    _, mem = _make_project()
    rc, out, err = _cli(mem, "graph_stats")
    check("exit_0", rc == 0, err[:200])
    check("returns_entity_count", '"entities": 0' in out)


def test_cli_create_entities_returns_count():
    """create_entities returns {created: N}, not entity names."""
    print("=== CLI: create_entities ===")
    _, mem = _make_project()
    rc, out, err = _cli(mem, "create_entities", {
        "entities": [{"name": "test-mod", "entityType": "Module",
                      "observations": ["handles auth"]}]
    })
    check("exit_0", rc == 0, err[:200])
    data = json.loads(out)
    check("returns_created_count",
          data.get("created") == 1, str(data))


def test_cli_create_and_search():
    """Created entity is findable via semantic search."""
    print("=== CLI: create + maintenance + search ===")
    proj, mem = _make_project()
    _cli(mem, "create_entities", {
        "entities": [{"name": "jwt-validator",
                      "entityType": "Module",
                      "observations": ["validates JWT tokens"]}]
    })
    subprocess.run(
        [sys.executable, os.path.join(_ROOT, "maintenance.py"),
         proj],
        capture_output=True, text=True,
    )
    rc, out, err = _cli(mem, "semantic_search_memory",
                        {"query": "JWT token validation"})
    check("search_exit_0", rc == 0, err[:200])
    check("search_finds_created_entity",
          "jwt-validator" in out, out[:200])


def test_cli_traverse():
    """traverse_relations returns nodes and edges."""
    print("=== CLI: traverse_relations ===")
    _, mem = _make_project()
    _cli(mem, "create_entities", {
        "entities": [{"name": "svc-a", "entityType": "Service",
                      "observations": ["core service"]}]
    })
    rc, out, err = _cli(mem, "traverse_relations",
                        {"entity": "svc-a"})
    check("exit_0", rc == 0, err[:200])
    check("returns_nodes_key", '"nodes"' in out)


def test_cli_decision_lifecycle():
    """create_decision + update_decision_outcome roundtrip."""
    print("=== CLI: decision lifecycle ===")
    _, mem = _make_project()
    rc, _, err = _cli(mem, "create_decision",
                      {"title": "Use Redis",
                       "rationale": "Fast cache"})
    check("create_exit_0", rc == 0, err[:200])
    rc, _, err = _cli(mem, "update_decision_outcome",
                      {"title": "Use Redis",
                       "outcome": "successful",
                       "lesson": "Latency dropped 40%"})
    check("update_exit_0", rc == 0, err[:200])


def test_maintenance_invalid_dir():
    """maintenance.py skips and logs when .memory/ missing."""
    print("=== maintenance: invalid dir ===")
    r = subprocess.run(
        [sys.executable,
         os.path.join(_ROOT, "maintenance.py"),
         "/tmp/no_such_dir_999"],
        capture_output=True, text=True,
    )
    check("reports_skip",
          "skipped" in r.stdout, r.stdout[:200])


# ---- Static checks ----

def test_shell_syntax():
    """All shell scripts pass bash -n."""
    print("=== shell syntax ===")
    for script in [
        "hooks/capture-tool-context.sh",
        "hooks/prime-memory.sh",
        "hooks/nudge-setup.sh",
        "setup-project.sh",
        "install.sh",
        "cleanup.sh",
    ]:
        path = os.path.join(_ROOT, script)
        r = subprocess.run(
            ["bash", "-n", path],
            capture_output=True, text=True,
        )
        check(f"syntax_{os.path.basename(script)}",
              r.returncode == 0, r.stderr[:200])


def test_smart_recall_tool_list():
    """smart_recall.py lists key tools."""
    print("=== smart_recall tool list ===")
    with open(os.path.join(
        _ROOT, "hooks/smart_recall.py"
    )) as f:
        src = f.read()
    for tool in ["semantic_search_memory",
                 "traverse_relations", "create_entities",
                 "create_decision", "graph_stats"]:
        check(f"lists_{tool}", tool in src)


def test_setup_dedup():
    """setup-project.sh uses single function for MCP removal."""
    print("=== setup-project.sh dedup ===")
    with open(os.path.join(_ROOT, "setup-project.sh")) as f:
        src = f.read()
    check("function_defined",
          "_remove_memory_servers()" in src)
    check("called_twice",
          src.count('_remove_memory_servers "') == 2)
    check("uses_local_vars", "local MCP_FILE" in src)


def test_shim_importerror_guard():
    """semantic_server.py catches ImportError."""
    print("=== shim guard ===")
    with open(os.path.join(
        _ROOT, "semantic_server.py"
    )) as f:
        src = f.read()
    check("has_importerror_catch",
          "except ImportError" in src)


def test_nudge_fallback_chain():
    """nudge-setup.sh has known install path fallback."""
    print("=== nudge fallback ===")
    with open(os.path.join(
        _ROOT, "hooks/nudge-setup.sh"
    )) as f:
        src = f.read()
    check("has_known_install_fallback",
          "/.claude/memory/setup-project.sh" in src)


def test_hook_exit_propagation():
    """capture-tool-context.sh propagates real failures."""
    print("=== hook exit propagation ===")
    with open(os.path.join(
        _ROOT, "hooks/capture-tool-context.sh"
    )) as f:
        src = f.read()
    check("propagates_nonzero", 'exit "$PY_EXIT"' in src)
    check("treats_2_as_success",
          '[ "$PY_EXIT" -eq 2 ]' in src)


if __name__ == "__main__":
    test_score_entity_signature()
    test_main_branches_import()
    test_read_recall_counts()
    test_write_direct()
    test_cache_total()
    test_all_imports()
    test_cli_graph_stats()
    test_cli_create_entities_returns_count()
    test_cli_create_and_search()
    test_cli_traverse()
    test_cli_decision_lifecycle()
    test_maintenance_invalid_dir()
    test_shell_syntax()
    test_smart_recall_tool_list()
    test_setup_dedup()
    test_shim_importerror_guard()
    test_nudge_fallback_chain()
    test_hook_exit_propagation()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

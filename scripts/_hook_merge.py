#!/usr/bin/env python3
import argparse
import json
import os
import sys

_MEMORY_COMMANDS = {
    '$HOME/.claude/hooks/prime-memory.sh',
    '$HOME/.claude/hooks/capture-decisions.sh',
    '$HOME/.claude/hooks/nudge-setup.sh',
    '$HOME/.claude/hooks/capture-tool-context.sh',
}


def _load(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        print(f'  [warn] {path} is corrupt — backing up and recreating')
        import shutil
        shutil.copy2(path, path + '.bak')
        return {}
    except OSError as e:
        print(f'  [error] {path} — {e}', file=sys.stderr)
        sys.exit(1)


def _dump(cfg, path):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mode_add(cfg, path, hook_file, event, timeout=None):
    hooks = cfg.setdefault('hooks', {})
    groups = hooks.setdefault(event, [])
    catch_all = None
    for g in groups:
        if g.get('matcher', '') == '':
            catch_all = g
            break
    if catch_all is None:
        catch_all = {'matcher': '', 'hooks': []}
        groups.append(catch_all)
    existing_cmds = {h.get('command', '') for h in catch_all.get('hooks', [])}
    if hook_file in existing_cmds:
        print(f'  [skip] {hook_file} already in {path} {event}')
        return False
    entry = {'type': 'command', 'command': hook_file}
    if timeout is not None:
        entry['timeout'] = timeout
    catch_all.setdefault('hooks', []).append(entry)
    return True


def mode_strip(cfg, path, hook_file, event, dry_run=False):
    hooks = cfg.get('hooks', {})
    if not hooks:
        print(f'  [skip] {path} — no hooks section')
        return False

    if hook_file:
        target_cmds = {hook_file}
    else:
        target_cmds = _MEMORY_COMMANDS

    changed = False
    for ev in list(hooks.keys()):
        if event and ev != event:
            continue
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        for group in groups:
            hook_list = group.get('hooks', [])
            original_len = len(hook_list)
            group['hooks'] = [
                h for h in hook_list
                if h.get('command', '') not in target_cmds
            ]
            if len(group['hooks']) < original_len:
                changed = True
        hooks[ev] = [g for g in groups if g.get('hooks')]
        if not hooks[ev]:
            del hooks[ev]
            changed = True

    return changed


def main():
    ap = argparse.ArgumentParser(description='Merge or strip hooks in settings.json')
    ap.add_argument('--mode', required=True, choices=['add', 'strip'])
    ap.add_argument('--settings', required=True)
    ap.add_argument('--hook-file', default='')
    ap.add_argument('--event', default='')
    ap.add_argument('--timeout', type=int, default=None)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not os.path.isfile(args.settings):
        if args.mode == 'add':
            cfg = {}
        else:
            print(f'  [skip] {args.settings} — not found')
            sys.exit(0)
    else:
        cfg = _load(args.settings)

    if args.mode == 'add':
        changed = mode_add(cfg, args.settings, args.hook_file, args.event, args.timeout)
        if changed:
            _dump(cfg, args.settings)
            print(f'  [ok] Merged memory hooks into {args.settings}')
        else:
            print(f'  [skip] Memory hooks already present in {args.settings}')
    else:
        if args.dry_run:
            print(f'  [dry-run] Would remove memory hooks from {args.settings}')
            sys.exit(0)
        changed = mode_strip(cfg, args.settings, args.hook_file, args.event)
        if not changed:
            print(f'  [skip] {args.settings} — no memory hooks found')
            sys.exit(0)
        cfg_hooks = cfg.get('hooks', {})
        if not cfg_hooks:
            cfg.pop('hooks', None)
        _dump(cfg, args.settings)
        print(f'  \033[0;32m[removed]\033[0m memory hooks from {args.settings}')


if __name__ == '__main__':
    main()

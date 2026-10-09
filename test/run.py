#!/usr/bin/env python3
"""Autonomous end-to-end tests for zehntage-gnome.

Starts a throwaway headless GNOME Shell on a private D-Bus session with
isolated XDG dirs, runtime dir and a keyfile GSettings backend (your real
settings, history, sockets and ~/.claude are never touched), installs the
extension plus a test helper, and drives it via org.gnome.Shell.Eval with
virtual pointer/keyboard devices. The Claude API is a local mock and
`claude` a fake by default.

    ./test/run.py                 # all mock scenarios
    ./test/run.py drag followup   # scenarios whose name contains a filter
    ./test/run.py --screens 1920x1080@1.25,1920x1080@1
    ./test/run.py --live          # real API (needs ANTHROPIC_API_KEY)
    ./test/run.py --live-cli      # real claude CLI with your login

The headless shell still reaches the real *system* bus (polkit, network
manager, geoclue); that is harmless here. Screenshots go to test/out/.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'test' / 'out'

# Re-run on a private session bus so we never talk to the real shell.
# Isolation is set up *before* the exec so dbus-daemon and every service it
# activates inherit it: XDG dirs and runtime dir in a temp dir, GSettings in
# an in-process keyfile, no real DISPLAY/XAUTHORITY, and (mock runs) a
# throwaway $HOME with a fake `claude` first on $PATH.
if not os.environ.get('ZT_TMP'):
    live = {'--live', '--live-cli'} & set(sys.argv)
    tmp = Path(tempfile.mkdtemp(prefix='zt-'))
    for d in ('config', 'data', 'cache', 'state', 'home', 'bin'):
        (tmp / d).mkdir()
    # Own runtime dir (sockets: Wayland, at-spi, ...). A subdirectory of the
    # real one, because socket paths must stay under 108 bytes.
    run = Path(os.environ.get('XDG_RUNTIME_DIR') or tmp) / f'zt-{os.getpid()}'
    run.mkdir(mode=0o700)
    (tmp / 'bin' / 'claude').symlink_to(ROOT / 'test' / 'fake-claude.py')
    # The API key never goes through the environment or argv: a 0600 file.
    key = os.environ.pop('ANTHROPIC_API_KEY', None)
    if key:
        fd = os.open(tmp / 'api-key', os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, key.encode())
        os.close(fd)
    for var in ('DISPLAY', 'XAUTHORITY', 'WAYLAND_SOCKET'):
        os.environ.pop(var, None)
    os.environ.update(
        ZT_TMP=str(tmp),
        ZT_RUN=str(run),
        ZT_REAL_HOME=str(Path.home()),
        ZT_BUS_BEFORE=os.environ.get('DBUS_SESSION_BUS_ADDRESS', ''),
        # Fixed socket name so bus-activated apps (the prefs dialog) find
        # the headless shell as their display.
        WAYLAND_DISPLAY='zt-wayland',
        XDG_RUNTIME_DIR=str(run),
        GSETTINGS_BACKEND='keyfile',
        XDG_CONFIG_HOME=str(tmp / 'config'),
        XDG_DATA_HOME=str(tmp / 'data'),
        XDG_CACHE_HOME=str(tmp / 'cache'),
        XDG_STATE_HOME=str(tmp / 'state'),
        XDG_DATA_DIRS='/usr/local/share:/usr/share')
    if not live:   # the real claude CLI needs the real home (its login)
        os.environ['HOME'] = str(tmp / 'home')
        os.environ['PATH'] = f"{tmp / 'bin'}:{os.environ['PATH']}"
    # Fresh screenshots; bus-activated services spam stderr on teardown,
    # keep it out of sight.
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob('*.png'):
        old.unlink()
    bus_log = os.open(OUT / 'bus.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    import subprocess
    try:
        code = subprocess.call(
            ['dbus-run-session', '--', sys.executable, *sys.argv],
            stderr=bus_log)
    except KeyboardInterrupt:
        code = 130
    # Only now is every bus-activated service gone (they write sockets into
    # the runtime dir until the private bus exits). The test session's
    # document portal may still hold a FUSE mount on run/doc for a moment.
    import time
    for _ in range(20):
        shutil.rmtree(run, ignore_errors=True)
        if not run.exists():
            break
        for mount in ('doc', 'gvfs'):
            subprocess.call(['fusermount3', '-u', '-z', str(run / mount)],
                            stderr=subprocess.DEVNULL)
        time.sleep(0.25)
    sys.exit(code)

import re  # noqa: E402
import threading  # noqa: E402
import traceback  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

import harness  # noqa: E402
from harness import (SCREENS, Handler, Shell, keyfile_paths,  # noqa: E402
                     real_dconf_dump, reset, shot)
import live as live_scenarios  # noqa: E402
import scenarios  # noqa: E402


def isolated(tmp):
    """Refuse to run outside the environment set up above."""
    env = os.environ
    return (env.get('GSETTINGS_BACKEND') == 'keyfile' and
            env['XDG_CONFIG_HOME'].startswith(str(tmp)) and
            env['XDG_RUNTIME_DIR'] == env['ZT_RUN'] and
            env.get('DBUS_SESSION_BUS_ADDRESS', '') != env['ZT_BUS_BEFORE'])


def main():
    args = sys.argv[1:]
    live = '--live' in args
    live_cli = '--live-cli' in args
    if '--screens' in args:
        spec = args[args.index('--screens') + 1]
        args.remove(spec)
        SCREENS[:] = [(int(w), int(h), float(sc)) for w, h, sc in
                      (re.match(r'(\d+)x(\d+)@([\d.]+)', p).groups()
                       for p in spec.split(','))]
    filters = [a for a in args if not a.startswith('--')]

    tmp = Path(os.environ['ZT_TMP'])
    if not isolated(tmp):
        sys.exit('refusing to run: not in the isolated environment')
    dconf_before = real_dconf_dump(tmp)
    (tmp / 'fake').mkdir(exist_ok=True)
    os.environ['ZT_FAKE_DIR'] = str(tmp / 'fake')

    key_file = tmp / 'api-key'
    if live_cli:
        settings = {'backend': "'cli'"}
        todo = [scenarios.t_loads, live_scenarios.live_cli]
    elif live:
        if not key_file.exists():
            print('--live needs ANTHROPIC_API_KEY')
            sys.exit(2)
        key = key_file.read_text().strip()
        key_file.unlink()
        settings = {'claude-api-key': f"'{key}'", 'backend': "'api'"}
        todo = [scenarios.t_loads, live_scenarios.live_api]
    else:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        settings = {'claude-api-key': "'test-key'",
                    'backend': "'api'",
                    'claude-path': f"'{harness.FAKE_CLAUDE}'",
                    'api-base-url':
                        f"'http://127.0.0.1:{server.server_port}'"}
        todo = [s for s in scenarios.SCENARIOS
                if not filters or any(f in s.__name__ for f in filters)]

    sh = Shell(tmp, settings)
    failed = 0
    try:
        sh.install()
        sh.start()
        for sc in todo:
            name = sc.__name__.removeprefix('t_')
            try:
                if sc is not scenarios.t_loads:
                    reset(sh)
                sc(sh)
                print(f'PASS  {name}')
            except Exception as e:
                failed += 1
                print(f'FAIL  {name}: {e}')
                if not isinstance(e, AssertionError):
                    traceback.print_exc(file=sys.stdout)
                try:
                    print('    events:', sh.js('return zt.events.slice(-12);'))
                except Exception:
                    pass
                try:
                    shot(sh, f'FAIL-{name}.png')
                except Exception:
                    pass
    finally:
        errs = sh.errors()
        sh.stop()
        # Every key the harness ever wrote (read back from its keyfile) is
        # guarded: if one changes in the real dconf, isolation is broken.
        ours_set = keyfile_paths(tmp)
        after = real_dconf_dump(tmp)
        changed = sorted(k for k in dconf_before.keys() | after.keys()
                         if dconf_before.get(k) != after.get(k))
        ours = [k for k in changed if k in ours_set]
        if ours:
            print(f'\n!!! ISOLATION BROKEN: real dconf keys the harness '
                  f'sets changed: {ours} !!!')
            failed += 1
        elif changed:
            print(f'\n(note: real dconf changed during the run, none of it '
                  f'ours — your session: {changed[:8]})')
    if errs:
        print('\nzehntage log lines with errors/warnings:')
        for line in errs[:40]:
            print('   ', line)
    ok = not failed and not errs
    if ok and not os.environ.get('ZT_KEEP'):   # ZT_KEEP=1: keep the logs
        shutil.rmtree(tmp, ignore_errors=True)   # incl. any key in keyfile
        where = ''
    else:
        if live:   # keep logs for debugging, but not the API key
            keyfile = tmp / 'config' / 'glib-2.0' / 'settings' / 'keyfile'
            keyfile.unlink(missing_ok=True)
        where = f'; kept: {tmp}'
    print(f'\n{len(todo) - failed}/{len(todo)} passed; screenshots: '
          f'{OUT}{where}')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)

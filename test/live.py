"""Live scenarios against the real Claude API / claude CLI (timed).

Not part of the mock suite: run.py runs them with --live / --live-cli.
"""

import re
import time

from harness import (check, followup, hotkey, press_strong, reset,  # noqa
                     shot, state, wait_state)

def live_api(sh):
    """Real Claude API: time to first visible words and to the full answer."""
    first_t, total_t = [], []
    for i in range(3):
        reset(sh)
        fx = sh.js("return zt.fixture('Das Eichhörnchen frisst Nüsse');")
        hotkey(sh)
        wait_state(sh, lambda s: s['selector'])
        time.sleep(0.8)   # a human takes this long to drag (the monitor is
        x, y, w, h = fx   # captured meanwhile)
        sh.js(f'await zt.drag({x - 10}, {y - 10}, '
              f'{x + w + 10}, {y + h + 10});')
        t0 = time.time()
        first = None
        while time.time() - t0 < 30:
            st = sh.js('const i = zt.inst(); '
                       'const e = i._history.entries[0]; '
                       'return [e?.status, !!i._jobs.get(e?.id)?.partial];')
            if first is None and (st[1] or st[0] == 'ok'):
                first = time.time() - t0
            if st[0] in ('ok', 'error'):
                break
            time.sleep(0.02)
        total = time.time() - t0
        s = state(sh)
        check(s['status'] == 'ok', s['error'])
        check(re.search('бел(к|оч)', s['turns'][0]['answer'].lower()), s['turns'][0])
        first_t.append(first)
        total_t.append(total)
        print(f'    run {i + 1}: first words {first:.2f}s, done {total:.2f}s'
              f'  {s["turns"][0]["answer"][:60]!r}')
    shot(sh, 'live.png')
    # One word only: the context (whole screen, red frame) should still let
    # the model read it in its sentence.
    reset(sh)
    sh.js("return zt.fixture('Das Eichhörnchen frisst Nüsse');")
    wx, wy, ww, wh = sh.js('''
        const t = zt._fixture.clutter_text, l = t.get_layout();
        const bytes = s => new TextEncoder().encode(s).length;
        const a = l.index_to_pos(bytes('Das Eichhörnchen '));
        const b = l.index_to_pos(bytes('Das Eichhörnchen frisst'));
        const [x, y] = t.get_transformed_position();
        // Pango units, at the text's resource scale (2 on a 1.25 monitor).
        const u = 1024 * t.get_resource_scale();
        return [x + a.x / u, y + a.y / u, (b.x - a.x) / u,
                a.height / u].map(Math.round);''')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    time.sleep(0.8)
    sh.js(f'await zt.drag({wx - 2}, {wy}, {wx + ww + 2}, {wy + wh});')
    t0 = time.time()
    s = wait_state(sh, lambda s: s['status'] in ('ok', 'error'), timeout=60)
    check(s['status'] == 'ok', s['error'])
    print(f'    one word ("frisst"), with context: {time.time() - t0:.2f}s  '
          f'{s["turns"][0]["answer"][:110]!r}')
    # Background fact-check by the real Opus over the real claude CLI.
    fc = None
    if sh.js("return zt.setting('org.gnome.shell.extensions.zehntage-gnome', "
             "'factcheck');") == 'false':
        print('    (fact-check off)')
        fc = {'skip': True}
    while time.time() - t0 < 180 and not fc:
        fc = sh.js('return zt.inst()._history.entries[0]?.turns[0]'
                   '?.factcheck ?? null;')
        time.sleep(0.5)
    check(fc, 'no fact-check result within 180 s')
    if not fc.get('skip'):
        print(f'    fact-check ({fc["model"]}): {time.time() - t0:.1f}s '
              f'after release, significant={fc["significant"]} '
              f'{fc["correction"][:100]!r}')
    t0 = time.time()
    sh.js("await zt.type('Wie ist der Plural?'); "
          "await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2 or
                   s['followUpError'], timeout=60)
    check(not s['followUpError'], s['followUpError'])
    print(f'    follow-up: {time.time() - t0:.2f}s  '
          f'{s["turns"][1]["answer"][:60]!r}')
    shot(sh, 'live-followup.png')
    # [Opus] rewrite of the last answer, real Opus 5.5 at high effort.
    t0 = time.time()
    press_strong(sh)
    first = None
    while time.time() - t0 < 120:
        st = sh.js('const i = zt.inst(); '
                   'const e = i._history.entries[0]; '
                   'const job = i._jobs.get(e.id); '
                   'return [!!job?.partial, !!job, e.strongError ?? null];')
        if first is None and st[0]:
            first = time.time() - t0
        if not st[1]:
            break
        time.sleep(0.05)
    check(not st[2], st[2])
    s = state(sh)
    check(s['turns'][-1].get('model') == 'claude-opus-5-5', s['turns'][-1])
    print(f'    Opus rewrite: first words {first or 0:.2f}s, done '
          f'{time.time() - t0:.2f}s  {s["turns"][-1]["answer"][:70]!r}')
    shot(sh, 'live-opus.png')


def live_cli(sh):
    """Real `claude -p` (your Claude Code login), timed end to end."""
    times = []
    for i in range(3):
        reset(sh)   # also clears fixtures, so add it each round
        fx = sh.js("return zt.fixture('Das Eichhörnchen frisst Nüsse');")
        sh.gset('backend', "'cli'")
        hotkey(sh)
        wait_state(sh, lambda s: s['selector'])
        time.sleep(0.8)   # a human takes this long to drag (the monitor is
        x, y, w, h = fx   # captured meanwhile)
        sh.js(f'await zt.drag({x - 10}, {y - 10}, '
              f'{x + w + 10}, {y + h + 10});')
        t0 = time.time()
        first = None
        while time.time() - t0 < 60:
            st = sh.js('const i = zt.inst(); '
                       'const e = i._history.entries[0]; '
                       'return [e?.status, !!i._jobs.get(e?.id)?.partial];')
            if first is None and (st[1] or st[0] == 'ok'):
                first = time.time() - t0
            if st[0] in ('ok', 'error'):
                break
            time.sleep(0.02)
        times.append(time.time() - t0)
        s = state(sh)
        check(s['status'] == 'ok', s['error'])
        check(not s['overview'], f'overview opened in run {i + 1}')
        check(re.search('бел(к|оч)', s['turns'][0]['answer'].lower()),
              f'answer does not mention the squirrel: {s["turns"][0]}')
        print(f'    run {i + 1}: first words {first:.2f}s, '
              f'done {times[-1]:.2f}s  '
              f'{s["turns"][0]["answer"][:70]!r}')
    shot(sh, 'live-cli.png')
    t0 = time.time()
    sh.js("await zt.type('Wie ist der Plural?'); "
          "await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2 or
                   s['followUpError'], timeout=60)
    check(not s['followUpError'], s['followUpError'])
    print(f'    follow-up: {time.time() - t0:.2f}s  '
          f'{s["turns"][1]["answer"][:70]!r}')
    shot(sh, 'live-cli-followup.png')
    check(min(times) < 4, f'too slow: {times}')

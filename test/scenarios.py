"""Mock scenarios: every t_* function here runs against the mock API.

The order is the definition order; run.py resets between scenarios.
"""

import base64
import re
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harness import (OUT, context_image, first_content, monitor_at,  # noqa
                     monitor_px, red)
from harness import (SUFFIX, ANSWER, REAL_HISTORY, ROOT, UUID, Mock,  # noqa
                     argval, capture, check, cyan, data_dir, decode_png,
                     entries, fake_calls, followup, full_px, hotkey,
                     labels, last_image, magenta, menu_text, noise_background,
                     png_size, press_cancel, press_strong, px,
                     reload_extension, shot, span, stage_size, state,
                     use_cli, wait_for, wait_state)

def t_loads(sh):
    info = sh.js('const e = zt.ext(); '
                 'return {state: e.state, error: e.error ?? null};')
    check(info['error'] in (None, ''), f'extension error: {info}')
    check(info['state'] == 1, f'extension not ACTIVE: {info}')


def t_hotkey_opens_selector_and_escape_cancels(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    shot(sh, 'selector-open.png')
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')
    s = wait_state(sh, lambda s: not s['selector'])
    check(s['entries'] == 0 and not Mock.requests, 'Escape must not send')


def t_right_click_cancels(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.click(300, 300, zt.Clutter.BUTTON_SECONDARY);')
    s = wait_state(sh, lambda s: not s['selector'])
    check(s['entries'] == 0 and not Mock.requests, 'right click must cancel')


def t_drag_select_sends_and_shows_answer(sh):
    fx = sh.js("return zt.fixture('Das Eichhörnchen');")
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    x, y, w, h = fx
    # Stop mid-drag once to see the selection frame.
    sh.js(f'await zt.moveTo({x - 10}, {y - 10}); zt.press(); '
          f'await zt.moveTo({x + w // 2}, {y + h // 2}); '
          f'await zt.moveTo({x + w + 10}, {y + h + 10}); await zt.sleep(100);')
    shot(sh, 'selecting.png')
    sh.js('zt.release();')
    s = wait_state(sh, lambda s: s['status'] == 'ok')
    check(s['menuOpen'], 'popup should be open with the answer')
    check(len(Mock.requests) == 1, f'{len(Mock.requests)} requests')
    req = Mock.requests[0]
    check(req['path'] == '/v1/messages', req['path'])
    check(req['headers'].get('x-api-key') == 'test-key', 'api key header')
    check(req['headers'].get('anthropic-version') == '2023-06-01', 'version')
    b = req['body']
    check(b['model'] == 'claude-haiku-5-5', b['model'])
    check(b['output_config'] == {'effort': 'low'}, b.get('output_config'))
    for bad in ('temperature', 'top_p', 'top_k'):
        check(bad not in b, f'{bad} must not be sent to Haiku 5.5')
    check('Russian' in b['system'], 'system prompt missing')
    img = last_image()
    check(img['source']['media_type'] == 'image/png', img['source'])
    size = png_size(img['source']['data'])
    exp = (span(sh, x - 10, x + w + 10), span(sh, y - 10, y + h + 10))
    check(size == exp, f'crop size {size} != {exp}')
    text = menu_text(sh)
    check(not state(sh)['overview'], 'Super+Z must not open the overview')
    check('Eichhörnchen' in text and 'белка' in text,
          f'answer not shown: {text!r}')
    shot(sh, 'answer-popup.png')
    thumb = sh.js('return zt.inst()._history.entries[0].thumbPath;')
    check(thumb and Path(thumb).exists(), 'no thumbnail written')
    tw, th = png_size(base64.b64encode(Path(thumb).read_bytes()))
    check(tw <= 640 and th <= 280, f'thumbnail {tw}x{th}')
    check(sh.js('return !!zt.inst()._indicator._thumbs.size;'),
          'popup does not use the in-memory thumbnail')
    sh.js('zt._fixture?.destroy(); zt._fixture = null;')


def t_click_captures_full_screen(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sw, shh = stage_size(sh)
    sh.js(f'await zt.click({sw // 2}, {shh // 2});')
    wait_state(sh, lambda s: s['status'] == 'ok')
    content = first_content()
    check(len(content) == 1, 'a click sends one image, no context')
    want = monitor_px(sh, sw // 2, shh // 2)
    check(png_size(content[0]['source']['data']) == want,
          f"{png_size(content[0]['source']['data'])} != monitor {want}")


def t_followup_typed_is_append_only(sh):
    sh.gset('thinking', 'true')   # so there are thinking blocks to replay
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 400, 300);')
    wait_state(sh, lambda s: s['status'] == 'ok')
    first = Mock.requests[0]['body']
    # Follow-up entry should already have key focus: just type + Enter.
    sh.js("await zt.type('und Plural?'); "
          "await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(len(Mock.requests) == 2, 'follow-up not sent')
    second = Mock.requests[1]['body']
    msgs = second['messages']
    check([m['role'] for m in msgs] == ['user', 'assistant', 'user'],
          [m['role'] for m in msgs])
    check(msgs[0] == first['messages'][0], 'image turn changed')
    check(msgs[1]['content'][0] == {'type': 'thinking', 'thinking': '',
                                    'signature': 'sig1'},
          'thinking block must be replayed verbatim')
    check(msgs[2]['content'] == 'und Plural?' + SUFFIX, msgs[2]['content'])
    check(second['system'] == first['system'], 'system changed')
    check('Antwort 2' in menu_text(sh), 'second answer not shown')
    shot(sh, 'followup.png')


def t_api_error_shown_and_retry(sh):
    Mock.mode = '401'
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    s = wait_state(sh, lambda s: s['status'] == 'error')
    check('401' in s['error'] and 'invalid x-api-key' in s['error'],
          s['error'])
    check('invalid x-api-key' in menu_text(sh), 'error not displayed')
    shot(sh, 'error.png')
    Mock.mode = 'ok'
    sh.js('const i = zt.inst(); i._askInitial(i._history.entries[0]);')
    wait_state(sh, lambda s: s['status'] == 'ok')


def t_refusal_is_error(sh):
    Mock.mode = 'refusal'
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    s = wait_state(sh, lambda s: s['status'] == 'error')
    check('declined' in s['error'], s['error'])


def t_pending_state_visible(sh):
    Mock.delay = 1.5
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['status'] == 'pending' and s['menuOpen'])
    check('Thinking' in menu_text(sh), 'no pending indicator')
    shot(sh, 'pending.png')
    wait_state(sh, lambda s: s['status'] == 'ok')


def t_hotkey_while_popup_open(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['status'] == 'ok' and s['menuOpen'])
    hotkey(sh)
    s = wait_state(sh, lambda s: s['selector'])
    check(not s['menuOpen'], 'menu should close before selecting')
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')
    wait_state(sh, lambda s: not s['selector'])


def t_menu_item_starts_capture(sh):
    sh.js('const i = zt.inst(); i._indicator.menu.open(0); '
          'await zt.sleep(100); '
          'i._indicator.menu._getMenuItems()[0].activate('
          'zt.Clutter.get_current_event());')
    wait_state(sh, lambda s: s['selector'] and not s['menuOpen'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_history_survives_reload(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['status'] == 'ok')
    reload_extension(sh)
    s = state(sh)
    check(s['entries'] == 1 and s['status'] == 'ok', f'history lost: {s}')
    check(s['turns'][0]['answer'] == ANSWER, 'answer lost')


def t_no_key_shows_hint(sh):
    sh.gset('claude-api-key', "''")       # reset() restores it
    hotkey(sh)
    s = wait_state(sh, lambda s: s['menuOpen'])
    check(not s['selector'], 'selector must not open without a key')
    check('API key is not set' in menu_text(sh), 'no key hint')
    shot(sh, 'no-key.png')


def t_prefs_dialog_opens(sh):
    sh.js('zt.inst().openPreferences();')
    title = sh.js('''
        return await zt.waitFor(() => global.display.list_all_windows()
            .map(w => w.get_title()).find(t => t?.includes('Zehntage')),
            15000);
    ''', timeout=20)
    check(title, 'prefs window did not appear')
    time.sleep(1)
    shot(sh, 'prefs.png')
    sh.js('global.display.list_all_windows().forEach(w => w.delete(0));')
    time.sleep(0.5)
    bus = (OUT / 'bus.log').read_text(errors='replace')
    bad = [line for line in bus.splitlines()
           if '@lyka/prefs.js' in line or
           ('zehntage' in line.lower() and 'Error' in line)]
    check(not bad, f'prefs dialog errors: {bad[:3]}')


def t_cli_drag_sends_fast_flags(sh):
    use_cli(sh)
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 400, 260);')
    s = wait_state(sh, lambda s: s['status'] in ('ok', 'error'))
    check(s['status'] == 'ok', s['error'])
    check(not Mock.requests, 'cli backend must not hit the API')
    calls = fake_calls()
    check(len(calls) == 1, f'{len(calls)} cli calls')
    argv = calls[0]['argv']
    for flag in ('-p', '--safe-mode', '--strict-mcp-config',
                 '--no-session-persistence', '--verbose',
                 '--include-partial-messages'):
        check(flag in argv, f'missing {flag}')
    check(argval(argv, '--model') == 'claude-haiku-5-5', argv)
    check(argval(argv, '--effort') == 'low', argv)
    check(argval(argv, '--tools') == '', 'tools must be disabled')
    check(argval(argv, '--input-format') == 'stream-json', argv)
    check('Russian' in argval(argv, '--system-prompt'), 'system prompt')
    env = calls[0]['env']
    check(env.get('DISABLE_AUTOUPDATER') == '1' and
          env.get('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC') == '1', env)
    msg = json.loads(calls[0]['stdin'])
    images = [b for b in msg['message']['content'] if b['type'] == 'image']
    check(msg['type'] == 'user' and len(images) == 2,
          'cli gets context + selection')
    img = images[-1]
    check(png_size(img['source']['data']) ==
          (span(sh, 100, 400), span(sh, 100, 260)), 'crop size')
    check('белка (cli)' in menu_text(sh), 'cli answer not shown')
    shot(sh, 'cli-answer.png')


def t_cli_followup_carries_transcript(sh):
    use_cli(sh)
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['status'] == 'ok')
    sh.js("await zt.type('und Plural?'); "
          "await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    calls = fake_calls()
    check(len(calls) == 2, f'{len(calls)} cli calls')
    content = json.loads(calls[1]['stdin'])['message']['content']
    check(sum(b['type'] == 'image' for b in content) == 2,
          'images missing in follow-up')
    text = content[-1]['text']
    check('белка (cli)' in text and 'New question: und Plural?' in text,
          text)
    check('CLI Antwort 2' in menu_text(sh), 'follow-up answer not shown')


def t_cli_error_result_shown(sh):
    use_cli(sh, 'error')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    s = wait_state(sh, lambda s: s['status'] == 'error')
    check('Please run /login' in s['error'], s['error'])


def t_cli_crash_shows_stderr(sh):
    use_cli(sh, 'crash')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    s = wait_state(sh, lambda s: s['status'] == 'error')
    check('something exploded' in s['error'], s['error'])


def t_cli_missing_binary_hint(sh):
    use_cli(sh)
    sh.gset('claude-path', "'/nonexistent/claude'")   # reset() restores
    hotkey(sh)
    s = wait_state(sh, lambda s: s['menuOpen'])
    check(not s['selector'], 'selector must not open')
    check('claude CLI not found' in menu_text(sh), 'no cli hint')


# ----- geometry / pixels

def t_crop_alignment_every_monitor(sh):
    mons = sh.js('return zt.Main.layoutManager.monitors.map('
                 'm => [m.x, m.y, m.width, m.height]);')
    boxes = [(mx + 100, my + 120, 200, 90) for mx, my, _, _ in mons]
    if len(mons) > 1:   # straddling the seam between monitor 1 and 2
        boxes.append((mons[1][0] - 100, 400, 200, 90))
    for bx, by, bw, bh in boxes:
        sh.js(f'zt.box({bx}, {by}, {bw}, {bh});')
    for bx, by, bw, bh in boxes:
        # Inset 4px: every edge pixel must be magenta.
        capture(sh, bx + 4, by + 4, bx + bw - 4, by + bh - 4)
        w, h, pix = decode_png(last_image()['source']['data'])
        exp = (span(sh, bx + 4, bx + bw - 4), span(sh, by + 4, by + bh - 4))
        check((w, h) == exp, f'box {bx},{by}: size {(w, h)} != {exp}')
        for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
                     (w // 2, h // 2)):
            check(magenta(pix(x, y)),
                  f'box {bx},{by}: pixel {x},{y} = {pix(x, y)}')
        # Margin 10px: corners outside the box, box edge just inside.
        capture(sh, bx - 10, by - 10, bx + bw + 10, by + bh + 10)
        w, h, pix = decode_png(last_image()['source']['data'])
        m, = px(sh, 10)
        for x, y in ((1, 1), (w - 2, h - 2)):
            check(not magenta(pix(x, y)), f'box {bx},{by}: corner {x},{y} '
                  f'should be outside: {pix(x, y)}')
        for x, y in ((m + 2, m + 2), (w - m - 3, h - m - 3)):
            check(magenta(pix(x, y)), f'box {bx},{by}: {x},{y} should be '
                  f'inside: {pix(x, y)} (off-by-scale?)')
    shot(sh, 'boxes.png')


def t_reverse_drag(sh):
    sh.js('zt.box(300, 300, 160, 80);')
    capture(sh, 456, 376, 304, 304)   # bottom-right -> top-left
    w, h, pix = decode_png(last_image()['source']['data'])
    exp = (span(sh, 304, 456), span(sh, 304, 376))
    check((w, h) == exp, f'size {(w, h)} != {exp}')
    check(magenta(pix(0, 0)) and magenta(pix(w - 1, h - 1)), 'misaligned')


def t_drag_to_screen_edges(sh):
    sw, shh = stage_size(sh)
    capture(sh, 0, 0, sw - 1, shh - 1)
    w, h = png_size(last_image()['source']['data'])
    fw, fh = full_px(sh)
    check(abs(w - fw) <= 3 and abs(h - fh) <= 3, f'{(w, h)} vs {(fw, fh)}')


def t_tiny_drag_is_full_screen(sh):
    capture(sh, 300, 300, 302, 302)
    check(png_size(last_image()['source']['data']) ==
          monitor_px(sh, 301, 301) and context_image() is None,
          'tiny drag should capture the monitor, as a click')


def t_menu_not_in_capture(sh):
    sh.js('zt.inst()._indicator.menu.open(0); await zt.sleep(200);')
    rect = sh.js("""
        const a = zt.inst()._indicator.menu.actor;
        const [x, y] = a.get_transformed_position();
        return [x, y, a.width, a.height].map(Math.round);""")
    mx, my, mw, mh = rect
    sh.js(f'zt.box({mx}, {my}, {mw}, {mh});')   # under the menu
    sh.js('zt.inst()._indicator.menu._getMenuItems()[0].activate('
          'zt.Clutter.get_current_event());')
    wait_state(sh, lambda s: s['selector'])
    # Click on the menu's monitor: that monitor is captured.
    ox, oy, ow, oh = monitor_at(sh, mx + mw // 2, my + mh // 2)
    sh.js(f'await zt.click({ox + ow // 2}, {oy + oh // 2});')
    wait_state(sh, lambda s: s['status'] == 'ok')
    w, h, pix = decode_png(last_image()['source']['data'])
    k = w / span(sh, ox, ox + ow)
    cx, cy = (span(sh, ox, mx + mw // 2), span(sh, oy, my + mh // 2))
    check(magenta(pix(int(cx * k), int(cy * k))),
          'the popup menu leaked into the frozen screenshot')


# ----- selector lifecycle / input

def t_escape_mid_drag(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.moveTo(100, 100); zt.press(); '
          'await zt.moveTo(300, 300); await zt.chord(zt.Clutter.KEY_Escape); '
          'zt.release(); await zt.sleep(100);')
    s = wait_state(sh, lambda s: not s['selector'])
    check(s['entries'] == 0 and not Mock.requests, 'escape must cancel')


def t_double_hotkey_single_overlay(sh):
    sh.js('const c = zt.Clutter; '
          'await zt.chord(c.KEY_Super_L, c.KEY_z); '
          'await zt.chord(c.KEY_Super_L, c.KEY_z);')
    wait_state(sh, lambda s: s['selector'])
    time.sleep(0.3)
    m = sh.js('return zt.modal();')
    check(m['shades'] == 1 and m['modalCount'] == 1, f'double overlay: {m}')
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')
    wait_state(sh, lambda s: not s['selector'])


def t_hotkey_works_in_overview(sh):
    sh.js('zt.Main.overview.show(); '
          'await zt.waitFor(() => zt.Main.overview.visible);')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')
    wait_state(sh, lambda s: not s['selector'])


def t_second_accelerator_xf86favorites(sh):
    sh.gset('capture-hotkey', "['<Super>z', 'XF86Favorites']")
    sh.js('await zt.chord(zt.Clutter.KEY_Favorites);')
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')
    wait_state(sh, lambda s: not s['selector'])
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_hotkey_rebind_at_runtime(sh):
    sh.gset('capture-hotkey', "['<Super>x']")
    hotkey(sh)
    time.sleep(0.6)
    check(not state(sh)['selector'], 'old hotkey still active')
    sh.js('await zt.chord(zt.Clutter.KEY_Super_L, zt.Clutter.KEY_x);')
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_hotkey_during_pending_two_entries(sh):
    Mock.delay = 1.5
    Mock.answer = lambda n, body: f'Antwort {n}'
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['status'] == 'pending')
    hotkey(sh)    # popup is open and a request is in flight
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(400, 300, 600, 400);')
    es = wait_for(lambda: (lambda es: es if len(es) == 2 and all(
        e['status'] == 'ok' for e in es) else None)(entries(sh)), 10,
        'both entries never finished')
    check([e['turns'][0]['answer'] for e in es] == ['Antwort 2', 'Antwort 1'],
          f'answers mixed up: {es}')


# ----- disable at awkward moments (a stuck modal would freeze the shell)


def t_disable_while_selecting(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    m = reload_extension(sh)
    check(m['modalCount'] == 0 and m['shades'] == 0, f'stuck modal: {m}')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_disable_during_screen_freeze_race(sh):
    sh.js(f"""
        zt.inst()._startCapture();   // now awaiting the stage capture
        await zt.Main.extensionManager.disableExtension('{UUID}');
        await zt.sleep(500);
        globalThis.__modal = zt.modal();
        await zt.Main.extensionManager.enableExtension('{UUID}');
        await zt.sleep(200);
    """)
    m = sh.js('return globalThis.__modal;')
    check(m['modalCount'] == 0 and m['shades'] == 0,
          f'overlay appeared after disable: {m}')
    time.sleep(1.5)   # a late overlay from the orphaned capture?
    m = sh.js('return zt.modal();')
    check(m['modalCount'] == 0 and m['shades'] == 0,
          f'overlay appeared late after disable: {m}')


def t_disable_during_pending_request(sh):
    Mock.delay = 2.0
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['status'] == 'pending')
    reload_extension(sh)
    Mock.wait_idle()   # the aborted request has settled
    s = state(sh)
    check(s['entries'] == 1 and s['status'] == 'error' and
          s['error'] == 'Interrupted', f'after re-enable: {s}')
    sh.js('const i = zt.inst(); i._askInitial(i._history.entries[0]);')
    wait_state(sh, lambda s: s['status'] == 'ok', timeout=10)


# ----- history persistence


def t_history_cap_evicts_images(sh):
    sh.gset('history-size', '2')
    capture(sh, 100, 100, 200, 200)
    first = entries(sh)[0]['imagePath']
    check(Path(first).exists(), 'image not stored')
    capture(sh, 100, 100, 220, 200)
    capture(sh, 100, 100, 240, 200)
    es = entries(sh)
    check(len(es) == 2, f'{len(es)} entries kept')
    check(not Path(first).exists(), 'evicted image file not deleted')
    check(all(Path(e['imagePath']).exists() for e in es), 'kept image gone')


def t_corrupt_history_recovers(sh):
    capture(sh, 100, 100, 200, 200)
    old = sh.js('const e = zt.inst()._history.entries[0]; '
                'return [e.imagePath, e.thumbPath, e.contextPath];')
    (data_dir() / 'history.json').write_text('{ this is not json')
    # keep the garbage on disk (disable would save a good history)
    reload_extension(sh, 'zt.inst()._history.save = () => {};')
    check(state(sh)['entries'] == 0, 'garbage history should load empty')
    bak = data_dir() / 'history.json.bak'
    check(bak.read_text() == '{ this is not json', 'garbage not kept as .bak')
    capture(sh, 100, 100, 200, 200)
    reload_extension(sh)
    check(all(Path(p).exists() for p in old),
          'images of the .bak history must stay restorable')


def t_orphan_images_deleted_on_load(sh):
    capture(sh, 100, 100, 200, 200)
    d = data_dir()
    for name in ('1-2.png', '1-2.ctx.jpg', '1-2.thumb.png'):
        (d / name).write_bytes(b'x')
    (d / 'mine.png').write_bytes(b'x')          # not ours: kept
    reload_extension(sh)
    check(not any((d / n).exists() for n in
                  ('1-2.png', '1-2.ctx.jpg', '1-2.thumb.png')), 'orphans left')
    e = entries(sh)[0]
    check((d / 'mine.png').exists() and Path(e['imagePath']).exists(),
          'deleted a file that is not an orphan')
    check(state(sh)['entries'] == 1, 'entry lost')
    (d / 'mine.png').unlink()
    # Leftovers of earlier scenarios count as orphans too.
    check(len(list(d.glob('*.thumb.png'))) == 1, list(d.iterdir()))


def t_legacy_gemini_entry_renders_and_follows_up(sh):
    capture(sh, 100, 100, 200, 200)
    sh.js("""
        const i = zt.inst(), e = i._history.entries[0];
        for (const k of ['backend', 'model', 'system', 'mediaType'])
            delete e[k];
        e.turns = [{answer: 'old **gemini** answer'}];
        i._history.save();""")
    reload_extension(sh)
    sh.js('zt.inst()._indicator.menu.open(0); await zt.sleep(200);')
    check('old gemini answer' in menu_text(sh), 'legacy answer not rendered')
    Mock.requests.clear()
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('noch?'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    body = Mock.requests[0]['body']
    check(body['messages'][1] == {'role': 'assistant', 'content': [
        {'type': 'text', 'text': 'old **gemini** answer'}]}, body['messages'])
    check(body['model'] == 'claude-haiku-5-5', body['model'])


# ----- rendering

def t_markup_special_chars_render_literally(sh):
    Mock.answer = lambda n, b: ('a < b && c > d <b>raw</b> **fett** '
                                'unbalanced ** `code <x>` & done')
    capture(sh, 100, 100, 200, 200)
    text = menu_text(sh)
    for frag in ('a < b && c > d', '<b>raw</b>', 'fett', 'code <x>', 'done'):
        check(frag in text, f'{frag!r} not shown literally: {text!r}')


def t_long_answer_stays_on_screen(sh):
    Mock.answer = lambda n, b: '\n'.join(f'- Zeile {i}' for i in range(150))
    capture(sh, 100, 100, 200, 200)
    box = sh.js('''
        const a = zt.inst()._indicator.menu.actor;
        const [x, y] = a.get_transformed_position();
        const m = zt.Main.layoutManager.findMonitorForActor(a);
        return {y, h: a.height, top: m.y, bottom: m.y + m.height};''')
    check(box['y'] >= box['top'] and box['y'] + box['h'] <= box['bottom'],
          f'menu leaves its monitor: {box}')
    shot(sh, 'long-answer.png')


# ----- follow-ups

def t_enter_twice_sends_one_followup(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.delay = 1.0
    sh.js("await zt.type('x'); const c = zt.Clutter; "
          "await zt.chord(c.KEY_Return); await zt.chord(c.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    Mock.wait_idle()
    time.sleep(0.3)   # grace: a queued second request would show up now
    check(len(Mock.requests) == 2, f'{len(Mock.requests)} requests')


def t_followup_error_then_recovers(sh):
    Mock.status = lambda n: 401 if n == 2 else 200
    capture(sh, 100, 100, 200, 200)
    sh.js("await zt.type('q1'); await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: s['followUpError'])
    check('401' in s['followUpError'] and len(s['turns']) == 1, s)
    check('q1' in menu_text(sh), 'follow-up error not displayed')
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('q2'); await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(not s['followUpError'], 'stale follow-up error')
    msgs = Mock.requests[2]['body']['messages']
    check([m['role'] for m in msgs] == ['user', 'assistant', 'user'] and
          msgs[2]['content'] == 'q2' + SUFFIX,
          f'failed q1 leaked into history: {msgs}')


# ----- settings / backends

def t_backend_is_pinned_per_entry(sh):
    capture(sh, 100, 100, 200, 200)            # via API
    use_cli(sh)
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('weiter'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(len(Mock.requests) == 2 and not fake_calls(),
          "follow-up must stay on the entry's backend")
    capture(sh, 100, 100, 220, 200)            # new entry -> CLI
    check(len(fake_calls()) == 1 and entries(sh)[0]['backend'] == 'cli',
          'new capture should use the CLI')


def t_effort_and_model_settings(sh):
    sh.gset('effort', "'medium'")
    sh.gset('model', "'claude-test-x'")
    capture(sh, 100, 100, 200, 200)
    b = Mock.requests[0]['body']
    check(b['model'] == 'claude-test-x' and
          b['output_config'] == {'effort': 'medium'}, b)
    sh.gset('model', "'claude-other'")
    sh.js("await zt.type('und?'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(Mock.requests[1]['body']['model'] == 'claude-test-x',
          'model must stay pinned for the conversation')


def t_cli_garbage_output(sh):
    use_cli(sh, 'garbage')
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'error' and 'weird failure' in s['error'], s)


def t_cli_hang_is_killed_on_timeout(sh):
    sh.gset('cli-timeout', '2')
    use_cli(sh, 'hang')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 200, 200);')
    # From the moment the request starts (a 4K capture alone takes ~3 s).
    wait_state(sh, lambda s: s['busy'], timeout=15)
    t0 = time.time()
    s = wait_state(sh, lambda s: s['status'] == 'error', timeout=15)
    check('timed out' in s['error'], s)
    check(time.time() - t0 < 4, f'timeout took {time.time() - t0:.1f}s')
    pid = int((Path(os.environ['ZT_FAKE_DIR']) / 'hang.pid').read_text())

    def gone():
        try:
            return 'zombie' in Path(f'/proc/{pid}/status').read_text()
        except FileNotFoundError:
            return True
    wait_for(gone, 3, f'hung claude process {pid} left running')


def t_thin_horizontal_drag_completes(sh):
    """Underlining a line of text: 200x0 drag must not hang."""
    s = capture(sh, 200, 300, 400, 300)
    check(s['status'] == 'ok', s)
    w, h = png_size(last_image()['source']['data'])
    check((w, h) == (span(sh, 200, 400), span(sh, 288, 312)), f'{(w, h)}')


def t_thin_drag_at_screen_edge_clamped(sh):
    shh = sh.js('return zt.Main.layoutManager.monitors[0].height;')
    s = capture(sh, 100, shh - 1, 300, shh - 1)   # bottom edge, 0 high
    check(s['status'] == 'ok', s)
    w, h = png_size(last_image()['source']['data'])
    check(w == span(sh, 100, 300) and 0 < h <= px(sh, 24)[0] + 1, (w, h))


def t_legacy_entry_uses_current_backend(sh):
    """Gemini-era entries (no backend/model/system) on the CLI backend."""
    use_cli(sh)
    capture(sh, 100, 100, 200, 200)
    sh.js("""
        const i = zt.inst(), e = i._history.entries[0];
        for (const k of ['backend', 'model', 'system', 'mediaType'])
            delete e[k];
        e.turns = [{answer: 'gemini says hi'},
                   {question: 'q', answer: '⚠ Gemini API error 503: x'}];
        i._history.save();""")
    reload_extension(sh)
    sh.js('zt.inst()._indicator.menu.open(0); await zt.sleep(200);')
    check('Gemini API error' not in menu_text(sh), 'stored error shown as answer')
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('noch?'); await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2 or
                   s['followUpError'])
    check(not s['followUpError'], s['followUpError'])
    calls = fake_calls()
    check(len(calls) == 2, f'follow-up went elsewhere: {len(calls)} cli calls')
    text = json.loads(calls[1]['stdin'])['message']['content'][-1]['text']
    check('gemini says hi' in text and '⚠' not in text, text)


def _real_history_fingerprint():
    return sorted((f.name, f.stat().st_size, f.stat().st_mtime_ns)
                  for f in REAL_HISTORY.iterdir())


def t_real_user_history_copy(sh):
    """Your actual history (any era), copied with rewritten paths."""
    src = REAL_HISTORY / 'history.json'
    if not src.exists():
        print('    (no real history on this machine; skipped)')
        return
    before = _real_history_fingerprint()
    dst = data_dir()
    data = json.loads(src.read_text())
    for e in data:
        for key in ('imagePath', 'contextPath'):   # never point at the
            if e.get(key):                         # real files
                name = Path(e[key]).name
                shutil.copy(REAL_HISTORY / name, dst / name)
                e[key] = str(dst / name)
        e.pop('thumbPath', None)           # (v2 adds these; regenerate)
    leaks = [v for e in data for v in e.values()
             if isinstance(v, str) and v.startswith(str(REAL_HISTORY))]
    check(not leaks, f'copy still points at real files: {leaks[:3]}')
    use_cli(sh)
    sh.js(f"""
        const m = zt.Main.extensionManager;
        const i = zt.inst();
        i._history.save = () => {{}};
        await m.disableExtension('{UUID}');
    """)
    (dst / 'history.json').write_text(json.dumps(data))
    sh.js(f"""await zt.Main.extensionManager.enableExtension('{UUID}');
              await zt.sleep(300);""")
    es = entries(sh)
    check(len(es) == len(data), f'{len(es)} of {len(data)} entries loaded')
    # Legacy entries get thumbnails in the background.
    wait_for(lambda: all(sh.js('return zt.inst()._history.entries.map('
                               'e => !!e.thumbPath);')), 15,
             'legacy thumbnails never created')
    thumbs = sh.js('return zt.inst()._history.entries.map(e => e.thumbPath);')
    check(all(thumbs), f'{thumbs.count(None)} thumbnails missing')
    for t in thumbs:
        tw, th = png_size(base64.b64encode(Path(t).read_bytes()))
        check(tw <= 640 and th <= 280, f'thumbnail too big: {tw}x{th}')
        check(str(t).startswith(str(dst)), f'thumbnail outside test dir: {t}')
    # Gemini-era entries adopt the current backend; v2 ones keep theirs.
    want = [src_e.get('backend', 'cli') for src_e in data]
    check([e['backend'] for e in es] == want and all(e['model'] for e in es),
          f'migration: {[e["backend"] for e in es]} vs {want}')
    t0 = time.time()
    sh.js('zt.inst()._indicator.menu.open(0); await zt.sleep(300);')
    opened = time.time() - t0
    shot(sh, 'real-history.png')
    check(opened < 1, f'opening the menu took {opened:.1f}s')
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('und?'); await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: s['followUpError'] or
                   len(s['turns'] or []) > len(es[0]['turns']))
    check(not s['followUpError'], s['followUpError'])
    errs = [n for n, e in enumerate(es) if e['status'] == 'error']
    if errs:
        sh.js(f'const i = zt.inst(); i._askInitial(i._history.entries[{errs[0]}]);')
        wait_for(lambda: entries(sh)[errs[0]]['status'] == 'ok', 8,
                 'retry of old error')
    capture(sh, 100, 100, 200, 200)   # evicts the oldest copied entry
    check(_real_history_fingerprint() == before,
          'REAL history directory was modified!')


def t_collapsed_rows_are_single_line(sh):
    Mock.status = lambda n: 400 if n == 1 else 200
    capture(sh, 100, 100, 200, 200)       # error entry
    sh.js("""const e = zt.inst()._history.entries[0];
             e.error = 'API error 403: {\\n  "error": {\\n    "code": 403';""")
    capture(sh, 100, 100, 220, 200)       # newer ok entry -> first collapses
    rows = labels(sh, 'zehntage-collapsed-label')
    check(rows, 'no collapsed rows rendered')
    check(all('\n' not in t for t in rows), f'multi-line row: {rows}')


def _followup_text(sh):
    return sh.js('return zt.inst()._indicator._focusTarget?.get_text() ?? null;')


def t_followup_draft_survives_pending_and_rerender(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.delay = 1.5
    sh.js("await zt.type('erste'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: s['busy'] == 'followUp')
    # Type the next question while the first is pending, press Enter too.
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('zweite'); await zt.chord(zt.Clutter.KEY_Return);")
    check(_followup_text(sh) == 'zweite',
          f'text lost while pending: {_followup_text(sh)!r}')
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)  # re-render
    check(_followup_text(sh) == 'zweite',
          f'draft lost on re-render: {_followup_text(sh)!r}')
    check(len(Mock.requests) == 2, 'second question must not be sent yet')
    Mock.delay = 0
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 3)
    check(Mock.requests[2]['body']['messages'][-1]['content'] ==
          'zweite' + SUFFIX,
          'draft not sent')
    check(_followup_text(sh) == '', 'field not cleared after sending')


def t_noise_image_respects_size_limit(sh):
    """Incompressible full-screen noise: payload must stay under 5 MB."""
    noise_background(sh, 'noise.png')
    sw, shh = stage_size(sh)
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js(f'await zt.click({sw // 2}, {shh // 2});')
    s = wait_state(sh, lambda s: s['status'] in ('ok', 'error'), timeout=20)
    check(s['status'] == 'ok', s)
    src = last_image()['source']
    check(len(src['data']) < 5_000_000, f'base64 {len(src["data"])} bytes')
    raw = base64.b64decode(src['data'])
    magic = {'image/png': b'\x89PNG', 'image/jpeg': b'\xff\xd8'}
    check(raw.startswith(magic[src['media_type']]), 'type/bytes mismatch')
    iw, ih, _ = decode_png(src['data'])
    check(max(iw, ih) <= 1568, f'{iw}x{ih} not downscaled')
    print(f"    {src['media_type']} {iw}x{ih}, {len(raw) / 1e6:.2f} MB raw")


def t_max_tokens_scales_with_effort(sh):
    for effort, expected in (('low', 8000), ('max', 32000)):
        sh.gset('effort', f"'{effort}'")
        capture(sh, 100, 100, 200, 200)
        b = Mock.requests[-1]['body']
        check(b['max_tokens'] == expected and
              b['output_config']['effort'] == effort, b)


def t_bad_markup_falls_back_to_plain_text(sh):
    Mock.answer = lambda n, b: '**a _b** c_ and `x*y` *z*'
    log = Path(os.environ['ZT_TMP']) / 'shell.log'
    before = log.read_text(errors='replace').count('Failed to set the markup')
    capture(sh, 100, 100, 200, 200)
    answer = labels(sh, 'zehntage-answer')
    check(answer == ['**a _b** c_ and `x*y` *z*'],
          f'invalid markup should show as plain text: {answer}')
    after = log.read_text(errors='replace').count('Failed to set the markup')
    check(after == before, 'invalid Pango markup reached set_markup()')


def t_cli_path_with_tilde(sh):
    # The shell's $HOME (a throwaway dir in mock runs) gets a link.
    home = Path(sh.js("return zt.getenv('HOME');"))
    link = home / 'my-claude'
    link.unlink(missing_ok=True)
    link.symlink_to(ROOT / 'test' / 'fake-claude.py')
    use_cli(sh)
    sh.gset('claude-path', "'~/my-claude'")   # reset() restores it
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'ok', s)


def t_capture_failure_is_visible(sh):
    sh.js("""const sel = zt.inst()._selector;
             globalThis.__origSelect = sel.select;
             sel.select = async () => { throw new Error('boom in selector'); };""")
    try:
        hotkey(sh)
        found = sh.js("""return await zt.waitFor(() => zt.Main.messageTray
            .getSources().flatMap(s => s.notifications)
            .find(n => `${n.title} ${n.body}`.includes('boom in selector')),
            3000).then(() => true, () => false);""")
        check(found, 'capture error was swallowed silently')
    finally:
        sh.js("""const sel = zt.inst()._selector;
                 sel.select = globalThis.__origSelect;
                 zt.Main.messageTray.getSources().forEach(s => s.destroy());""")


def t_light_and_dark_theme_screenshots(sh):
    Mock.answer = lambda n, b: ('**Перевод:** Белка ест орехи.\n\n'
                                '- *das* Eichhörnchen')
    Mock.status = lambda n: 401 if n == 1 else 200
    capture(sh, 100, 100, 200, 200)           # an error entry
    for scheme in ('prefer-light', 'prefer-dark'):
        sh.gset('color-scheme', f"'{scheme}'", 'org.gnome.desktop.interface')
        capture(sh, 100, 100, 260, 200)
        shot(sh, f'theme-{scheme}.png')
    dim = sh.js('''return zt.findAll(zt.inst()._indicator.menu.box,
            a => a.style_class === 'zehntage-answer')
        .map(a => a.get_theme_node().get_foreground_color().alpha);''')
    check(dim and all(a == 255 for a in dim), f'answer text dimmed: {dim}')


# ----- speed: streaming, thinking, warm-up


def t_thinking_switch_on(sh):
    sh.gset('thinking', 'true')
    capture(sh, 100, 100, 200, 200)
    b = Mock.requests[0]['body']
    check('thinking' not in b, f'thinking on = adaptive default: {b}')


def t_warmup_connection_on_hotkey(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    wait_for(lambda: Mock.gets, 2, 'no warm-up request while selecting')
    check(Mock.gets == ['/v1/models/claude-haiku-5-5'], Mock.gets)
    check(not Mock.requests, 'warm-up must not create a message')
    sh.js('await zt.drag(100, 100, 200, 200);')
    wait_state(sh, lambda s: s['status'] == 'ok')
    # The point of warming up: the request reuses that connection.
    check(Mock.requests[0]['port'] == Mock.get_ports[0],
          f'request on a new connection: {Mock.requests[0]["port"]} vs '
          f'warm-up {Mock.get_ports[0]}')
    check(Mock.gets == ['/v1/models/claude-haiku-5-5'],
          f'capabilities fetched again: {Mock.gets}')


def t_failed_metadata_lookup_is_not_repeated(sh):
    Mock.get_status = 404
    capture(sh, 100, 100, 200, 200)
    capture(sh, 100, 100, 220, 200)
    # One warm-up per capture, but send() must not wait for another lookup.
    check(len(Mock.gets) == 2, f'metadata fetched per send: {Mock.gets}')
    check(Mock.requests[1]['body'].get('thinking') == {'type': 'disabled'},
          'unknown capabilities count as "thinking can be off"')


def t_no_warmup_for_cli_backend(sh):
    use_cli(sh)
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    check(sh.js('return zt.inst()._claude._capsPending.size;') == 0 and
          not Mock.gets, f'cli backend must not touch the API: {Mock.gets}')
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_streaming_shows_partial_answer(sh):
    Mock.answer = lambda n, b: 'ERSTER TEIL zweiter teil DRITTER TEIL'
    Mock.chunks = 3
    Mock.chunk_delay = 0.8
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    text = wait_for(lambda: (lambda t: t if 'ERSTER' in t else None)(
        menu_text(sh)), 10, 'no partial answer')   # 4K capture ~3 s
    check('ERSTER' in text and 'DRITTER' not in text,
          f'no partial answer while streaming: {text!r}')
    check(state(sh)['status'] == 'pending', 'should still be pending')
    shot(sh, 'streaming.png')
    s = wait_state(sh, lambda s: s['status'] == 'ok', timeout=8)
    check(s['turns'][0]['answer'] == 'ERSTER TEIL zweiter teil DRITTER TEIL',
          s['turns'])
    check('DRITTER' in menu_text(sh), 'final answer not shown')


def t_streaming_followup_partial(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.answer = lambda n, b: 'FOLGE eins FOLGE zwei'
    Mock.chunks = 2
    Mock.chunk_delay = 0.8
    sh.js("await zt.type('und?'); await zt.chord(zt.Clutter.KEY_Return);")
    text = wait_for(lambda: (lambda t: t if 'FOLGE eins' in t else None)(
        menu_text(sh)), 5, 'no partial follow-up')
    check('FOLGE eins' in text and 'FOLGE zwei' not in text, text)
    wait_state(sh, lambda s: len(s['turns'] or []) == 2, timeout=8)


def t_stream_error_event_is_shown(sh):
    Mock.stream_error = True
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'error' and 'Overloaded' in s['error'], s)


def t_large_capture_sent_as_jpeg(sh):
    """A detailed full screen is >300 KB as PNG: must go out as JPEG."""
    noise_background(sh, 'noise2.png')
    sw, shh = stage_size(sh)
    s = capture(sh, 100, 100, 700, 500)
    check(s['status'] == 'ok', s)
    src = last_image()['source']
    check(src['media_type'] == 'image/jpeg', src['media_type'])
    check(base64.b64decode(src['data'])[:2] == b'\xff\xd8', 'not JPEG bytes')


def t_defaults_are_api_stream_no_thinking(sh):
    for key in ('backend', 'stream', 'thinking', 'effort'):
        sh.greset(key)
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'ok', s)
    check(len(Mock.requests) == 1 and not fake_calls(), 'default backend '
          'must be the API')
    b = Mock.requests[0]['body']
    check(b['stream'] is True and b.get('thinking') == {'type': 'disabled'}
          and b['output_config'] == {'effort': 'low'}, b)


def t_stream_setting_off(sh):
    sh.gset('stream', 'false')
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'ok' and s['turns'][0]['answer'] == ANSWER, s)
    check(Mock.requests[0]['body']['stream'] is False, 'stream not off')
    sh.js("await zt.type('und?'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(Mock.requests[1]['body']['stream'] is False, 'follow-up streamed')


# ----- real app windows (Wayland and X11 clients)

APP = ROOT / 'test' / 'color-app.py'


class App:
    """A real GTK client window; closed on exit."""

    def __init__(self, sh, backend, *args):
        self.sh = sh
        env = dict(os.environ, GDK_BACKEND=backend, NO_AT_BRIDGE='1')
        if backend == 'x11':
            env['DISPLAY'] = sh.js("return zt.getenv('DISPLAY');")
            # The test shell's Xwayland has its own auth cookie.
            env['XAUTHORITY'] = sh.js("return zt.getenv('XAUTHORITY');")
            env.pop('WAYLAND_DISPLAY', None)
        self.log = Path(os.environ['ZT_TMP']) / 'app.log'
        self.proc = subprocess.Popen([sys.executable, str(APP), *args],
                                     env=env, stdout=subprocess.DEVNULL,
                                     stderr=open(self.log, 'w'))
        self.display = env.get('DISPLAY')

    def __enter__(self):
        try:
            self._wait_window()
        except RuntimeError as e:
            tail = self.log.read_text(errors='replace')[-400:]
            raise RuntimeError(f'app window never appeared (DISPLAY='
                               f'{self.display}): {tail or e}') from None
        time.sleep(1.0)   # first frames painted, any fullscreen settled
        self.rect = self.sh.js("""
            const w = global.display.list_all_windows()
                .find(w => w.get_title() === 'zt-color');
            const r = w.get_frame_rect();
            return [r.x, r.y, r.width, r.height];""")
        return self

    def _wait_window(self):
        self.sh.js("""
            return await zt.waitFor(() => {
                const w = global.display.list_all_windows()
                    .find(w => w.get_title() === 'zt-color');
                if (!w) return null;
                const r = w.get_frame_rect();
                return r.width > 50 ? [r.x, r.y, r.width, r.height] : null;
            }, 15000);""", timeout=20)

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.sh.js("""await zt.waitFor(() => !global.display.list_all_windows()
            .some(w => w.get_title() === 'zt-color'), 5000)
            .catch(() => {});""")


def _check_inset(sh, rect, pred, what, inset=20):
    x, y, w, h = rect
    capture(sh, x + inset, y + inset, x + w - inset, y + h - inset)
    iw, ih, pix = decode_png(last_image()['source']['data'])
    for px_, py_ in ((1, 1), (iw - 2, 1), (1, ih - 2), (iw - 2, ih - 2),
                     (iw // 2, ih // 2)):
        check(pred(pix(px_, py_)),
              f'{what}: pixel {px_},{py_} = {pix(px_, py_)}')


def t_capture_wayland_app_window(sh):
    with App(sh, 'wayland') as app:
        _check_inset(sh, app.rect, magenta, 'wayland window')
        shot(sh, 'app-wayland.png')


def t_capture_x11_app_window(sh):
    with App(sh, 'x11') as app:
        _check_inset(sh, app.rect, magenta, 'X11 (Xwayland) window')


def t_capture_fullscreen_app(sh):
    with App(sh, 'wayland', '--fullscreen') as app:
        mon = sh.js('return (m => [m.x, m.y, m.width, m.height])'
                    '(zt.Main.layoutManager.primaryMonitor);')
        check(app.rect == mon, f'not fullscreen: {app.rect} vs {mon}')
        _check_inset(sh, app.rect, magenta, 'fullscreen window', inset=60)


def t_capture_open_app_menu(sh):
    """Dropdown open in an app: must be in the frozen frame."""
    with App(sh, 'wayland', '--menu') as app:
        x, y, _, _ = app.rect
        sh.js(f'await zt.click({x + 20}, {y + 15});')
        menu = sh.js("""
            return await zt.waitFor(() => {
                const w = global.display.list_all_windows()
                    .find(w => w.get_title() !== 'zt-color' &&
                          w.get_frame_rect().width > 50 &&
                          w.get_window_type() !== 0);
                if (!w) return null;
                const r = w.get_frame_rect();
                return [r.x, r.y, r.width, r.height];
            }, 5000);""")
        time.sleep(0.5)
        shot(sh, 'app-menu-open.png')
        _check_inset(sh, menu, cyan, 'open dropdown menu', inset=10)
        s = state(sh)
        check(s['status'] == 'ok', s)


def t_opus_button_rewrites_answer(sh):
    capture(sh, 100, 100, 200, 200)
    label = sh.js('''return zt.findAll(zt.inst()._indicator.menu.box,
            a => a.style_class?.includes('zehntage-strong'))[0]?.label;''')
    check(label == 'Opus 5.5', f'button label: {label!r}')
    press_strong(sh)
    s = wait_state(sh, lambda s: s['turns'] and
                   s['turns'][0].get('model'), timeout=8)
    b = Mock.requests[1]['body']
    check(b['model'] == 'claude-opus-5-5', b['model'])
    check(b['output_config'] == {'effort': 'high'}, b['output_config'])
    check('thinking' not in b, 'Opus 5.5: thinking must stay adaptive')
    check(b['max_tokens'] >= 16000, b['max_tokens'])
    check(len(b['messages']) == 1 and last_image(1), b['messages'])
    check(len(s['turns']) == 1 and s['turns'][0]['answer'] == '**Opus** 2',
          s['turns'])
    text = menu_text(sh)
    check('Opus 2' in text and 'белка' not in text, 'Haiku answer not replaced')
    check('— Opus 5.5' in text, 'no model tag')
    shot(sh, 'opus-rewrite.png')
    # Follow-up goes back to Haiku; Opus' blocks replayed as text only.
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('und?'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    b = Mock.requests[2]['body']
    check(b['model'] == 'claude-haiku-5-5', b['model'])
    check(b['messages'][1]['content'] == [{'type': 'text',
                                           'text': '**Opus** 2'}],
          b['messages'][1])


def t_opus_rewrites_last_followup(sh):
    capture(sh, 100, 100, 200, 200)
    sh.js("await zt.type('Plural?'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    press_strong(sh)
    s = wait_state(sh, lambda s: s['turns'][-1].get('model'), timeout=8)
    msgs = Mock.requests[2]['body']['messages']
    check([m['role'] for m in msgs] == ['user', 'assistant', 'user'] and
          msgs[2]['content'] == 'Plural?' + SUFFIX, msgs)
    check(s['turns'][0]['answer'] == ANSWER and
          s['turns'][1] == {'question': 'Plural?', 'answer': '**Opus** 3',
                            'content': s['turns'][1]['content'],
                            'model': 'claude-opus-5-5'}, s['turns'])


def t_opus_after_haiku_error(sh):
    Mock.status = lambda n: 401 if n == 1 else 200
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'error', s)
    press_strong(sh)
    s = wait_state(sh, lambda s: s['status'] == 'ok', timeout=8)
    check(Mock.requests[1]['body']['model'] == 'claude-opus-5-5', 'model')
    check(s['turns'][0]['model'] == 'claude-opus-5-5', s['turns'])


def t_opus_failure_keeps_haiku_answer(sh):
    Mock.status = lambda n: 529 if n == 2 else 200
    capture(sh, 100, 100, 200, 200)
    press_strong(sh)
    s = wait_state(sh, lambda s: s['strongError'], timeout=6)
    err = s['strongError']
    check('529' in err, f'wrong strong-model error: {err}')
    s = state(sh)
    check(s['turns'][0]['answer'] == ANSWER and
          'model' not in s['turns'][0], 'Haiku answer lost')
    check('529' in menu_text(sh), 'upgrade error not shown')


def t_opus_streams_live(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.answer = lambda n, b: 'OPUSTEIL eins OPUSTEIL zwei'
    Mock.chunks = 2
    Mock.chunk_delay = 0.8
    press_strong(sh)
    text = wait_for(lambda: (lambda t: t if 'OPUSTEIL eins' in t else None)(
        menu_text(sh)), 5, 'no partial strong answer')
    check('OPUSTEIL eins' in text and 'OPUSTEIL zwei' not in text and
          'белка' not in text, text)
    wait_state(sh, lambda s: s['turns'][0].get('model'), timeout=8)


def t_opus_blocks_followup_while_running(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.delay = 1.5
    press_strong(sh)
    wait_state(sh, lambda s: s['busy'] == 'strong')
    followup(sh, 'warte')
    wait_state(sh, lambda s: s['turns'][0].get('model'), timeout=8)
    Mock.wait_idle()
    time.sleep(0.3)   # grace: a queued follow-up would show up now
    check(len(Mock.requests) == 2, 'follow-up sent during the Opus rewrite')


def t_opus_with_cli_backend(sh):
    use_cli(sh)
    capture(sh, 100, 100, 200, 200)
    press_strong(sh)
    wait_state(sh, lambda s: s['turns'] and
                   s['turns'][0].get('model'), timeout=8)
    argv = fake_calls()[1]['argv']
    check(argval(argv, '--model') == 'claude-opus-5-5' and
          argval(argv, '--effort') == 'high', argv)
    check(not Mock.requests, 'cli entry must not use the API')


def t_model_name_labels(sh):
    names = sh.js("""return import('file://' + zt.ext().path + '/indicator.js')
        .then(m => ['claude-opus-5-5', 'claude-haiku-5-5', 'claude-fable-5-1',
                    'claude-sonnet-5', 'weird'].map(m.modelName));""")
    check(names == ['Opus 5.5', 'Haiku 5.5', 'Fable 5.1', 'Sonnet 5',
                    'weird'], names)


def t_cli_streams_partial_answer(sh):
    use_cli(sh, 'slowstream')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    text = wait_for(lambda: (lambda t: t if 'ANFANG' in t else None)(
        menu_text(sh)), 6, 'no partial CLI answer')
    check('ANFANG' in text and 'ENDE' not in text,
          f'no partial CLI answer: {text!r}')
    s = wait_state(sh, lambda s: s['status'] == 'ok', timeout=10)
    check(s['turns'][0]['answer'].endswith('ENDE'), s['turns'])


# ----- strong model and error state

def t_opus_answers_failed_followup(sh):
    Mock.status = lambda n: 529 if n == 2 else 200
    capture(sh, 100, 100, 200, 200)
    followup(sh, 'q-failed')
    wait_state(sh, lambda s: s['followUpError'])
    press_strong(sh)
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2 and
                   not s['busy'], timeout=8)
    msgs = Mock.requests[2]['body']['messages']
    check(Mock.requests[2]['body']['model'] == 'claude-opus-5-5', 'model')
    check([m['role'] for m in msgs] == ['user', 'assistant', 'user'] and
          msgs[2]['content'] == 'q-failed' + SUFFIX,
          f'wrong question asked: {msgs}')
    check(s['turns'][0]['answer'] == ANSWER, 'first answer must stay')
    check(s['turns'][1]['question'] == 'q-failed' and
          s['turns'][1]['model'] == 'claude-opus-5-5', s['turns'][1])
    check(not s['followUpError'], 'stale follow-up error')


def t_errors_cleared_by_next_success(sh):
    Mock.status = lambda n: 529 if n == 2 else 200
    capture(sh, 100, 100, 200, 200)
    press_strong(sh)
    wait_state(sh, lambda s: s['strongError'])
    followup(sh, 'weiter')
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(not s['strongError'], f'stale strong error: {s["strongError"]}')
    check('529' not in menu_text(sh), 'old error still on screen')


def t_main_model_that_cannot_disable_thinking(sh):
    sh.gset('model', "'claude-opus-5-5'")
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'ok', s)
    b = Mock.requests[0]['body']
    check('thinking' not in b, f'Opus 5.5 rejects thinking disabled: {b}')


def t_history_json_has_no_transient_state(sh):
    Mock.chunks = 3
    Mock.chunk_delay = 0.5
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 200, 200);')
    wait_state(sh, lambda s: s['partial'])
    sh.js('zt.inst()._history.save();')   # a save while streaming
    path = data_dir() / 'history.json'
    keys = set().union(*(e.keys() for e in json.loads(path.read_text())))
    allowed = {'id', 'time', 'imagePath', 'mediaType', 'contextPath',
               'contextMediaType', 'rect', 'backend', 'model', 'system',
               'suffix', 'turns', 'status', 'error', 'thumbPath',
               'followUpError', 'failedQuestion', 'strongError'}
    check(keys <= allowed, f'unexpected saved keys: {sorted(keys - allowed)}')
    wait_state(sh, lambda s: s['status'] == 'ok', timeout=8)


def t_utf8_intact_without_streaming(sh):
    # Only 3-byte characters, ~180 KB: libsoup hands the body over in
    # several chunks, and a chunk border inside a character is likely
    # (a per-chunk decoder slipped through this test with '€uro ' text).
    sh.gset('stream', 'false')
    answer = '€' * 60000
    Mock.answer = lambda n, b: answer
    for i in range(2):
        s = capture(sh, 100 + i, 100, 200, 200)
        check(s['status'] == 'ok', s)
        got = s['turns'][0]['answer']
        check(got == answer, f'answer corrupted ({got.count(chr(0xfffd))} '
              f'replacement chars, {len(got)} vs {len(answer)} chars)')


# ----- whole screen as context, selection outlined in red

def t_context_image_outlines_selection(sh):
    # Select 4 px inside the magenta box (fractional scales blur the edge
    # pixel), so the focus image must be magenta to the corners.
    bx, by, bw, bh = 400, 300, 160, 70
    sh.js(f'zt.box({bx - 4}, {by - 4}, {bw + 8}, {bh + 8});')
    capture(sh, bx, by, bx + bw, by + bh)
    content = first_content()
    check([b['type'] for b in content] == ['text', 'image', 'text', 'image'],
          [b['type'] for b in content])
    ctx = context_image()
    cw, ch, pix = decode_png(ctx['source']['data'])
    check((cw, ch) == monitor_px(sh, bx, by), f'context {cw}x{ch}')
    import re
    m = re.search(r'x=(\d+), y=(\d+), width=(\d+), height=(\d+)',
                  content[0]['text'])
    check(m, f'no coordinates in: {content[0]["text"]!r}')
    x, y, w, h = map(int, m.groups())
    # Inside: the magenta box (content not covered); just outside: red.
    check(magenta(pix(x + w // 2, y + h // 2)), 'rect does not match box')
    check(magenta(pix(x + 2, y + 2)), 'frame covers the selection')
    for px_, py_ in ((x + w // 2, y - 3), (x + w // 2, y + h + 2),
                     (x - 3, y + h // 2), (x + w + 2, y + h // 2)):
        check(red(pix(px_, py_)), f'no red frame at {px_},{py_}: '
              f'{pix(px_, py_)}')
    fw, fh, fpix = decode_png(last_image()['source']['data'])
    check((fw, fh) == (span(sh, bx, bx + bw), span(sh, by, by + bh)) and
          magenta(fpix(0, 0)) and magenta(fpix(fw - 1, fh - 1)),
          f'focus {fw}x{fh} vs {(span(sh, bx, bx + bw), span(sh, by, by + bh))}, '
          f'corners {fpix(0, 0)} {fpix(fw - 1, fh - 1)}')
    check('red rectangle' in Mock.requests[0]['body']['system'],
          'system prompt does not explain the rectangle')
    shot(sh, 'context.png')
    (OUT / 'sent-context.jpg').write_bytes(
        base64.b64decode(ctx['source']['data']))   # what Claude gets
    # Thumbnails/history keep both images; follow-ups resend both.
    followup(sh, 'und?')
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(first_content(1) == content, 'follow-up changed the screenshot')


def t_context_on_the_selections_monitor(sh):
    mons = sh.js('return zt.Main.layoutManager.monitors.map('
                 'm => [m.x, m.y, m.width, m.height]);')
    mx, my, mw, mh = mons[-1]
    capture(sh, mx + 50, my + 100, mx + 250, my + 180)
    cw, ch = png_size(context_image()['source']['data'])
    check((cw, ch) == monitor_px(sh, mx + 60, my + 110),
          f'context {cw}x{ch} is not that monitor')


def t_context_setting_off(sh):
    sh.gset('context', 'false')
    capture(sh, 100, 100, 300, 200)
    content = first_content()
    check(len(content) == 1 and content[0]['type'] == 'image',
          'context off: only the selection')
    check(png_size(content[0]['source']['data']) ==
          (span(sh, 100, 300), span(sh, 100, 200)), 'selection size')


def t_context_image_saved_with_entry(sh):
    capture(sh, 100, 100, 300, 200)
    e = sh.js('const e = zt.inst()._history.entries[0]; '
              'return [e.contextPath, e.rect, e.contextMediaType];')
    check(e[0] and Path(e[0]).exists() and len(e[1]) == 4 and
          e[2] == 'image/jpeg', f'context not stored: {e}')


def t_one_word_selection(sh):
    """Select one word of a sentence: the focus image must show that word
    (white text on the fixture's dark background), not the wallpaper."""
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
    capture(sh, wx - 2, wy, wx + ww + 2, wy + wh)
    (OUT / 'one-word-focus.png').write_bytes(
        base64.b64decode(last_image()['source']['data']))
    (OUT / 'one-word-context.jpg').write_bytes(
        base64.b64decode(context_image()['source']['data']))
    w, h, pix = decode_png(last_image()['source']['data'])
    pixels = [pix(x, y) for x in range(0, w, 3) for y in range(0, h, 3)]
    white = sum(min(p) > 200 for p in pixels)
    dark = sum(max(p) < 60 for p in pixels)
    check(white > 10 and dark > len(pixels) // 3,
          f'focus is not the word: {white} white, {dark} dark of '
          f'{len(pixels)} at {(wx, wy, ww, wh)}')


def t_followup_suffix_pinned_and_legacy_free(sh):
    capture(sh, 100, 100, 200, 200)
    sh.gset('followup-suffix', "'(answer in English)'")   # after capture
    followup(sh, 'Wie ist der Plural?')
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    sent = Mock.requests[1]['body']['messages'][-1]['content']
    check(sent == 'Wie ist der Plural?' + SUFFIX,
          f'suffix must be pinned at capture time: {sent!r}')
    turns = state(sh)['turns']
    check(turns[1]['question'] == 'Wie ist der Plural?',
          'the suffix must not be stored or shown')


def t_rerender_keeps_scroll_position(sh):
    Mock.answer = lambda n, body: '\n'.join(f'Zeile {i}' for i in range(40))
    capture(sh, 100, 100, 300, 200)
    capture(sh, 100, 100, 320, 200)
    probe = '''const v = zt.findAll(zt.inst()._indicator.menu.box,
        a => a instanceof zt.St.ScrollView)[0].vadjustment;'''
    upper = sh.js(probe + 'return v.upper - v.page_size;')
    check(upper > 200, f'popup does not scroll: {upper}')
    sh.js(probe + 'v.value = 150; await zt.sleep(100);')
    Mock.chunk_delay = 0.3                 # re-render per finished answer
    followup(sh, 'und Plural?', focus=False)
    wait_state(sh, lambda s: len(s['turns']) == 2 and not s['busy'])
    sh.js('await zt.sleep(200);')
    check(sh.js(probe + 'return v.value;') == 150, 'scroll position lost')
    sh.js('zt.inst()._indicator.menu.close(0); zt.inst()._indicator.open(); '
          'await zt.sleep(200);')
    check(sh.js(probe + 'return v.value;') == 0, 'reopen must start at top')


# ----- Cancel

def t_cancel_pending_answer(sh):
    Mock.delay = 3.0
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['busy'] == 'initial')
    t = time.monotonic()
    press_cancel(sh)
    s = wait_state(sh, lambda s: not s['busy'], timeout=2)
    check(time.monotonic() - t < 1, 'cancel did not abort the request')
    check(s['status'] == 'error' and s['error'] == 'Cancelled', s)
    check('Retry' in sh.js('''return zt.findAll(zt.inst()._indicator.menu.box,
        a => a instanceof zt.St.Button).map(b => b.label);'''), 'no Retry')
    Mock.delay = 0
    sh.js('zt.inst()._askInitial(zt.inst()._history.entries[0]);')
    s = wait_state(sh, lambda s: s['status'] == 'ok' and not s['busy'])
    check(s['turns'][0]['answer'], s)


def t_cancel_followup_leaves_no_trace(sh):
    capture(sh, 100, 100, 300, 200)
    Mock.delay = 3.0
    followup(sh, 'und Plural?')
    wait_state(sh, lambda s: s['busy'] == 'followUp')
    press_cancel(sh)
    s = wait_state(sh, lambda s: not s['busy'], timeout=2)
    check(s['status'] == 'ok' and len(s['turns']) == 1 and
          not s['followUpError'], s)
    check(sh.js('return zt.inst()._history.entries[0].failedQuestion '
                '?? null;') is None, '[Opus] would answer a cancelled question')


def t_cancel_kills_cli(sh):
    use_cli(sh, 'hang')
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_for(lambda: (Path(os.environ['ZT_FAKE_DIR']) / 'hang.pid')
             .exists(), 10, 'claude not started')
    wait_state(sh, lambda s: s['busy'] == 'initial')
    pid = int((Path(os.environ['ZT_FAKE_DIR']) / 'hang.pid').read_text())
    press_cancel(sh)
    s = wait_state(sh, lambda s: not s['busy'], timeout=3)
    check(s['error'] == 'Cancelled', s)
    wait_for(lambda: not Path(f'/proc/{pid}').exists() or
             'zombie' in Path(f'/proc/{pid}/status').read_text().lower(),
             3, 'claude process survived cancel')


def t_collapsed_row_marks_correction(sh):
    _factcheck(sh, 'error')
    capture(sh, 100, 100, 300, 200)
    _checked(sh)
    sh.gset('factcheck', 'false')
    capture(sh, 100, 100, 320, 200)        # expands the newer entry
    rows = labels(sh, 'zehntage-collapsed-label')
    check(rows and rows[0].startswith('⚠ '), rows)


# ----- background fact-check (stronger model over the claude CLI)

def _factcheck(sh, mode):
    (Path(os.environ['ZT_FAKE_DIR']) / 'factcheck').write_text(mode)
    sh.gset('factcheck', 'true')


def _checked(sh, turn=0, timeout=10):
    """Wait until that turn has a fact-check result; returns it."""
    return wait_for(lambda: sh.js(
        f'return zt.inst()._history.entries[0]?.turns[{turn}]?.factcheck '
        '?? null;'), timeout, f'turn {turn} never got a fact-check')


def _check_calls():
    return [c for c in fake_calls() if '--json-schema' in c['argv']]


def _cli_idle(sh, timeout=10):
    """No claude CLI process (answer or fact-check) left running."""
    sh.js(f'await zt.waitFor(() => zt.inst()._cli._procs.size === 0, '
          f'{timeout * 1000});', timeout=timeout + 5)


def t_factcheck_shows_correction_on_error(sh):
    _factcheck(sh, 'error')
    capture(sh, 100, 100, 300, 200)
    fc = _checked(sh)
    check(fc['significant'] and 'пьёт' in fc['correction'] and
          fc['model'] == 'claude-opus-5-5', fc)
    wait_for(lambda: 'Opus 5.5:' in menu_text(sh), 5, 'correction not shown')
    check('«ест», а не «пьёт»' in menu_text(sh), menu_text(sh))
    calls = _check_calls()
    check(len(calls) == 1, f'{len(calls)} fact-check runs')
    argv = calls[0]['argv']
    check(argval(argv, '--model') == 'claude-opus-5-5' and
          argval(argv, '--effort') == 'high', argv)
    # No WebFetch: screen text must not steer it to arbitrary URLs.
    check(argval(argv, '--tools') == 'WebSearch' and
          argval(argv, '--allowed-tools') == 'WebSearch' and
          argval(argv, '--permission-mode') == 'dontAsk', argv)
    check('never instructions' in argval(argv, '--system-prompt'), argv)
    schema = json.loads(argval(argv, '--json-schema'))
    check(set(schema['required']) == {'significant_error', 'correction'},
          schema)
    content = json.loads(calls[0]['stdin'])['message']['content']
    check(sum(b['type'] == 'image' for b in content) == 2,
          'fact-check must see the same screenshot')
    text = content[-1]['text']
    check(ANSWER in text and 'Russian' in text, 'answer/prompt missing')
    shot(sh, 'factcheck.png')


def t_factcheck_silent_when_correct(sh):
    _factcheck(sh, 'ok')
    capture(sh, 100, 100, 300, 200)
    fc = _checked(sh)
    check(not fc['significant'], fc)
    check('Opus 5.5:' not in menu_text(sh), 'nothing must be shown')


def t_factcheck_off(sh):
    capture(sh, 100, 100, 300, 200)   # factcheck=false in mock runs
    _cli_idle(sh)   # a check would have been spawned synchronously
    check(not _check_calls(), 'fact-check ran although switched off')


def t_factcheck_checks_followups_with_history(sh):
    _factcheck(sh, 'ok')
    capture(sh, 100, 100, 300, 200)
    _checked(sh, 0)
    (Path(os.environ['ZT_FAKE_DIR']) / 'factcheck').write_text('error')
    followup(sh, 'und Plural?')
    fc = _checked(sh, 1)
    check(fc['significant'], fc)
    text = json.loads(_check_calls()[-1]['stdin'])['message']['content'][-1]['text']
    check(f'Assistant: {ANSWER}' in text and 'User: und Plural?' in text and
          'Antwort 2' in text, f'history missing: {text!r}')
    wait_for(lambda: 'Opus 5.5:' in menu_text(sh), 5, 'not shown')


def t_factcheck_skips_strong_and_cli_answers(sh):
    _factcheck(sh, 'error')
    capture(sh, 100, 100, 300, 200)
    _checked(sh)
    press_strong(sh)                      # Opus answer: no check
    wait_state(sh, lambda s: s['turns'][0].get('model'), timeout=8)
    _cli_idle(sh)
    check(len(_check_calls()) == 1, 'strong answer was fact-checked')
    use_cli(sh)                           # CLI entry: no check
    capture(sh, 100, 100, 320, 200)
    _cli_idle(sh)
    check(len(_check_calls()) == 1, 'CLI answer was fact-checked')


def t_factcheck_dropped_when_answer_replaced(sh):
    _factcheck(sh, 'slow')                # 3 s
    capture(sh, 100, 100, 300, 200)
    press_strong(sh)                      # replaces turn 0 meanwhile
    wait_state(sh, lambda s: s['turns'][0].get('model'), timeout=8)
    _cli_idle(sh)                         # the slow check has finished
    turn = state(sh)['turns'][0]
    check('factcheck' not in turn, f'stale fact-check applied: {turn}')


def t_factcheck_failure_is_silent(sh):
    _factcheck(sh, 'crash')
    s = capture(sh, 100, 100, 300, 200)
    _cli_idle(sh)
    check(s['status'] == 'ok' and 'factcheck' not in state(sh)['turns'][0],
          'a failed check must change nothing')
    check('Opus 5.5:' not in menu_text(sh), 'nothing must be shown')


def t_factcheck_pauses_on_usage_limit(sh):
    _factcheck(sh, 'limit')
    capture(sh, 100, 100, 300, 200)
    wait_for(lambda: sh.js('return zt.inst()._checkPausedUntil > 0;'), 10,
             'usage limit did not pause fact-checks')
    check(any('fact-check paused' in t for t in sh.js(
        'return zt.Main.messageTray.getSources().flatMap(s => '
        's.notifications.map(n => n.title));')), 'user was not told')
    _cli_idle(sh)
    capture(sh, 100, 100, 320, 200)
    _cli_idle(sh)
    check(len(_check_calls()) == 1, 'checked again while paused')


def t_factcheck_one_at_a_time(sh):
    _factcheck(sh, 'slow 20')             # outlasts the 2nd capture on 4K
    capture(sh, 100, 100, 300, 200)
    wait_for(lambda: len(_check_calls()) == 1, 5, 'first check not started')
    (Path(os.environ['ZT_FAKE_DIR']) / 'factcheck').write_text('error')
    capture(sh, 100, 100, 320, 200)
    _checked(sh)                          # the newer one finishes
    _cli_idle(sh)
    check(sh.js('return zt.inst()._cli._procs.size;') == 0, 'leaked proc')
    older = sh.js('return zt.inst()._history.entries[1].turns[0];')
    check('factcheck' not in older, f'superseded check applied: {older}')


def t_factcheck_skipped_when_images_gone(sh):
    capture(sh, 100, 100, 300, 200)
    _factcheck(sh, 'ok')
    Path(entries(sh)[0]['imagePath']).unlink()
    sh.js('const i = zt.inst(), e = i._history.entries[0]; '
          'i._factCheck(e, e.turns[0]);')   # must not throw
    check(not _check_calls(), 'checked without images')


def t_factcheck_survives_reload(sh):
    _factcheck(sh, 'error')
    capture(sh, 100, 100, 300, 200)
    _checked(sh)
    reload_extension(sh)
    sh.js('zt.inst()._indicator.menu.open(0); await zt.sleep(200);')
    check('«ест», а не «пьёт»' in menu_text(sh), 'correction lost on reload')


def t_cli_large_stdin_arrives_intact(sh):
    """>1 MB of images through the non-blocking stdin pipe, byte-exact."""
    noise_background(sh, 'noise3.png')
    use_cli(sh)
    s = capture(sh, 100, 100, 700, 500)
    check(s['status'] == 'ok', s)
    stdin = fake_calls()[0]['stdin']
    check(len(stdin) > 1_000_000, f'only {len(stdin)} bytes: test too small')
    images = [b for b in json.loads(stdin)['message']['content']
              if b['type'] == 'image']
    e = sh.js('const e = zt.inst()._history.entries[0]; '
              'return [e.contextPath, e.imagePath];')
    for block, path in zip(images, e):
        check(base64.b64decode(block['source']['data']) ==
              Path(path).read_bytes(), f'{path}: bytes differ')


def _rss_mb(sh):
    """gnome-shell resident memory after a forced GC."""
    sh.js('imports.system.gc(); await zt.sleep(300); imports.system.gc();')
    status = Path(f'/proc/{sh.proc.pid}/status').read_text()
    return int(re.search(r'VmRSS:\s+(\d+)', status).group(1)) / 1024


def t_memory_does_not_grow_with_captures(sh):
    """Full-res monitor captures are big: none may stay alive. With a
    history cap of 5, memory must plateau instead of growing per capture."""
    sh.gset('history-size', '5')
    for i in range(6):                       # warm-up: fill caches/history
        capture(sh, 100 + i, 100, 400, 300)
    sizes = [_rss_mb(sh)]
    for _ in range(4):
        for i in range(10):
            capture(sh, 100 + i, 100, 400, 300)
        sizes.append(_rss_mb(sh))
    print('    RSS per 10 captures: ' + ' -> '.join(f'{v:.0f}' for v in sizes)
          + ' MB')
    check(sizes[-1] - sizes[1] < 40,
          f'still growing after the caches filled: {sizes}')


SCENARIOS = [v for k, v in list(globals().items()) if k.startswith('t_')]

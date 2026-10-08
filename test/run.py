#!/usr/bin/env python3
"""Autonomous end-to-end tests for zehntage-gnome.

Starts a throwaway headless GNOME Shell on a private D-Bus session with
isolated XDG dirs (your real settings/history are never touched), installs
the extension plus a test helper, and drives it via org.gnome.Shell.Eval with
virtual pointer/keyboard devices. The Claude API is a local mock by default.

    ./test/run.py                 # all mock scenarios
    ./test/run.py drag followup   # scenarios whose name contains a filter
    ./test/run.py --live          # one real request (needs ANTHROPIC_API_KEY)

Screenshots of each scenario land in test/out/.
"""

import base64
import math
import re
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'test' / 'out'
UUID = 'zehntage-gnome@lyka'
HELPER_UUID = 'zehntage-test-helper@lyka'
SCHEMA = 'org.gnome.shell.extensions.zehntage-gnome'
EXT_FILES = ['extension.js', 'claude.js', 'cli.js', 'selector.js', 'history.js',
             'indicator.js', 'prefs.js', 'metadata.json', 'stylesheet.css']
SCREEN = (1280, 800)
# --screens "1920x1080@1.25,1920x1080@1": monitors left to right, with scale.
SCREENS = [(1280, 800, 1.0)]

REAL_DCONF = Path.home() / '.config' / 'dconf' / 'user'


# Keys this harness sets in its own (keyfile) settings. If one of these ever
# changes in the real dconf during a run, isolation is broken.
HARNESS_KEYS = ('/org/gnome/shell/extensions/zehntage-gnome/',
                '/org/gnome/shell/enabled-extensions',
                '/org/gnome/shell/welcome-dialog-last-shown-version',
                '/org/gnome/desktop/interface/enable-animations',
                '/org/gnome/desktop/interface/enable-hot-corners')


def real_dconf_dump(tmp):
    """Read-only {path: value} of the real user db (no dconf-service)."""
    if not REAL_DCONF.exists():
        return {}
    profile = tmp / 'real-dconf.profile'
    profile.write_text(f'file-db:{REAL_DCONF}\n')
    env = dict(os.environ, DCONF_PROFILE=str(profile))
    out = subprocess.run(['dconf', 'dump', '/'], env=env, check=True,
                         capture_output=True, text=True).stdout
    keys, section = {}, ''
    for line in out.splitlines():
        if line.startswith('[') and line.endswith(']'):
            section = '/' + line[1:-1].strip('/') + '/'
            section = '/' if section == '//' else section
        elif '=' in line:
            k, v = line.split('=', 1)
            keys[section + k] = v
    return keys


# Re-exec on a private session bus so we never talk to the real shell.
# Isolation is set up *before* the exec so dbus-daemon and every service it
# activates inherit it, and GSettings uses an in-process keyfile backend, so
# no dconf-service can ever write to the real ~/.config/dconf/user.
if os.environ.get('ZT_IN_DBUS') != '1':
    tmp = Path(tempfile.mkdtemp(prefix='zt-'))
    for d in ('config', 'data', 'cache', 'state'):
        (tmp / d).mkdir()
    # Fixed socket name so bus-activated apps (the prefs dialog) find the
    # headless shell as their display.
    os.environ.update(ZT_IN_DBUS='1', ZT_TMP=str(tmp),
                      WAYLAND_DISPLAY=f'zt-wayland-{os.getpid()}',
                      GSETTINGS_BACKEND='keyfile',
                      XDG_CONFIG_HOME=str(tmp / 'config'),
                      XDG_DATA_HOME=str(tmp / 'data'),
                      XDG_CACHE_HOME=str(tmp / 'cache'),
                      XDG_STATE_HOME=str(tmp / 'state'))
    # Bus-activated services spam stderr on teardown; keep it out of sight.
    OUT.mkdir(exist_ok=True)
    bus_log = os.open(OUT / 'bus.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.dup2(bus_log, 2)
    os.execvp('dbus-run-session',
              ['dbus-run-session', '--', sys.executable, *sys.argv])

import gi  # noqa: E402

gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib  # noqa: E402

ANSWER = '**Eichhörnchen** — белка.\n\n- *das* Eichhörnchen, neutr.'


# --------------------------------------------------------------- mock API

class Mock:
    mode = 'ok'          # ok | 401 | refusal | slow
    requests: list = []
    answer: Callable | None = None   # callable(n, body) -> text
    status: Callable | None = None   # callable(n) -> http status
    delay = 0.0          # seconds before answering
    gets: list = []      # GET paths (connection warm-up)
    chunks = 3           # text deltas per answer when streaming
    chunk_delay = 0.0    # seconds between deltas
    stream_error = False # send an SSE error event mid-answer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 (silence logging)
        pass

    def do_GET(self):   # connection warm-up (model metadata)
        Mock.gets.append(self.path)
        self._send(200, {'id': self.path.rsplit('/', 1)[-1],
                         'type': 'model'})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Mock.requests.append({'path': self.path,
                              'headers': dict(self.headers), 'body': body})
        n = len(Mock.requests)
        mode = Mock.mode
        time.sleep(1.5 if mode == 'slow' else Mock.delay)
        status = Mock.status(n) if Mock.status else (
            401 if mode == '401' else 200)
        if status != 200:
            return self._send(status, {'type': 'error', 'error': {
                'type': 'authentication_error',
                'message': 'invalid x-api-key'}})
        if mode == 'refusal':
            return self._stream(body, [], 'refusal')
        if Mock.answer:
            text = Mock.answer(n, body)
        elif 'opus' in body['model']:
            text = f'**Opus** {n}'
        else:
            text = ANSWER if n == 1 or mode != 'ok' else f'Antwort {n}'
        blocks = []
        if body.get('thinking', {}).get('type') != 'disabled':
            blocks.append({'type': 'thinking', 'thinking': '',
                           'signature': f'sig{n}'})
        blocks.append({'type': 'text', 'text': text})
        if body.get('stream'):
            return self._stream(body, blocks, 'end_turn')
        self._send(200, {
            'id': f'msg_{n}', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'stop_reason': 'end_turn',
            'content': blocks,
            'usage': {'input_tokens': 10, 'output_tokens': 5}})

    def _stream(self, body, blocks, stop):
        """Real-API-shaped SSE; text arrives in Mock.chunks pieces."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()

        def ev(obj):
            self.wfile.write(f"event: {obj['type']}\ndata: "
                             f"{json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
        ev({'type': 'message_start', 'message': {
            'id': 'msg', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'content': [],
            'usage': {'input_tokens': 10, 'output_tokens': 1}}})
        ev({'type': 'ping'})
        for i, b in enumerate(blocks):
            if b['type'] == 'thinking':
                ev({'type': 'content_block_start', 'index': i,
                    'content_block': {'type': 'thinking', 'thinking': '',
                                      'signature': ''}})
                ev({'type': 'content_block_delta', 'index': i, 'delta': {
                    'type': 'signature_delta', 'signature': b['signature']}})
            else:
                ev({'type': 'content_block_start', 'index': i,
                    'content_block': {'type': 'text', 'text': ''}})
                text = b['text']
                k = max(1, -(-len(text) // Mock.chunks))
                for j in range(0, len(text), k):
                    if j and Mock.chunk_delay:
                        time.sleep(Mock.chunk_delay)
                    if Mock.stream_error and j:
                        ev({'type': 'error', 'error': {
                            'type': 'overloaded_error',
                            'message': 'Overloaded'}})
                        return
                    ev({'type': 'content_block_delta', 'index': i, 'delta': {
                        'type': 'text_delta', 'text': text[j:j + k]}})
            ev({'type': 'content_block_stop', 'index': i})
        ev({'type': 'message_delta', 'delta': {'stop_reason': stop},
            'usage': {'output_tokens': 5}})
        ev({'type': 'message_stop'})

    def _send(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# --------------------------------------------------------------- shell

class Shell:
    def __init__(self, tmp, settings):
        self.tmp = tmp
        self.settings = settings
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.proc = None

    def install(self):
        data = self.tmp / 'data'
        ext = data / 'gnome-shell' / 'extensions'
        dst = ext / UUID
        (dst / 'schemas').mkdir(parents=True)
        for f in EXT_FILES:
            shutil.copy(ROOT / f, dst / f)
        shutil.copy(ROOT / 'schemas' / f'{SCHEMA}.gschema.xml',
                    dst / 'schemas')
        subprocess.run(['glib-compile-schemas', dst / 'schemas'], check=True)
        shutil.copytree(ROOT / 'test' / 'helper', ext / HELPER_UUID)
        self.schemadir = dst / 'schemas'

    def gset(self, key, value, schema=SCHEMA):
        args = ['gsettings']
        if schema == SCHEMA:
            args += ['--schemadir', str(self.schemadir)]
        subprocess.run(args + ['set', schema, key, value], check=True)

    def start(self):
        self.gset('enabled-extensions', f"['{UUID}', '{HELPER_UUID}']",
                  'org.gnome.shell')
        self.gset('welcome-dialog-last-shown-version', "'999'",
                  'org.gnome.shell')
        self.gset('enable-animations', 'false', 'org.gnome.desktop.interface')
        # The virtual pointer starts at (0,0): the hot corner would open the
        # overview on the first pointer event.
        self.gset('enable-hot-corners', 'false', 'org.gnome.desktop.interface')
        for k, v in self.settings.items():
            self.gset(k, v)
        env = dict(os.environ)
        display = env.pop('WAYLAND_DISPLAY')
        env.pop('DISPLAY', None)
        self.log = open(self.tmp / 'shell.log', 'w')
        self.proc = subprocess.Popen(
            ['gnome-shell', '--headless', '--wayland',   # Xwayland on demand
             '--wayland-display', display,
             *[a for w, h, _ in SCREENS
               for a in ('--virtual-monitor', f'{w}x{h}')]],
            env=env, stdout=self.log, stderr=subprocess.STDOUT)
        end = time.time() + 40
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError('gnome-shell exited; see shell.log')
            try:
                if self.js('return typeof zt') == 'object':
                    break
            except (GLib.Error, RuntimeError):
                pass  # shell not on the bus yet / helper not enabled yet
            time.sleep(0.3)
        else:
            raise RuntimeError(f'shell did not come up: {self.ext_states()}')
        # Hide the overview and the test-only "unsafe mode" notification.
        if any(scale != 1 for *_, scale in SCREENS) or len(SCREENS) > 1:
            self.configure_monitors()
        # GNOME opens the overview once startup completes; wait for that,
        # then close it (hiding earlier races the startup animation).
        self.js('''
            const lm = zt.Main.layoutManager;
            await zt.waitFor(() => !lm._startingUp, 20000);
            await zt.sleep(300);
            zt.Main.overview.hide();
            await zt.waitFor(() => !zt.Main.overview.visible);
            zt.Main.messageTray.getSources().forEach(s => s.destroy());
            await zt.sleep(200);
        ''')

    def configure_monitors(self):
        """Apply SCREENS' scales side by side via Mutter's DisplayConfig."""
        def call(method, args, reply):
            return self.bus.call_sync(
                'org.gnome.Mutter.DisplayConfig',
                '/org/gnome/Mutter/DisplayConfig',
                'org.gnome.Mutter.DisplayConfig', method, args,
                GLib.VariantType(reply) if reply else None,
                Gio.DBusCallFlags.NONE, 10000, None)

        serial, monitors, _logical, _props = call(
            'GetCurrentState', None,
            '(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv})',
        ).unpack()
        monitors = sorted(monitors, key=lambda m: m[0][0])
        logical, x = [], 0
        for (w, h, scale), mon in zip(SCREENS, monitors):
            mode = next(m for m in mon[1] if m[1] == w and m[2] == h)
            supported = mode[5]
            match = [sc for sc in supported if abs(sc - scale) < 0.01]
            if not match:
                raise RuntimeError(f'scale {scale} unsupported: {supported}')
            logical.append((x, 0, match[0], 0, not logical,
                            [(mon[0][0], mode[0], {})]))
            x += round(w / match[0])
        call('ApplyMonitorsConfig', GLib.Variant(
            '(uua(iiduba(ssa{sv}))a{sv})', (serial, 1, logical, {})), None)
        time.sleep(1)
        geo = self.js('return zt.Main.layoutManager.monitors.map('
                      'm => [m.x, m.y, m.width, m.height, '
                      'm.geometry_scale ?? 1]);')
        print(f'    monitors: {geo}')

    def ext_states(self):
        """Extension states/errors; works without unsafe mode."""
        try:
            res = self.bus.call_sync(
                'org.gnome.Shell', '/org/gnome/Shell',
                'org.gnome.Shell.Extensions', 'ListExtensions', None,
                GLib.VariantType('(a{sa{sv}})'), Gio.DBusCallFlags.NONE,
                5000, None)
        except GLib.Error as e:
            return f'ListExtensions failed: {e.message}'
        exts = res.unpack()[0]
        return {u: {k: exts[u].get(k) for k in ('state', 'error')}
                for u in (UUID, HELPER_UUID) if u in exts} or \
            f'not found; known: {sorted(exts)[:10]}'

    def js(self, body, timeout=30):
        """Run body inside an async function in the shell; returns JSON."""
        code = f'(async () => {{ {body} }})()'
        res = self.bus.call_sync(
            'org.gnome.Shell', '/org/gnome/Shell', 'org.gnome.Shell', 'Eval',
            GLib.Variant('(s)', (code,)), GLib.VariantType('(bs)'),
            Gio.DBusCallFlags.NONE, timeout * 1000, None)
        ok, out = res.unpack()
        if not ok:
            raise RuntimeError(f'JS error: {out}')
        return json.loads(out) if out else None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def errors(self):
        """Log messages (with their stack) that come from our extension."""
        lines = (self.tmp / 'shell.log').read_text(
            errors='replace').splitlines()
        new_msg = re.compile(r'^(\(|\*\*|[\w .-]+-(Message|WARNING|CRITICAL))')
        out = []
        for i, line in enumerate(lines):
            if not any(w in line for w in ('CRITICAL', 'JS ERROR', 'WARNING',
                                           'Warning', 'Error')):
                continue
            block = [line]
            for nxt in lines[i + 1:i + 40]:
                if new_msg.match(nxt):
                    break
                block.append(nxt)
            text = '\n'.join(block)
            if 'boom in selector' in text:   # deliberately injected
                continue
            if '@lyka/' in text or 'zehntage-gnome:' in text:
                out.append(line.strip())
        return out


# --------------------------------------------------------------- helpers

def png_size(b64):
    """Image size of a base64 PNG or JPEG."""
    raw = base64.b64decode(b64)
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return struct.unpack('>II', raw[16:24])
    w, h, _ = decode_png(b64)
    return w, h


def decode_png(b64):
    """-> (width, height, pixel(x, y) -> (r, g, b))."""
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf
    loader = GdkPixbuf.PixbufLoader()
    loader.write(base64.b64decode(b64))
    loader.close()
    pb = loader.get_pixbuf()
    data, stride, n = pb.get_pixels(), pb.get_rowstride(), pb.get_n_channels()

    def pixel(x, y):
        i = y * stride + x * n
        return tuple(data[i:i + 3])
    return pb.get_width(), pb.get_height(), pixel


def magenta(rgb):
    r, g, b = rgb
    return r > 220 and g < 40 and b > 220


_scale = {}


def js_round(v):
    return math.floor(v + 0.5)   # JS Math.round, not banker's rounding


def scale_of(sh):
    if 'v' not in _scale:
        _scale['v'] = sh.js('return await zt.captureScale();')
    return _scale['v']


def px(sh, *vals):
    """Logical px -> captured image px, rounded like the selector does."""
    return tuple(js_round(v * scale_of(sh)) for v in vals)


def span(sh, a, b):
    """Captured size of the logical span a..b (edges rounded separately)."""
    s = scale_of(sh)
    return js_round(b * s) - js_round(a * s)


def stage_size(sh):
    return tuple(sh.js('return [global.stage.width, global.stage.height];'))


def full_px(sh):
    w, h = px(sh, *stage_size(sh))
    k = 1568 / max(w, h)   # the selector downscales the long edge
    return (round(w * k), round(h * k)) if k < 1 else (w, h)


def last_image(i=-1):
    return Mock.requests[i]['body']['messages'][0]['content'][0]


def capture(sh, x1, y1, x2, y2):
    """Hotkey, drag, wait for the new entry to settle; returns state."""
    before = state(sh)['id']
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js(f'await zt.drag({x1}, {y1}, {x2}, {y2});')
    return wait_state(sh, lambda s: s['id'] != before and
                      s['status'] in ('ok', 'error'), timeout=15)


def entries(sh):
    return sh.js('return zt.inst()._history.entries.map(e => ({'
                 'status: e.status, error: e.error, turns: e.turns, '
                 'imagePath: e.imagePath, model: e.model, '
                 'backend: e.backend ?? null}));')


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


STATE = '''
const i = zt.inst();
const e = i._history.entries[0];
return {
    menuOpen: i._indicator.menu.isOpen,
    overview: zt.Main.overview.visible,
    selector: i._selector.active,
    entries: i._history.entries.length,
    id: e?.id ?? null,
    status: e?.status ?? null, error: e?.error ?? null,
    turns: e?.turns ?? null,
    followUpError: e?.followUpError ?? null,
    labels: i._indicator.menu.box.get_children().length,
};
'''


def state(sh):
    return sh.js(STATE)


def wait_state(sh, pred, timeout=8):
    end = time.time() + timeout
    s = None
    while time.time() < end:
        s = state(sh)
        if pred(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f'state never matched; last: {s}')


def menu_text(sh):
    return sh.js('''
        const out = [];
        const walk = a => {
            if (a instanceof zt.St.Label) out.push(a.get_text());
            for (const c of a.get_children()) walk(c);
        };
        walk(zt.inst()._indicator.menu.box);
        return out.join('\\n');
    ''')


def hotkey(sh):
    sh.js('await zt.chord(zt.Clutter.KEY_Super_L, zt.Clutter.KEY_z);')


def shot(sh, name):
    OUT.mkdir(exist_ok=True)
    sh.js(f'return zt.screenshot({json.dumps(str(OUT / name))});')


def reset(sh):
    """Close UI, clear history and mock between scenarios."""
    sh.js('''
        const i = zt.inst();
        i._selector.cancel();
        i._indicator.menu.close(0);
        i._history.entries.length = 0;
        i._history.save();
        i._indicator.setEntries(i._history.entries);
        zt.clearBoxes();
        if (zt.Main.overview.visible) zt.Main.overview.hide();
        await zt.moveTo(5, 400);
        await zt.sleep(150);
    ''')
    m = sh.js('return zt.modal();')
    check(m['modalCount'] == 0 and m['shades'] == 0,
          f'leaked modal/overlay from previous scenario: {m}')
    Mock.mode = 'ok'
    Mock.answer = Mock.status = None
    Mock.delay = Mock.chunk_delay = 0.0
    Mock.chunks = 3
    Mock.stream_error = False
    Mock.requests.clear()
    Mock.gets.clear()
    for key in ('history-size', 'effort', 'model', 'cli-timeout',
                'capture-hotkey', 'thinking', 'stream', 'strong-model',
                'strong-effort'):
        subprocess.run(['gsettings', '--schemadir', str(sh.schemadir),
                        'reset', SCHEMA, key], check=True)
    sh.gset('backend', "'api'")
    fake = Path(os.environ['ZT_FAKE_DIR'])
    for f in ('mode', 'calls.jsonl'):
        (fake / f).unlink(missing_ok=True)


def fake_calls():
    path = Path(os.environ['ZT_FAKE_DIR']) / 'calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def use_cli(sh, mode='ok'):
    (Path(os.environ['ZT_FAKE_DIR']) / 'mode').write_text(mode)
    sh.gset('backend', "'cli'")
    time.sleep(0.1)


def argval(argv, flag):
    return argv[argv.index(flag) + 1]


# --------------------------------------------------------------- scenarios

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
    img = b['messages'][0]['content'][0]
    check(img['type'] == 'image' and
          img['source']['media_type'] == 'image/png', img['type'])
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
    img = Mock.requests[0]['body']['messages'][0]['content'][0]
    check(png_size(img['source']['data']) == full_px(sh),
          f"{png_size(img['source']['data'])} != full {full_px(sh)}")


def t_followup_typed_is_append_only(sh):
    sh.gset('thinking', 'true')   # so there are thinking blocks to replay
    time.sleep(0.2)
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 400, 300);')
    wait_state(sh, lambda s: s['status'] == 'ok')
    first = Mock.requests[0]['body']
    # Follow-up entry should already have key focus: just type + Enter.
    sh.js("await zt.type('und Plural?'); "
          "await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(len(Mock.requests) == 2, 'follow-up not sent')
    second = Mock.requests[1]['body']
    msgs = second['messages']
    check([m['role'] for m in msgs] == ['user', 'assistant', 'user'],
          [m['role'] for m in msgs])
    check(msgs[0] == first['messages'][0], 'image turn changed')
    check(msgs[1]['content'][0] == {'type': 'thinking', 'thinking': '',
                                    'signature': 'sig1'},
          'thinking block must be replayed verbatim')
    check(msgs[2]['content'] == 'und Plural?', msgs[2]['content'])
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
    sh.js('const i = zt.inst(); i._retry(i._history.entries[0]);')
    wait_state(sh, lambda s: s['status'] == 'ok')


def t_refusal_is_error(sh):
    Mock.mode = 'refusal'
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    s = wait_state(sh, lambda s: s['status'] == 'error')
    check('declined' in s['error'], s['error'])


def t_pending_state_visible(sh):
    Mock.mode = 'slow'
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
    sh.js(f'''
        const m = zt.Main.extensionManager;
        await m.disableExtension('{UUID}');
        await m.enableExtension('{UUID}');
        await zt.sleep(200);
    ''')
    s = state(sh)
    check(s['entries'] == 1 and s['status'] == 'ok', f'history lost: {s}')
    check(s['turns'][0]['answer'] == ANSWER, 'answer lost')


def t_no_key_shows_hint(sh):
    sh.gset('claude-api-key', "''")
    try:
        time.sleep(0.2)
        hotkey(sh)
        s = wait_state(sh, lambda s: s['menuOpen'])
        check(not s['selector'], 'selector must not open without a key')
        check('API key is not set' in menu_text(sh), 'no key hint')
        shot(sh, 'no-key.png')
    finally:
        sh.gset('claude-api-key', "'test-key'")


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
                 '--no-session-persistence', '--verbose'):
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
    img = msg['message']['content'][0]
    check(msg['type'] == 'user' and img['type'] == 'image', msg['type'])
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
    check(content[0]['type'] == 'image', 'image missing in follow-up')
    text = content[1]['text']
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
    sh.gset('claude-path', "'/nonexistent/claude'")
    try:
        time.sleep(0.1)
        hotkey(sh)
        s = wait_state(sh, lambda s: s['menuOpen'])
        check(not s['selector'], 'selector must not open')
        check('claude CLI not found' in menu_text(sh), 'no cli hint')
    finally:
        sh.gset('claude-path', f"'{ROOT / 'test' / 'fake-claude.py'}'")


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
    check(png_size(last_image()['source']['data']) == full_px(sh),
          'tiny drag should capture the whole screen')


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
    sw, shh = stage_size(sh)
    sh.js(f'await zt.click({sw // 2}, {shh // 2});')
    wait_state(sh, lambda s: s['status'] == 'ok')
    w, h, pix = decode_png(last_image()['source']['data'])
    k = w / px(sh, sw)[0]
    cx, cy = px(sh, mx + mw // 2, my + mh // 2)
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
    time.sleep(0.3)
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')
    wait_state(sh, lambda s: not s['selector'])


def t_second_accelerator_xf86favorites(sh):
    sh.gset('capture-hotkey', "['<Super>z', 'XF86Favorites']")
    time.sleep(0.3)
    sh.js('await zt.chord(zt.Clutter.KEY_Favorites);')
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')
    wait_state(sh, lambda s: not s['selector'])
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_hotkey_rebind_at_runtime(sh):
    sh.gset('capture-hotkey', "['<Super>x']")
    time.sleep(0.3)
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
    es = []
    end = time.time() + 10
    while time.time() < end:
        es = entries(sh)
        if len(es) == 2 and all(e['status'] == 'ok' for e in es):
            break
        time.sleep(0.1)
    check([e['turns'][0]['answer'] for e in es] == ['Antwort 2', 'Antwort 1'],
          f'answers mixed up: {es}')


# ----- disable at awkward moments (a stuck modal would freeze the shell)

def _disable_enable(sh):
    sh.js(f"""
        const m = zt.Main.extensionManager;
        await m.disableExtension('{UUID}');
        await zt.sleep(300);
        globalThis.__modal = zt.modal();
        await m.enableExtension('{UUID}');
        await zt.sleep(200);
    """)
    return sh.js('return globalThis.__modal;')


def t_disable_while_selecting(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    m = _disable_enable(sh)
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


def t_disable_during_pending_request(sh):
    Mock.delay = 2.0
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    wait_state(sh, lambda s: s['status'] == 'pending')
    _disable_enable(sh)
    time.sleep(2.5)   # let the aborted request settle
    s = state(sh)
    check(s['entries'] == 1 and s['status'] == 'error' and
          s['error'] == 'Interrupted', f'after re-enable: {s}')
    sh.js('const i = zt.inst(); i._retry(i._history.entries[0]);')
    wait_state(sh, lambda s: s['status'] == 'ok', timeout=10)


# ----- history persistence

def _data_dir():
    return Path(os.environ['XDG_DATA_HOME']) / 'zehntage-gnome@lyka'


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
    (_data_dir() / 'history.json').write_text('{ this is not json')
    sh.js(f"""
        const m = zt.Main.extensionManager;
        const i = zt.inst();
        i._history.save = () => {{}};   // keep the garbage on disk
        await m.disableExtension('{UUID}');
        await m.enableExtension('{UUID}');
        await zt.sleep(200);
    """)
    check(state(sh)['entries'] == 0, 'garbage history should load empty')
    capture(sh, 100, 100, 200, 200)


def t_legacy_gemini_entry_renders_and_follows_up(sh):
    capture(sh, 100, 100, 200, 200)
    sh.js("""
        const i = zt.inst(), e = i._history.entries[0];
        for (const k of ['backend', 'model', 'system', 'mediaType'])
            delete e[k];
        e.turns = [{answer: 'old **gemini** answer'}];
        i._history.save();""")
    _disable_enable(sh)
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
    h = sh.js('return zt.inst()._indicator.menu.actor.height;')
    check(h <= stage_size(sh)[1], f'menu taller than screen: {h}')
    shot(sh, 'long-answer.png')


# ----- follow-ups

def t_enter_twice_sends_one_followup(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.delay = 1.0
    sh.js("await zt.type('x'); const c = zt.Clutter; "
          "await zt.chord(c.KEY_Return); await zt.chord(c.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    time.sleep(1.5)
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
          msgs[2]['content'] == 'q2', f'failed q1 leaked into history: {msgs}')


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
    time.sleep(0.2)
    capture(sh, 100, 100, 200, 200)
    b = Mock.requests[0]['body']
    check(b['model'] == 'claude-test-x' and
          b['output_config'] == {'effort': 'medium'}, b)
    sh.gset('model', "'claude-other'")
    time.sleep(0.2)
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
    t0 = time.time()
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'error' and 'timed out' in s['error'], s)
    check(time.time() - t0 < 6, f'took {time.time() - t0:.1f}s')
    pid = int((Path(os.environ['ZT_FAKE_DIR']) / 'hang.pid').read_text())
    time.sleep(0.3)
    try:
        os.kill(pid, 0)
        alive = 'zombie' not in Path(f'/proc/{pid}/status').read_text()
    except (ProcessLookupError, FileNotFoundError):
        alive = False
    check(not alive, f'hung claude process {pid} left running')


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
    """Gemini-era entries (no backend/model/system) on the default CLI."""
    use_cli(sh)
    capture(sh, 100, 100, 200, 200)
    sh.js("""
        const i = zt.inst(), e = i._history.entries[0];
        for (const k of ['backend', 'model', 'system', 'mediaType'])
            delete e[k];
        e.turns = [{answer: 'gemini says hi'},
                   {question: 'q', answer: '⚠ Gemini API error 503: x'}];
        i._history.save();""")
    _disable_enable(sh)
    sh.js('zt.inst()._indicator.menu.open(0); await zt.sleep(200);')
    check('Gemini API error' not in menu_text(sh), 'stored error shown as answer')
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('noch?'); await zt.chord(zt.Clutter.KEY_Return);")
    s = wait_state(sh, lambda s: len(s['turns'] or []) == 2 or
                   s['followUpError'])
    check(not s['followUpError'], s['followUpError'])
    calls = fake_calls()
    check(len(calls) == 2, f'follow-up went elsewhere: {len(calls)} cli calls')
    text = json.loads(calls[1]['stdin'])['message']['content'][1]['text']
    check('gemini says hi' in text and '⚠' not in text, text)


REAL_HISTORY = Path.home() / '.local' / 'share' / 'zehntage-gnome@lyka'


def _real_history_fingerprint():
    return sorted((f.name, f.stat().st_size, f.stat().st_mtime_ns)
                  for f in REAL_HISTORY.iterdir())


def t_real_user_history_copy(sh):
    """Your actual (Gemini-era) history, copied with rewritten paths."""
    src = REAL_HISTORY / 'history.json'
    if not src.exists():
        print('    (no real history on this machine; skipped)')
        return
    before = _real_history_fingerprint()
    dst = _data_dir()
    data = json.loads(src.read_text())
    for e in data:
        name = Path(e['imagePath']).name
        shutil.copy(REAL_HISTORY / name, dst / name)
        e['imagePath'] = str(dst / name)   # never point at the real files
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
    end = time.time() + 15
    while time.time() < end and not all(
            sh.js('return zt.inst()._history.entries.map('
                  'e => !!e.thumbPath);')):
        time.sleep(0.2)
    thumbs = sh.js('return zt.inst()._history.entries.map(e => e.thumbPath);')
    check(all(thumbs), f'{thumbs.count(None)} thumbnails missing')
    for t in thumbs:
        tw, th = png_size(base64.b64encode(Path(t).read_bytes()))
        check(tw <= 640 and th <= 280, f'thumbnail too big: {tw}x{th}')
        check(str(t).startswith(str(dst)), f'thumbnail outside test dir: {t}')
    check(all(e['backend'] == 'cli' and e['model'] for e in es), 'migration')
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
        sh.js(f'const i = zt.inst(); i._retry(i._history.entries[{errs[0]}]);')
        end = time.time() + 8
        while entries(sh)[errs[0]]['status'] != 'ok' and time.time() < end:
            time.sleep(0.1)
        check(entries(sh)[errs[0]]['status'] == 'ok', 'retry of old error')
    capture(sh, 100, 100, 200, 200)   # evicts the oldest copied entry
    check(_real_history_fingerprint() == before,
          'REAL history directory was modified!')


def t_collapsed_rows_are_single_line(sh):
    Mock.status = lambda n: 400 if n == 1 else 200
    capture(sh, 100, 100, 200, 200)       # error entry
    sh.js("""const e = zt.inst()._history.entries[0];
             e.error = 'API error 403: {\\n  "error": {\\n    "code": 403';""")
    capture(sh, 100, 100, 220, 200)       # newer ok entry -> first collapses
    labels = sh.js("""
        const out = [];
        const walk = a => {
            if (a.style_class === 'zehntage-collapsed-label')
                out.push(a.get_text());
            for (const c of a.get_children()) walk(c);
        };
        walk(zt.inst()._indicator.menu.box);
        return out;""")
    check(labels, 'no collapsed rows rendered')
    check(all('\n' not in t for t in labels), f'multi-line row: {labels}')


def _followup_text(sh):
    return sh.js('return zt.inst()._indicator._focusTarget?.get_text() ?? null;')


def t_followup_draft_survives_pending_and_rerender(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.delay = 1.5
    sh.js("await zt.type('erste'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: True)
    time.sleep(0.2)
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
    check(Mock.requests[2]['body']['messages'][-1]['content'] == 'zweite',
          'draft not sent')
    check(_followup_text(sh) == '', 'field not cleared after sending')


def t_noise_image_respects_size_limit(sh):
    """Incompressible full-screen noise: payload must stay under 5 MB."""
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf
    sw, shh = stage_size(sh)
    w, h = px(sh, sw, shh)
    noise = Path(os.environ['ZT_TMP']) / 'noise.png'
    GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(os.urandom(w * h * 3)), GdkPixbuf.Colorspace.RGB,
        False, 8, w, h, w * 3).savev(str(noise), 'png', [], [])
    sh.js(f"""const b = new zt.St.Widget({{x: 0, y: 0, width: {sw},
        height: {shh}, style: 'background-image: url("file://{noise}");'
            + 'background-size: cover;'}});
        zt.Main.layoutManager.uiGroup.insert_child_above(b,
            zt.Main.layoutManager.panelBox);
        (zt._boxes ??= []).push(b); await zt.sleep(300);""")
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
        time.sleep(0.2)
        capture(sh, 100, 100, 200, 200)
        b = Mock.requests[-1]['body']
        check(b['max_tokens'] == expected and
              b['output_config']['effort'] == effort, b)


def t_bad_markup_falls_back_to_plain_text(sh):
    Mock.answer = lambda n, b: '**a _b** c_ and `x*y` *z*'
    log = Path(os.environ['ZT_TMP']) / 'shell.log'
    before = log.read_text(errors='replace').count('Failed to set the markup')
    capture(sh, 100, 100, 200, 200)
    text = menu_text(sh)
    check('a' in text and 'c' in text and 'z' in text, text)
    after = log.read_text(errors='replace').count('Failed to set the markup')
    check(after == before, 'invalid Pango markup reached set_markup()')


def t_cli_path_with_tilde(sh):
    home = Path.home()
    fake = ROOT / 'test' / 'fake-claude.py'
    if home not in fake.parents:
        print('    (repo not under $HOME; skipped)')
        return
    use_cli(sh)
    sh.gset('claude-path', f"'~/{fake.relative_to(home)}'")
    try:
        time.sleep(0.2)
        s = capture(sh, 100, 100, 200, 200)
        check(s['status'] == 'ok', s)
    finally:
        sh.gset('claude-path', f"'{fake}'")


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
        time.sleep(0.8)
        capture(sh, 100, 100, 260, 200)
        shot(sh, f'theme-{scheme}.png')
    dim = sh.js("""
        const out = [];
        const walk = a => {
            if (a.style_class === 'zehntage-answer')
                out.push(a.get_theme_node().get_foreground_color().alpha);
            for (const c of a.get_children()) walk(c);
        };
        walk(zt.inst()._indicator.menu.box);
        return out;""")
    check(dim and all(a == 255 for a in dim), f'answer text dimmed: {dim}')
    sh.gset('color-scheme', "'default'", 'org.gnome.desktop.interface')


# ----- speed: streaming, thinking, warm-up

def t_request_is_tuned_for_speed(sh):
    capture(sh, 100, 100, 200, 200)
    b = Mock.requests[0]['body']
    check(b['stream'] is True, 'request must stream')
    check(b.get('thinking') == {'type': 'disabled'},
          f'thinking should be off by default: {b.get("thinking")}')
    check(b['output_config'] == {'effort': 'low'}, b['output_config'])


def t_thinking_switch_on(sh):
    sh.gset('thinking', 'true')
    time.sleep(0.2)
    capture(sh, 100, 100, 200, 200)
    b = Mock.requests[0]['body']
    check('thinking' not in b, f'thinking on = adaptive default: {b}')


def t_warmup_connection_on_hotkey(sh):
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    end = time.time() + 2
    while not Mock.gets and time.time() < end:
        time.sleep(0.05)
    check(Mock.gets == ['/v1/models/claude-haiku-5-5'],
          f'no warm-up request while selecting: {Mock.gets}')
    check(not Mock.requests, 'warm-up must not create a message')
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_no_warmup_for_cli_backend(sh):
    use_cli(sh)
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    time.sleep(0.5)
    check(not Mock.gets, f'cli backend must not touch the API: {Mock.gets}')
    sh.js('await zt.chord(zt.Clutter.KEY_Escape);')


def t_streaming_shows_partial_answer(sh):
    Mock.answer = lambda n, b: 'ERSTER TEIL zweiter teil DRITTER TEIL'
    Mock.chunks = 3
    Mock.chunk_delay = 0.8
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js('await zt.drag(100, 100, 300, 200);')
    end = time.time() + 5
    text = ''
    while time.time() < end:
        text = menu_text(sh)
        if 'ERSTER' in text:
            break
        time.sleep(0.05)
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
    end = time.time() + 5
    while 'FOLGE eins' not in menu_text(sh) and time.time() < end:
        time.sleep(0.05)
    text = menu_text(sh)
    check('FOLGE eins' in text and 'FOLGE zwei' not in text, text)
    wait_state(sh, lambda s: len(s['turns'] or []) == 2, timeout=8)


def t_stream_error_event_is_shown(sh):
    Mock.stream_error = True
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'error' and 'Overloaded' in s['error'], s)


def t_large_capture_sent_as_jpeg(sh):
    """A detailed full screen is >300 KB as PNG: must go out as JPEG."""
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf
    sw, shh = stage_size(sh)
    w, h = px(sh, sw, shh)
    noise = Path(os.environ['ZT_TMP']) / 'noise2.png'
    GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(os.urandom(w * h * 3)), GdkPixbuf.Colorspace.RGB,
        False, 8, w, h, w * 3).savev(str(noise), 'png', [], [])
    sh.js(f"""const b = new zt.St.Widget({{x: 0, y: 0, width: {sw},
        height: {shh}, style: 'background-image: url("file://{noise}");'
            + 'background-size: cover;'}});
        zt.Main.layoutManager.uiGroup.insert_child_above(b,
            zt.Main.layoutManager.panelBox);
        (zt._boxes ??= []).push(b); await zt.sleep(300);""")
    s = capture(sh, 100, 100, 700, 500)
    check(s['status'] == 'ok', s)
    src = last_image()['source']
    check(src['media_type'] == 'image/jpeg', src['media_type'])
    check(base64.b64decode(src['data'])[:2] == b'\xff\xd8', 'not JPEG bytes')


def t_defaults_are_api_stream_no_thinking(sh):
    for key in ('backend', 'stream', 'thinking', 'effort'):
        subprocess.run(['gsettings', '--schemadir', str(sh.schemadir),
                        'reset', SCHEMA, key], check=True)
    time.sleep(0.2)
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'ok', s)
    check(len(Mock.requests) == 1 and not fake_calls(), 'default backend '
          'must be the API')
    b = Mock.requests[0]['body']
    check(b['stream'] is True and b.get('thinking') == {'type': 'disabled'}
          and b['output_config'] == {'effort': 'low'}, b)


def t_stream_setting_off(sh):
    sh.gset('stream', 'false')
    time.sleep(0.2)
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'ok' and s['turns'][0]['answer'] == ANSWER, s)
    check(Mock.requests[0]['body']['stream'] is False, 'stream not off')
    sh.js("await zt.type('und?'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: len(s['turns'] or []) == 2)
    check(Mock.requests[1]['body']['stream'] is False, 'follow-up streamed')


# ----- real app windows (Wayland and X11 clients)

APP = ROOT / 'test' / 'color-app.py'


def cyan(rgb):
    r, g, b = rgb
    return r < 40 and g > 220 and b > 220


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


def _upgrade(sh):
    sh.js("""const b = (function find(a) {
            if (a.style_class?.includes('zehntage-upgrade')) return a;
            for (const c of a.get_children()) {
                const r = find(c); if (r) return r;
            }
            return null;
        })(zt.inst()._indicator.menu.box);
        if (!b) throw new Error('no Opus button');
        b.emit('clicked', 1);""")


def t_opus_button_rewrites_answer(sh):
    capture(sh, 100, 100, 200, 200)
    label = sh.js("""return (function find(a) {
            if (a.style_class?.includes('zehntage-upgrade')) return a.label;
            for (const c of a.get_children()) {
                const r = find(c); if (r) return r;
            }
            return null;
        })(zt.inst()._indicator.menu.box);""")
    check(label == 'Opus 5.5', f'button label: {label!r}')
    _upgrade(sh)
    s = wait_state(sh, lambda s: s['turns'] and
                   s['turns'][0].get('model'), timeout=8)
    b = Mock.requests[1]['body']
    check(b['model'] == 'claude-opus-5-5', b['model'])
    check(b['output_config'] == {'effort': 'high'}, b['output_config'])
    check('thinking' not in b, 'Opus 5.5: thinking must stay adaptive')
    check(b['max_tokens'] >= 16000, b['max_tokens'])
    check(len(b['messages']) == 1 and
          b['messages'][0]['content'][0]['type'] == 'image', b['messages'])
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
    _upgrade(sh)
    s = wait_state(sh, lambda s: s['turns'][-1].get('model'), timeout=8)
    msgs = Mock.requests[2]['body']['messages']
    check([m['role'] for m in msgs] == ['user', 'assistant', 'user'] and
          msgs[2]['content'] == 'Plural?', msgs)
    check(s['turns'][0]['answer'] == ANSWER and
          s['turns'][1] == {'question': 'Plural?', 'answer': '**Opus** 3',
                            'content': s['turns'][1]['content'],
                            'model': 'claude-opus-5-5'}, s['turns'])


def t_opus_after_haiku_error(sh):
    Mock.status = lambda n: 401 if n == 1 else 200
    s = capture(sh, 100, 100, 200, 200)
    check(s['status'] == 'error', s)
    _upgrade(sh)
    s = wait_state(sh, lambda s: s['status'] == 'ok', timeout=8)
    check(Mock.requests[1]['body']['model'] == 'claude-opus-5-5', 'model')
    check(s['turns'][0]['model'] == 'claude-opus-5-5', s['turns'])


def t_opus_failure_keeps_haiku_answer(sh):
    Mock.status = lambda n: 529 if n == 2 else 200
    capture(sh, 100, 100, 200, 200)
    _upgrade(sh)
    end = time.time() + 6
    err = None
    while time.time() < end and not err:
        err = sh.js('return zt.inst()._history.entries[0].upgradeError '
                    '?? null;')
        time.sleep(0.1)
    check(err and '529' in err, f'no upgrade error: {err}')
    s = state(sh)
    check(s['turns'][0]['answer'] == ANSWER and
          'model' not in s['turns'][0], 'Haiku answer lost')
    check('529' in menu_text(sh), 'upgrade error not shown')


def t_opus_streams_live(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.answer = lambda n, b: 'OPUSTEIL eins OPUSTEIL zwei'
    Mock.chunks = 2
    Mock.chunk_delay = 0.8
    _upgrade(sh)
    end = time.time() + 5
    while 'OPUSTEIL eins' not in menu_text(sh) and time.time() < end:
        time.sleep(0.05)
    text = menu_text(sh)
    check('OPUSTEIL eins' in text and 'OPUSTEIL zwei' not in text and
          'белка' not in text, text)
    wait_state(sh, lambda s: s['turns'][0].get('model'), timeout=8)


def t_opus_blocks_followup_while_running(sh):
    capture(sh, 100, 100, 200, 200)
    Mock.delay = 1.5
    _upgrade(sh)
    time.sleep(0.3)
    sh.js("zt.inst()._indicator._focusTarget?.grab_key_focus(); "
          "await zt.type('warte'); await zt.chord(zt.Clutter.KEY_Return);")
    wait_state(sh, lambda s: s['turns'][0].get('model'), timeout=8)
    time.sleep(0.3)
    check(len(Mock.requests) == 2, 'follow-up sent during the Opus rewrite')


def t_opus_with_cli_backend(sh):
    use_cli(sh)
    capture(sh, 100, 100, 200, 200)
    _upgrade(sh)
    s = wait_state(sh, lambda s: s['turns'] and
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


SCENARIOS = [v for k, v in list(globals().items()) if k.startswith('t_')]


def t_live(sh):
    """Real Claude API: time to first visible words and to the full answer."""
    first_t, total_t = [], []
    for i in range(3):
        reset(sh)
        fx = sh.js("return zt.fixture('Das Eichhörnchen frisst Nüsse');")
        hotkey(sh)
        wait_state(sh, lambda s: s['selector'])
        x, y, w, h = fx
        sh.js(f'await zt.drag({x - 10}, {y - 10}, '
              f'{x + w + 10}, {y + h + 10});')
        t0 = time.time()
        first = None
        while time.time() - t0 < 30:
            st = sh.js('const e = zt.inst()._history.entries[0]; '
                       'return [e?.status, !!e?.partial];')
            if first is None and (st[1] or st[0] == 'ok'):
                first = time.time() - t0
            if st[0] in ('ok', 'error'):
                break
            time.sleep(0.02)
        total = time.time() - t0
        s = state(sh)
        check(s['status'] == 'ok', s['error'])
        check('белк' in s['turns'][0]['answer'].lower(), s['turns'][0])
        first_t.append(first)
        total_t.append(total)
        print(f'    run {i + 1}: first words {first:.2f}s, done {total:.2f}s'
              f'  {s["turns"][0]["answer"][:60]!r}')
    shot(sh, 'live.png')
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
    _upgrade(sh)
    first = None
    while time.time() - t0 < 120:
        st = sh.js('const e = zt.inst()._history.entries[0]; '
                   'return [!!e.partialUpgrade, !!e.upgradePending, '
                   'e.upgradeError ?? null];')
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


def t_live_cli(sh):
    """Real `claude -p` (your Claude Code login), timed end to end."""
    times = []
    for i in range(3):
        reset(sh)   # also clears fixtures, so add it each round
        fx = sh.js("return zt.fixture('Das Eichhörnchen frisst Nüsse');")
        sh.gset('backend', "'cli'")
        hotkey(sh)
        wait_state(sh, lambda s: s['selector'])
        x, y, w, h = fx
        sh.js(f'await zt.drag({x - 10}, {y - 10}, '
              f'{x + w + 10}, {y + h + 10});')
        t0 = time.time()
        s = wait_state(sh, lambda s: s['status'] in ('ok', 'error'),
                       timeout=60)
        times.append(time.time() - t0)
        check(s['status'] == 'ok', s['error'])
        check(not s['overview'], f'overview opened in run {i + 1}')
        check('белк' in s['turns'][0]['answer'].lower(),
              f'answer does not mention the squirrel: {s["turns"][0]}')
        print(f'    run {i + 1}: {times[-1]:.2f}s  '
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


# --------------------------------------------------------------- main

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
    dconf_before = real_dconf_dump(tmp)
    (tmp / 'fake').mkdir(exist_ok=True)
    os.environ['ZT_FAKE_DIR'] = str(tmp / 'fake')
    assert os.environ.get('GSETTINGS_BACKEND') == 'keyfile'
    assert os.environ['XDG_CONFIG_HOME'].startswith(str(tmp))

    if live_cli:
        settings = {'backend': "'cli'"}
        scenarios = [t_loads, t_live_cli]
    elif live:
        key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not key:
            print('--live needs ANTHROPIC_API_KEY'); sys.exit(2)
        settings = {'claude-api-key': f"'{key}'", 'backend': "'api'"}
        os.environ.pop('ANTHROPIC_API_KEY', None)
        scenarios = [t_loads, t_live]
    else:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        settings = {'claude-api-key': "'test-key'",
                    'backend': "'api'",
                    'claude-path': f"'{ROOT / 'test' / 'fake-claude.py'}'",
                    'api-base-url':
                        f"'http://127.0.0.1:{server.server_port}'"}
        scenarios = [s for s in SCENARIOS
                     if not filters or any(f in s.__name__ for f in filters)]

    sh = Shell(tmp, settings)
    failed = 0
    try:
        sh.install()
        sh.start()
        for sc in scenarios:
            name = sc.__name__[2:]
            try:
                if sc is not t_loads:
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
        after = real_dconf_dump(tmp)
        changed = sorted(k for k in dconf_before.keys() | after.keys()
                         if dconf_before.get(k) != after.get(k))
        ours = [k for k in changed if k.startswith(HARNESS_KEYS)]
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
    print(f'\n{len(scenarios) - failed}/{len(scenarios)} passed; '
          f'log: {tmp / "shell.log"}; screenshots: {OUT}')
    sys.exit(1 if failed or errs else 0)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)

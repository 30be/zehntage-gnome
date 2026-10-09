"""Test infrastructure: mock Claude API, the throwaway shell, helpers.

Imported by run.py only after it has re-executed itself into the isolated
environment (private D-Bus session, XDG dirs, runtime dir, keyfile
GSettings backend) — never import this from a normal environment.
"""

import base64
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable

import gi

gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'test' / 'out'
UUID = 'zehntage-gnome@lyka'
HELPER_UUID = 'zehntage-test-helper@lyka'
SCHEMA = 'org.gnome.shell.extensions.zehntage-gnome'
EXT_FILES = ['extension.js', 'claude.js', 'cli.js', 'selector.js',
             'history.js', 'indicator.js', 'prefs.js', 'metadata.json',
             'stylesheet.css']
FAKE_CLAUDE = ROOT / 'test' / 'fake-claude.py'
# The real home, even when mock runs give the shell a throwaway $HOME.
REAL_HOME = Path(os.environ.get('ZT_REAL_HOME') or Path.home())
REAL_DCONF = REAL_HOME / '.config' / 'dconf' / 'user'
REAL_HISTORY = REAL_HOME / '.local' / 'share' / 'zehntage-gnome@lyka'
# --screens "1920x1080@1.25,1920x1080@1": monitors left to right, with scale.
SCREENS = [(1280, 800, 1.0)]
ANSWER = '**Eichhörnchen** — белка.\n\n- *das* Eichhörnchen, neutr.'
SUFFIX = '\n\n(ответ по-русски)'   # default follow-up suffix, as sent

# Foreign keys the harness sets in the test shell (besides SCHEMA).
BASE_SETTINGS = {
    ('org.gnome.shell', 'enabled-extensions'): f"['{UUID}', '{HELPER_UUID}']",
    ('org.gnome.shell', 'welcome-dialog-last-shown-version'): "'999'",
    ('org.gnome.desktop.interface', 'enable-animations'): 'false',
    # The virtual pointer starts at (0,0): the hot corner would open the
    # overview on the first pointer event.
    ('org.gnome.desktop.interface', 'enable-hot-corners'): 'false',
}


# --------------------------------------------------------------- real dconf

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


def keyfile_paths(tmp):
    """dconf-style paths of every key the test shell's keyfile holds."""
    keyfile = tmp / 'config' / 'glib-2.0' / 'settings' / 'keyfile'
    paths, section = set(), ''
    if keyfile.exists():
        for line in keyfile.read_text().splitlines():
            if line.startswith('[') and line.endswith(']'):
                section = '/' + line[1:-1].strip('/') + '/'
            elif '=' in line:
                paths.add(section + line.split('=', 1)[0].strip())
    return paths


# --------------------------------------------------------------- mock API

class Mock:
    """State of the mock Claude API; reset() before every scenario."""
    mode = 'ok'          # ok | 401 | refusal
    requests: list = []
    gets: list = []      # GET paths (connection warm-up)
    get_ports: list = []  # client ports of those GETs (connection reuse)
    answer: Callable | None = None   # callable(n, body) -> text
    status: Callable | None = None   # callable(n) -> http status
    delay = 0.0          # seconds before answering
    chunks = 3           # text deltas per answer when streaming
    chunk_delay = 0.0    # seconds between deltas
    stream_error = False  # send an SSE error event mid-answer
    inflight = 0
    lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.mode = 'ok'
        cls.answer = cls.status = None
        cls.delay = cls.chunk_delay = 0.0
        cls.chunks = 3
        cls.stream_error = False
        cls.requests.clear()
        cls.gets.clear()
        cls.get_ports.clear()

    @classmethod
    def wait_idle(cls, timeout=10):
        wait_for(lambda: cls.inflight == 0, timeout,
                 'mock API requests still in flight')


class Handler(BaseHTTPRequestHandler):
    # Keep-alive, like the real API, so connection reuse is observable.
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):  # noqa: A002 (silence logging)
        pass

    def _track(self, fn):
        with Mock.lock:
            Mock.inflight += 1
        try:
            fn()
        finally:
            with Mock.lock:
                Mock.inflight -= 1

    def do_GET(self):   # connection warm-up (model metadata)
        self._track(self._get)

    def do_POST(self):
        self._track(self._post)

    def _get(self):
        Mock.gets.append(self.path)
        Mock.get_ports.append(self.client_address[1])
        model = self.path.rsplit('/', 1)[-1]
        info = {'id': model, 'type': 'model'}
        if 'opus' in model:   # like the real Models API for Opus 5.5
            info['capabilities'] = {'thinking': {'types': {
                'adaptive': {'supported': True},
                'disabled': {'supported': False}}}}
        self._send(200, info)

    def _post(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Mock.requests.append({'path': self.path,
                              'port': self.client_address[1],
                              'headers': dict(self.headers), 'body': body})
        n = len(Mock.requests)
        time.sleep(Mock.delay)
        status = Mock.status(n) if Mock.status else (
            401 if Mock.mode == '401' else 200)
        if status != 200:
            return self._send(status, {'type': 'error', 'error': {
                'type': 'authentication_error',
                'message': 'invalid x-api-key'}})
        if Mock.mode == 'refusal':
            return self._stream(body, [], 'refusal')
        if Mock.answer:
            text = Mock.answer(n, body)
        elif 'opus' in body['model']:
            text = f'**Opus** {n}'
        else:
            text = ANSWER if n == 1 else f'Antwort {n}'
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
        self.send_header('Connection', 'close')  # no length: end = close
        self.end_headers()
        self.close_connection = True

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
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# --------------------------------------------------------------- shell

class Shell:
    def __init__(self, tmp, settings):
        self.tmp = tmp
        self.settings = settings      # SCHEMA key -> GVariant text
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.proc = None
        self._gsettings = {}

    def install(self):
        ext = self.tmp / 'data' / 'gnome-shell' / 'extensions'
        dst = ext / UUID
        (dst / 'schemas').mkdir(parents=True)
        for f in EXT_FILES:
            shutil.copy(ROOT / f, dst / f)
        shutil.copy(ROOT / 'schemas' / f'{SCHEMA}.gschema.xml',
                    dst / 'schemas')
        subprocess.run(['glib-compile-schemas', dst / 'schemas'], check=True)
        shutil.copytree(ROOT / 'test' / 'helper', ext / HELPER_UUID)
        self.schemadir = dst / 'schemas'

    # ---- settings: written in-process (no argv, so no secrets in ps) and
    # ---- confirmed by reading them back inside the shell.

    def _settings_for(self, schema):
        if schema not in self._gsettings:
            if schema == SCHEMA:
                source = Gio.SettingsSchemaSource.new_from_directory(
                    str(self.schemadir),
                    Gio.SettingsSchemaSource.get_default(), False)
                self._gsettings[schema] = Gio.Settings.new_full(
                    source.lookup(SCHEMA, False), None, None)
            else:
                self._gsettings[schema] = Gio.Settings.new(schema)
        return self._gsettings[schema]

    def gset(self, key, value, schema=SCHEMA, wait=True):
        """Set key to GVariant text (e.g. "'api'", 'true', "['a']")."""
        s = self._settings_for(schema)
        vtype = s.props.settings_schema.get_key(key).get_value_type()
        variant = GLib.Variant.parse(vtype, value, None, None)
        s.set_value(key, variant)
        Gio.Settings.sync()
        if wait:
            self._wait_setting(schema, key, variant.print_(True))

    def greset(self, key, schema=SCHEMA):
        s = self._settings_for(schema)
        if s.get_user_value(key) is None:
            return
        s.reset(key)
        Gio.Settings.sync()
        self._wait_setting(schema, key, s.get_value(key).print_(True))

    def _wait_setting(self, schema, key, printed):
        wait_for(lambda: self.js(
            f'return zt.setting({json.dumps(schema)}, {json.dumps(key)});')
            == printed, 5, f'shell never saw {schema} {key}={printed}')

    def reset_settings(self):
        """Back to defaults + this run's settings (cheap when unchanged)."""
        s = self._settings_for(SCHEMA)
        for key in s.props.settings_schema.list_keys():
            if key in self.settings:
                if s.get_value(key).print_(True) != GLib.Variant.parse(
                        s.props.settings_schema.get_key(key).get_value_type(),
                        self.settings[key], None, None).print_(True):
                    self.gset(key, self.settings[key])
            else:
                self.greset(key)
        self.greset('color-scheme', 'org.gnome.desktop.interface')

    def start(self):
        for (schema, key), value in BASE_SETTINGS.items():
            self.gset(key, value, schema, wait=False)
        for key, value in self.settings.items():
            self.gset(key, value, wait=False)
        env = dict(os.environ)
        display = env.pop('WAYLAND_DISPLAY')
        self.log = open(self.tmp / 'shell.log', 'w')
        self.proc = subprocess.Popen(
            ['gnome-shell', '--headless', '--wayland',   # Xwayland on demand
             '--wayland-display', display,
             *[a for w, h, _ in SCREENS
               for a in ('--virtual-monitor', f'{w}x{h}')]],
            env=env, stdout=self.log, stderr=subprocess.STDOUT)

        def up():
            if self.proc.poll() is not None:
                raise RuntimeError('gnome-shell exited; see shell.log')
            try:
                return self.js('return typeof zt') == 'object'
            except (GLib.Error, RuntimeError):
                return False  # not on the bus yet / helper not enabled yet
        wait_for(up, 40, lambda: f'shell did not come up: '
                 f'{self.ext_states()}', interval=0.3)
        self._check_isolation()
        if any(scale != 1 for *_, scale in SCREENS) or len(SCREENS) > 1:
            self.configure_monitors()
        # GNOME opens the overview once startup completes; wait for that,
        # then close it (hiding earlier races the startup animation). Drop
        # the test-only "unsafe mode" notification too.
        self.js('''
            const lm = zt.Main.layoutManager;
            await zt.waitFor(() => !lm._startingUp, 20000);
            await zt.sleep(300);
            zt.Main.overview.hide();
            await zt.waitFor(() => !zt.Main.overview.visible);
            zt.Main.messageTray.getSources().forEach(s => s.destroy());
            await zt.sleep(200);
        ''')

    def _check_isolation(self):
        """Positive proof that both sides use the throwaway keyfile."""
        ours = self._settings_for(SCHEMA).props.backend.__gtype__.name
        theirs = self.js('return zt.settingsBackend();')
        if ours != 'GKeyfileSettingsBackend' or \
                theirs != 'GKeyfileSettingsBackend':
            raise RuntimeError(f'settings not isolated: harness={ours}, '
                               f'shell={theirs}')
        if '/org/gnome/shell/extensions/zehntage-gnome/backend' \
                not in keyfile_paths(self.tmp):
            raise RuntimeError('test settings did not reach the keyfile')

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
        logical, x, want = [], 0, []
        for (w, h, scale), mon in zip(SCREENS, monitors):
            mode = next(m for m in mon[1] if m[1] == w and m[2] == h)
            supported = mode[5]
            match = [sc for sc in supported if abs(sc - scale) < 0.01]
            if not match:
                raise RuntimeError(f'scale {scale} unsupported: {supported}')
            logical.append((x, 0, match[0], 0, not logical,
                            [(mon[0][0], mode[0], {})]))
            want.append([x, 0, round(w / match[0]), round(h / match[0])])
            x += round(w / match[0])
        call('ApplyMonitorsConfig', GLib.Variant(
            '(uua(iiduba(ssa{sv}))a{sv})', (serial, 1, logical, {})), None)
        geo = wait_for(lambda: (lambda g: g if [m[:4] for m in g] == want
                                else None)(self.js(
                                    'return zt.Main.layoutManager.monitors'
                                    '.map(m => [m.x, m.y, m.width, m.height,'
                                    ' m.geometry_scale ?? 1]);')),
                       10, f'monitors never became {want}')
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
        """Log messages (with their stack) from our extension, including
        the prefs process (bus.log)."""
        new_msg = re.compile(r'^(\(|\*\*|[\w .-]+-(Message|WARNING|CRITICAL))')
        out = []
        for log in (self.tmp / 'shell.log', OUT / 'bus.log'):
            if not log.exists():
                continue
            lines = log.read_text(errors='replace').splitlines()
            for i, line in enumerate(lines):
                if not any(w in line for w in ('CRITICAL', 'JS ERROR',
                                               'WARNING', 'Warning',
                                               'Error')):
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

def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def wait_for(fn, timeout=8, msg='condition never became true',
             interval=0.05):
    """Poll fn until it returns something truthy; returns that value."""
    end = time.time() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.time() > end:
            raise AssertionError(msg() if callable(msg) else msg)
        time.sleep(interval)


def png_size(b64):
    """Image size of a base64 PNG or JPEG."""
    raw = base64.b64decode(b64)
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return struct.unpack('>II', raw[16:24])
    w, h, _ = decode_png(b64)
    return w, h


def decode_png(b64):
    """-> (width, height, pixel(x, y) -> (r, g, b)). PNG or JPEG."""
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


def cyan(rgb):
    r, g, b = rgb
    return r < 40 and g > 220 and b > 220


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


def first_content(i=-1):
    return Mock.requests[i]['body']['messages'][0]['content']


def last_image(i=-1):
    """The focus image (selection, or the whole monitor for a click)."""
    return [b for b in first_content(i) if b['type'] == 'image'][-1]


def context_image(i=-1):
    """The whole-monitor context image, or None."""
    images = [b for b in first_content(i) if b['type'] == 'image']
    return images[0] if len(images) == 2 else None


def monitor_at(sh, x, y):
    return sh.js(f'''return (m => [m.x, m.y, m.width, m.height])(
        zt.Main.layoutManager.monitors.find(m => {x} >= m.x &&
            {x} < m.x + m.width && {y} >= m.y && {y} < m.y + m.height));''')


def monitor_px(sh, x, y):
    """Expected image size of the monitor under (x, y), downscaled."""
    mx, my, mw, mh = monitor_at(sh, x, y)
    w, h = span(sh, mx, mx + mw), span(sh, my, my + mh)
    k = 1568 / max(w, h)
    return (round(w * k), round(h * k)) if k < 1 else (w, h)


def red(rgb):
    r, g, b = rgb
    return r > 170 and g < 80 and b < 100


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
    busy: (e && i._jobs.get(e.id)?.kind) ?? null,
    partial: (e && i._jobs.get(e.id)?.partial) || null,
    followUpError: e?.followUpError ?? null,
    strongError: e?.strongError ?? null,
};
'''


def state(sh):
    return sh.js(STATE)


def wait_state(sh, pred, timeout=8):
    last = {}

    def probe():
        last['s'] = state(sh)
        return pred(last['s'])
    wait_for(probe, timeout, lambda: f'state never matched; last: '
             f'{last.get("s")}', interval=0.1)
    return last['s']


def entries(sh):
    return sh.js('return zt.inst()._history.entries.map(e => ({'
                 'status: e.status, error: e.error, turns: e.turns, '
                 'imagePath: e.imagePath, model: e.model, '
                 'backend: e.backend ?? null}));')


def hotkey(sh):
    sh.js('await zt.chord(zt.Clutter.KEY_Super_L, zt.Clutter.KEY_z);')


def capture(sh, x1, y1, x2, y2):
    """Hotkey, drag, wait for the new entry to settle; returns state."""
    before = state(sh)['id']
    hotkey(sh)
    wait_state(sh, lambda s: s['selector'])
    sh.js(f'await zt.drag({x1}, {y1}, {x2}, {y2});')
    return wait_state(sh, lambda s: s['id'] != before and
                      s['status'] in ('ok', 'error') and not s['busy'],
                      timeout=15)


def followup(sh, text, focus=True):
    """Type a follow-up question and press Enter."""
    sh.js(('zt.inst()._indicator._focusTarget?.grab_key_focus(); '
           if focus else '') +
          f'await zt.type({json.dumps(text)}); '
          'await zt.chord(zt.Clutter.KEY_Return);')


def labels(sh, style_class=None):
    """Texts of the popup's labels (optionally one style class)."""
    return sh.js(f'''
        return zt.findAll(zt.inst()._indicator.menu.box, a =>
            a instanceof zt.St.Label &&
            ({json.dumps(style_class)} === null ||
             a.style_class === {json.dumps(style_class)}))
            .map(a => a.get_text());''')


def menu_text(sh):
    return '\n'.join(labels(sh))


def press_strong(sh):
    """Click the [Opus] (strong model) button of the expanded entry."""
    sh.js('''
        const [b] = zt.findAll(zt.inst()._indicator.menu.box,
            a => a.style_class?.includes('zehntage-strong'));
        if (!b) throw new Error('no strong-model button');
        b.emit('clicked', 1);''')


def reload_extension(sh, before_disable=''):
    """Disable + enable the extension; returns the modal state while off."""
    sh.js(f'''
        const m = zt.Main.extensionManager;
        {before_disable}
        await m.disableExtension('{UUID}');
        await zt.sleep(300);
        globalThis.__modal = zt.modal();
        await m.enableExtension('{UUID}');
        await zt.sleep(200);
    ''')
    return sh.js('return globalThis.__modal;')


def noise_background(sh, name='noise.png'):
    """Full-stage incompressible noise (big captures, JPEG path)."""
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf
    sw, shh = stage_size(sh)
    w, h = px(sh, sw, shh)
    path = Path(os.environ['ZT_TMP']) / name
    GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(os.urandom(w * h * 3)), GdkPixbuf.Colorspace.RGB,
        False, 8, w, h, w * 3).savev(str(path), 'png', [], [])
    sh.js(f"""const b = new zt.St.Widget({{x: 0, y: 0, width: {sw},
        height: {shh}, style: 'background-image: url("file://{path}");'
            + 'background-size: cover;'}});
        zt.Main.layoutManager.uiGroup.insert_child_above(b,
            zt.Main.layoutManager.panelBox);
        (zt._boxes ??= []).push(b); await zt.sleep(300);""")


def shot(sh, name):
    OUT.mkdir(exist_ok=True)
    sh.js(f'return zt.screenshot({json.dumps(str(OUT / name))});')


def data_dir():
    return Path(os.environ['XDG_DATA_HOME']) / UUID


def fake_calls():
    path = Path(os.environ['ZT_FAKE_DIR']) / 'calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def use_cli(sh, mode='ok'):
    (Path(os.environ['ZT_FAKE_DIR']) / 'mode').write_text(mode)
    sh.gset('backend', "'cli'")


def argval(argv, flag):
    return argv[argv.index(flag) + 1]


def reset(sh):
    """Settle in-flight work, close UI, clear history, mock and settings."""
    Mock.wait_idle()
    sh.js('await zt.waitFor(() => zt.inst()._jobs.size === 0, 10000);')
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
    Mock.reset()
    sh.reset_settings()
    fake = Path(os.environ['ZT_FAKE_DIR'])
    for f in ('mode', 'factcheck', 'calls.jsonl'):
        (fake / f).unlink(missing_ok=True)

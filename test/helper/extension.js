// Test-only helper. Installed solely into the throwaway headless shell that
// test/run.py starts. Enables org.gnome.Shell.Eval (unsafe mode) and exposes
// globalThis.zt with virtual input + screenshot helpers.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot');
Gio._promisify(Shell.Screenshot.prototype, 'screenshot_stage_to_content');

const now = () => GLib.get_monotonic_time();
const sleep = ms => new Promise(resolve =>
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
        resolve();
        return GLib.SOURCE_REMOVE;
    }));

export default class TestHelper extends Extension {
    enable() {
        global.context.unsafe_mode = true;
        const seat = Clutter.get_default_backend().get_default_seat();
        const ptr = seat.create_virtual_device(
            Clutter.InputDeviceType.POINTER_DEVICE);
        const kbd = seat.create_virtual_device(
            Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._devices = [ptr, kbd];

        const key = (keyval, pressed) => kbd.notify_keyval(now(), keyval,
            pressed ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
        const button = (pressed, b = Clutter.BUTTON_PRIMARY) =>
            ptr.notify_button(now(), b,
                pressed ? Clutter.ButtonState.PRESSED
                    : Clutter.ButtonState.RELEASED);

        // Diagnostics: when does the overlay key (Super) fire?
        this._events = [];
        const t0 = Date.now();
        this._overlayId = global.display.connect('overlay-key', () =>
            this._events.push(`${Date.now() - t0}ms overlay-key ` +
                `selector=${globalThis.zt?.inst()?._selector?.active}`));
        this._showingId = Main.overview.connect('showing', () =>
            this._events.push(`${Date.now() - t0}ms overview showing ` +
                new Error().stack.split('\n').slice(4, 8).join(' <- ')));
        const events = this._events;

        globalThis.zt = {
            events,
            Main,
            St,
            Clutter,
            sleep,
            ext: () => Main.extensionManager.lookup('zehntage-gnome@lyka'),
            inst: () => globalThis.zt.ext()?.stateObj,
            press: () => button(true),
            release: () => button(false),
            /**
             * The virtual pointer occasionally lands elsewhere (seen: y
             * clamped to 0), so confirm the position and resend if needed.
             */
            async moveTo(x, y) {
                for (let i = 0; i < 20; i++) {
                    ptr.notify_absolute_motion(now(), x, y);
                    await sleep(10);
                    const [px, py] = global.get_pointer();
                    if (Math.abs(px - x) <= 1 && Math.abs(py - y) <= 1)
                        return;
                }
                throw new Error(`pointer never reached ${x},${y}`);
            },
            async click(x, y, b = Clutter.BUTTON_PRIMARY) {
                await this.moveTo(x, y);
                button(true, b);
                await sleep(30);
                button(false, b);
                await sleep(30);
            },
            async drag(x1, y1, x2, y2, steps = 8) {
                await this.moveTo(x1, y1);
                button(true);
                for (let i = 1; i <= steps; i++) {
                    await this.moveTo(Math.round(x1 + (x2 - x1) * i / steps),
                        Math.round(y1 + (y2 - y1) * i / steps));
                }
                await sleep(30);
                button(false);
                await sleep(30);
            },
            /** Press keyvals in order (a chord), release in reverse. */
            async chord(...keyvals) {
                events.push(`${Date.now() - t0}ms chord ${keyvals}`);
                for (const k of keyvals) {
                    key(k, true);
                    await sleep(20);
                }
                for (const k of [...keyvals].reverse()) {
                    key(k, false);
                    await sleep(20);
                }
            },
            async type(text) {
                for (const ch of text) {
                    await this.chord(Clutter.unicode_to_keysym(
                        ch.codePointAt(0)));
                }
            },
            async screenshot(path) {
                const stream = Gio.MemoryOutputStream.new_resizable();
                await new Shell.Screenshot().screenshot(false, stream);
                stream.close(null);
                GLib.file_set_contents(path,
                    stream.steal_as_bytes().toArray());
                return path;
            },
            /** Big static text to have something meaningful to capture. */
            fixture(text) {
                globalThis.zt._fixture?.destroy();
                const label = new St.Label({
                    text,
                    style: 'font-size: 48px; color: white; ' +
                        'background-color: #1e1e2e; padding: 24px;',
                    x: 200, y: 250,
                });
                Main.layoutManager.uiGroup.insert_child_above(
                    label, Main.layoutManager.panelBox);
                globalThis.zt._fixture = label;
                return [label.x, label.y, label.width, label.height];
            },
            /** Solid colored box at a logical stage position. */
            box(x, y, w, h, color = '#ff00ff') {
                const b = new St.Widget({
                    style: `background-color: ${color};`,
                    x, y, width: w, height: h,
                });
                Main.layoutManager.uiGroup.insert_child_above(
                    b, Main.layoutManager.panelBox);
                (globalThis.zt._boxes ??= []).push(b);
            },
            clearBoxes() {
                for (const b of globalThis.zt._boxes ?? [])
                    b.destroy();
                globalThis.zt._boxes = [];
                globalThis.zt._fixture?.destroy();
                globalThis.zt._fixture = null;
            },
            /** Scale of stage captures — what the extension crops with. */
            async captureScale() {
                const [, scale] =
                    await new Shell.Screenshot().screenshot_stage_to_content();
                return scale;
            },
            /** Modal/grab state, to catch a stuck overlay. */
            modal() {
                return {
                    modalCount: Main.modalCount,
                    actionMode: Main.actionMode,
                    shades: Main.uiGroup.get_children().filter(a =>
                        a.get_children?.().some(c =>
                            c.style_class === 'zehntage-shade')).length,
                };
            },
            async waitFor(fn, timeoutMs = 5000) {
                const end = Date.now() + timeoutMs;
                while (Date.now() < end) {
                    const v = fn();
                    if (v)
                        return v;
                    await sleep(50);
                }
                throw new Error(`waitFor timed out: ${fn}`);
            },
        };
    }

    disable() {
        globalThis.zt?.clearBoxes();
        global.display.disconnect(this._overlayId);
        Main.overview.disconnect(this._showingId);
        globalThis.zt?._fixture?.destroy();
        delete globalThis.zt;
        for (const d of this._devices ?? [])
            d.run_dispose?.();
        this._devices = null;
        global.context.unsafe_mode = false;
    }
}

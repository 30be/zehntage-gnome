// capture.js — red marker + screenshot, one-shot capture.
//
// Two modes (gsettings `interactive-capture`):
//  - false (default): capture the whole screen directly via Shell.Screenshot,
//    no UI — the marker actor is painted into the stage before the shot.
//  - true: the native interactive screenshot UI (area selection etc.).

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

/**
 * Takes one screenshot with a red ring marker placed at the current pointer
 * position so it ends up in the captured frame.
 *
 * Resolves with {file, x, y} (file: Gio.File of the PNG) or null if the
 * capture was dismissed/failed.
 */
export class Capture {
    constructor(settings) {
        this._settings = settings;
        this._marker = null;
        this._takenId = 0;
        this._visibleId = 0;
        this._timeoutId = 0;
        this._busy = false;
    }

    _addMarker(x, y) {
        const size = this._settings.get_int('marker-size');
        const glow = this._settings.get_int('marker-glow');
        this._marker = new St.Widget({
            style_class: 'zehntage-marker',
            width: size,
            height: size,
            x: Math.round(x - size / 2),
            y: Math.round(y - size / 2),
            reactive: false,
        });
        // CSS cannot read gsettings, so the glow is built inline here.
        this._marker.style = [
            'border: 2px solid #dc143c;',
            'border-radius: 999px;',
            'background-color: transparent;',
            `box-shadow: 0 0 ${glow}px ${Math.ceil(glow / 4)}px ` +
                'rgba(220, 20, 60, 0.55);',
        ].join(' ');
        Main.layoutManager.uiGroup.add_child(this._marker);
    }

    _removeMarker() {
        this._marker?.destroy();
        this._marker = null;
    }

    _disconnectAll() {
        if (this._takenId) {
            Main.screenshotUI.disconnect(this._takenId);
            this._takenId = 0;
        }
        if (this._visibleId) {
            Main.screenshotUI.disconnect(this._visibleId);
            this._visibleId = 0;
        }
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
    }

    take() {
        if (this._busy)
            return Promise.resolve(null); // a capture is already in flight
        if (this._settings.get_boolean('interactive-capture'))
            return this._takeInteractive();
        return this._takeFullScreen();
    }

    /** Whole-screen capture, no UI: marker → paint → Shell.Screenshot. */
    _takeFullScreen() {
        this._busy = true;
        const [x, y] = global.get_pointer();
        this._addMarker(x, y);

        return new Promise(resolve => {
            const finish = result => {
                this._disconnectAll();
                this._removeMarker();
                this._busy = false;
                resolve(result);
            };

            // Give the compositor a moment to actually paint the marker
            // before the stage is captured.
            this._timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 50,
                () => {
                    this._timeoutId = 0;
                    this._shoot(x, y).then(finish).catch(e => {
                        console.error(`zehntage-gnome: screenshot failed: ${e}`);
                        finish(null);
                    });
                    return GLib.SOURCE_REMOVE;
                });
        });
    }

    async _shoot(x, y) {
        const stream = Gio.MemoryOutputStream.new_resizable();
        const shooter = new Shell.Screenshot();
        await new Promise((resolve, reject) => {
            shooter.screenshot(false, stream, (s, res) => {
                try {
                    s.screenshot_finish(res);
                    resolve();
                } catch (e) {
                    reject(e);
                }
            });
        });
        stream.close(null);
        const bytes = stream.steal_as_bytes();

        const path = GLib.build_filenamev(
            [GLib.get_tmp_dir(), `zehntage-shot-${Date.now()}.png`]);
        const file = Gio.File.new_for_path(path);
        file.replace_contents(bytes.toArray(), null, false,
            Gio.FileCreateFlags.REPLACE_DESTINATION, null);
        return {file, x, y};
    }

    /** Native interactive screenshot UI (area selection). */
    _takeInteractive() {
        this._busy = true;
        const [x, y] = global.get_pointer();
        this._addMarker(x, y);

        return new Promise(resolve => {
            let done = false;
            const finish = result => {
                if (done)
                    return;
                done = true;
                this._disconnectAll();
                this._removeMarker();
                this._busy = false;
                resolve(result);
            };

            // One-shot: this fires for *our* invocation; the guard flag is the
            // connection itself — we disconnect as soon as we are done, so
            // unrelated user screenshots never reach us.
            this._takenId = Main.screenshotUI.connect(
                'screenshot-taken',
                (_ui, file) => finish({file, x, y}));

            // The UI hides when it closes — if it closes without a shot,
            // clean up and resolve null.
            this._visibleId = Main.screenshotUI.connect(
                'notify::visible', ui => {
                    if (!ui.visible)
                        finish(null);
                });

            Main.screenshotUI.open().catch(e => {
                console.error(`zehntage-gnome: screenshot UI failed: ${e}`);
                finish(null);
            });
        });
    }

    destroy() {
        this._disconnectAll();
        this._removeMarker();
        this._settings = null;
    }
}

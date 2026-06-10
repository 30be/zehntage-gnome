// capture.js — red marker + native screenshot UI, one-shot capture.

import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

/**
 * Takes one screenshot via the native screenshot UI, with a red ring marker
 * placed at the current pointer position so it ends up in the frozen frame.
 *
 * Resolves with {file, x, y} (file: Gio.File of the PNG) or null if the
 * screenshot UI was dismissed without taking a shot.
 */
export class Capture {
    constructor(settings) {
        this._settings = settings;
        this._marker = null;
        this._takenId = 0;
        this._visibleId = 0;
    }

    _addMarker(x, y) {
        const size = this._settings.get_int('marker-size');
        this._marker = new St.Widget({
            style_class: 'zehntage-marker',
            width: size,
            height: size,
            x: Math.round(x - size / 2),
            y: Math.round(y - size / 2),
            reactive: false,
        });
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
    }

    take() {
        if (this._takenId || this._visibleId)
            return Promise.resolve(null); // a capture is already in flight

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

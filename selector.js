// selector.js — freeze the screen, let the user drag a rectangle, crop.
//
// The stage is captured *before* the overlay appears, so menus/tooltips that
// would vanish on grab are still in the picture. Drag = area, single click =
// whole screen, Escape / right click = cancel.

import Clutter from 'gi://Clutter';
import GdkPixbuf from 'gi://GdkPixbuf';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot_stage_to_content');
Gio._promisify(Shell.Screenshot, 'composite_to_stream');

const CLICK_SLOP = 4;           // px; smaller drags count as a click
const MIN_THIN = 24;            // px; a thin drag (underlining) grows to this
const MAX_EDGE = 1568;          // the API downsizes past this anyway, px
// Bigger captures go out as JPEG: a 1568 px screen is ~1 MB as PNG but
// ~80 KB as JPEG (measured ~0.25 s faster to the first token), which also
// keeps every payload far below the API's 5 MB image limit.
const JPEG_OVER = 300_000;

export class AreaSelector {
    constructor() {
        this._overlay = null;
        this._grab = null;
        this._finish = null;
        this._destroyed = false;
    }

    get active() {
        return this._overlay !== null;
    }

    /**
     * @returns {Promise<{bytes: Uint8Array, mediaType: string,
     *   width: number, height: number}|null>} null when cancelled.
     */
    async select() {
        if (this._overlay)
            return null;

        const shooter = new Shell.Screenshot();
        const [content, scale] = await shooter.screenshot_stage_to_content();
        // Disabled while capturing: never put up a modal nobody will pop.
        if (this._destroyed || this._overlay)
            return null;

        const rect = await this._pickRect(content);
        if (!rect)
            return null;

        const texture = content.get_texture();
        const [x, y, w, h] = toTexture(rect, scale,
            texture.get_width(), texture.get_height());
        const stream = Gio.MemoryOutputStream.new_resizable();
        const pixbuf = await Shell.Screenshot.composite_to_stream(
            texture, x, y, w, h, scale,
            null, 0, 0, 1, stream);
        stream.close(null);
        return encode(pixbuf, stream.steal_as_bytes());
    }

    /** Shows the frozen frame and resolves with [x, y, w, h] or null. */
    _pickRect(content) {
        const {width, height} = global.stage;
        const overlay = new St.Widget({
            reactive: true,
            can_focus: true,
            x: 0, y: 0, width, height,
        });
        overlay.add_child(new Clutter.Actor({content, width, height}));
        const shades = [0, 1, 2, 3].map(() => {
            const shade = new St.Widget({style_class: 'zehntage-shade'});
            overlay.add_child(shade);
            return shade;
        });
        const frame = new St.Widget({
            style_class: 'zehntage-selection',
            visible: false,
        });
        overlay.add_child(frame);

        const layout = (x, y, w, h) => {
            shades[0].set({x: 0, y: 0, width, height: y});
            shades[1].set({x: 0, y: y + h, width, height: height - y - h});
            shades[2].set({x: 0, y, width: x, height: h});
            shades[3].set({x: x + w, y, width: width - x - w, height: h});
            frame.set({x, y, width: w, height: h, visible: w > 0 && h > 0});
        };
        layout(0, 0, 0, 0);

        Main.uiGroup.add_child(overlay);
        Main.uiGroup.set_child_above_sibling(overlay, null);
        this._overlay = overlay;
        this._grab = Main.pushModal(overlay,
            {actionMode: Shell.ActionMode.POPUP});
        overlay.grab_key_focus();

        return new Promise(resolve => {
            let start = null;
            let current = null;
            const box = () => {
                const x = Math.min(start[0], current[0]);
                const y = Math.min(start[1], current[1]);
                return [x, y, Math.abs(current[0] - start[0]),
                    Math.abs(current[1] - start[1])];
            };

            this._finish = result => {
                this._finish = null;
                this._close();
                resolve(result);
            };

            overlay.connect('button-press-event', (_a, event) => {
                if (event.get_button() !== Clutter.BUTTON_PRIMARY) {
                    this._finish?.(null);
                    return Clutter.EVENT_STOP;
                }
                start = current = event.get_coords();
                return Clutter.EVENT_STOP;
            });
            overlay.connect('motion-event', (_a, event) => {
                if (start) {
                    current = event.get_coords();
                    layout(...box());
                }
                return Clutter.EVENT_STOP;
            });
            overlay.connect('button-release-event', (_a, event) => {
                if (!start || event.get_button() !== Clutter.BUTTON_PRIMARY)
                    return Clutter.EVENT_STOP;
                current = event.get_coords();
                const [x, y, w, h] = box();
                const isClick = w < CLICK_SLOP && h < CLICK_SLOP;
                this._finish?.(isClick ? [0, 0, width, height] : [x, y, w, h]);
                return Clutter.EVENT_STOP;
            });
            overlay.connect('key-press-event', (_a, event) => {
                if (event.get_key_symbol() === Clutter.KEY_Escape)
                    this._finish?.(null);
                return Clutter.EVENT_STOP;
            });
        });
    }

    _close() {
        if (this._grab) {
            Main.popModal(this._grab);
            this._grab = null;
        }
        this._overlay?.destroy();
        this._overlay = null;
    }

    cancel() {
        this._finish?.(null);
    }

    destroy() {
        this._destroyed = true;
        this.cancel();
        this._close();
    }
}

/**
 * Logical selection -> texture pixel rect. Thin drags grow to MIN_THIN
 * around their middle; the result is clamped to the texture and never empty
 * (an empty or out-of-bounds sub-texture makes composite_to_stream hang).
 */
function toTexture([x, y, w, h], scale, texW, texH) {
    if (w < MIN_THIN) {
        x += (w - MIN_THIN) / 2;
        w = MIN_THIN;
    }
    if (h < MIN_THIN) {
        y += (h - MIN_THIN) / 2;
        h = MIN_THIN;
    }
    // Round both edges (not origin + size) so fractional scales like 1.25
    // never shift the far edge by a pixel.
    const clamp = (v, max) => Math.min(Math.max(Math.round(v), 0), max);
    const x1 = clamp(x * scale, texW - 1);
    const y1 = clamp(y * scale, texH - 1);
    const x2 = Math.max(clamp((x + w) * scale, texW), x1 + 1);
    const y2 = Math.max(clamp((y + h) * scale, texH), y1 + 1);
    return [x1, y1, x2 - x1, y2 - y1];
}

/**
 * Small crops stay PNG (crisp text); big or huge ones are downscaled to
 * MAX_EDGE and sent as JPEG.
 */
function encode(pixbuf, pngBytes) {
    let width = pixbuf.get_width();
    let height = pixbuf.get_height();
    const longEdge = Math.max(width, height);
    if (longEdge <= MAX_EDGE && pngBytes.get_size() <= JPEG_OVER) {
        return {bytes: pngBytes.toArray(), mediaType: 'image/png',
            width, height};
    }

    if (longEdge > MAX_EDGE) {
        const k = MAX_EDGE / longEdge;
        width = Math.max(1, Math.round(width * k));
        height = Math.max(1, Math.round(height * k));
        pixbuf = pixbuf.scale_simple(width, height,
            GdkPixbuf.InterpType.BILINEAR);
    }
    // The JPEG encoder (glycin) rejects RGBA: flatten onto white.
    if (pixbuf.get_has_alpha()) {
        const rgb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, false,
            8, width, height);
        rgb.fill(0xffffffff);
        pixbuf.composite(rgb, 0, 0, width, height, 0, 0, 1, 1,
            GdkPixbuf.InterpType.NEAREST, 255);
        pixbuf = rgb;
    }
    const [, bytes] = pixbuf.save_to_bufferv('jpeg', ['quality'], ['85']);
    return {bytes, mediaType: 'image/jpeg', width, height};
}

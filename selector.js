// selector.js — freeze the screen, let the user drag a rectangle, crop.
//
// The stage is captured *before* the overlay appears, so menus/tooltips that
// would vanish on grab are still in the picture. Drag = area, single click =
// the monitor under the pointer, Escape / right click = cancel.
//
// A drag yields two images: the selected region at full resolution (focus)
// and, for context, its whole monitor with the region outlined in red.

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
const FRAME_RGBA = 0xdc143cff;  // crimson, like the selection frame
const FRAME_PX = 3;             // frame width in the sent context image

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
     * @param {object} opts {context: bool} also return the monitor with the
     *   selection outlined
     * @returns {Promise<{focus: Image, context: Image|null}|null>} null when
     *   cancelled. Image: {bytes, mediaType, width, height}; context also
     *   has rect: [x, y, w, h] of the selection in its pixels.
     */
    async select({context = true} = {}) {
        if (this._overlay)
            return null;

        const shooter = new Shell.Screenshot();
        const [content, scale] = await shooter.screenshot_stage_to_content();
        // Disabled while capturing: never put up a modal nobody will pop.
        if (this._destroyed || this._overlay)
            return null;

        // Capture a monitor while the user is still aiming and dragging:
        // composite_to_stream always PNG-encodes (~0.65 s for a 1080p
        // monitor, ~3 s for 4K), which would otherwise delay the request.
        // It cannot be cancelled, so start it right away only if no
        // abandoned capture is still running (Escape + hotkey again);
        // otherwise at the mouse press, on that monitor.
        const texture = content.get_texture();
        const texW = texture.get_width();
        const texH = texture.get_height();
        const area = rect => toTexture(rect, scale, texW, texH);
        let early = null, earlyShot = null;
        const start = (x, y) => {
            const want = contextArea([x, y, 0, 0]);
            if (early?.join() === want.join())
                return;
            // The drag starts on another monitor than the pointer was on at
            // the hotkey: capture that one instead, unless other captures
            // are still encoding besides ours.
            if (earlyShot && running > 1)
                return;
            early = want;
            earlyShot = captureArea(texture, area(early), scale);
            earlyShot.catch(() => {}); // unused if the drag ends elsewhere
        };
        if (running === 0)
            start(...global.get_pointer());

        const pick = await this._pickRect(content, start);
        if (!pick)
            return null;

        // The selection's monitor (or, if the selection spills onto another
        // monitor, a fresh capture covering both).
        const logical = contextArea(pick.rect);
        const shot = await (early?.join() === logical.join() ? earlyShot
            : captureArea(texture, area(logical), scale));
        if (pick.click)
            return {focus: shotImage(shot), context: null};

        // Selection relative to the capture, cut to it on both edges (a
        // thin drag padded at a monitor edge may poke out).
        const [x, y, w, h] = area(pick.rect);
        const {pixbuf} = shot;
        const W = pixbuf.get_width(), H = pixbuf.get_height();
        const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
        const ax = x - shot.area[0], ay = y - shot.area[1];
        const x1 = clamp(ax, 0, W - 1), y1 = clamp(ay, 0, H - 1);
        const x2 = clamp(ax + w, x1 + 1, W), y2 = clamp(ay + h, y1 + 1, H);
        const sel = [x1, y1, x2 - x1, y2 - y1];
        const focus = encode(pixbuf.new_subpixbuf(...sel).copy());
        return {focus, context: context ? outlined(shot, sel) : null};
    }

    /**
     * Shows the frozen frame; resolves with {rect: [x, y, w, h], click} in
     * logical stage pixels (a click: its point, zero size), or null.
     * onPress(x, y) runs when the drag starts.
     */
    _pickRect(content, onPress) {
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
                onPress?.(...start);
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
                const click = w < CLICK_SLOP && h < CLICK_SLOP;
                this._finish?.({click,
                    rect: click ? [...current, 0, 0] : [x, y, w, h]});
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

/** Logical rect of the monitor under the selection, grown to cover it. */
function contextArea([x, y, w, h]) {
    const cx = x + w / 2, cy = y + h / 2;
    const mon = Main.layoutManager.monitors.find(m =>
        cx >= m.x && cx < m.x + m.width && cy >= m.y && cy < m.y + m.height) ??
        Main.layoutManager.primaryMonitor;
    const x1 = Math.min(mon.x, x), y1 = Math.min(mon.y, y);
    const x2 = Math.max(mon.x + mon.width, x + w);
    const y2 = Math.max(mon.y + mon.height, y + h);
    return [x1, y1, x2 - x1, y2 - y1];
}

/**
 * Captures a texture area: the full-resolution pixbuf, its PNG and a
 * downscaled, flattened (RGB) copy ready for JPEG.
 */
let running = 0; // captures still encoding (they cannot be cancelled)

async function captureArea(texture, area, scale) {
    running++;
    try {
        const stream = Gio.MemoryOutputStream.new_resizable();
        const pixbuf = await Shell.Screenshot.composite_to_stream(
            texture, ...area, scale, null, 0, 0, 1, stream);
        stream.close(null);
        return {pixbuf, png: stream.steal_as_bytes(), area,
            small: shrink(pixbuf)};
    } finally {
        running--;
    }
}

/** A whole capture as the one image (a click). */
function shotImage({pixbuf, png, small}) {
    const width = pixbuf.get_width(), height = pixbuf.get_height();
    if (Math.max(width, height) <= MAX_EDGE && png.get_size() <= JPEG_OVER)
        return {bytes: png.toArray(), mediaType: 'image/png', width, height};
    return jpeg(small);
}

/**
 * The context image: the downscaled capture with a frame drawn just
 * outside the selection (so it covers no content), as JPEG.
 */
function outlined({pixbuf, small}, sel) {
    const image = small.copy();
    const W = image.get_width(), H = image.get_height();
    const k = W / pixbuf.get_width();
    const [x, y, w, h] = sel.map(v => Math.round(v * k));
    // The frame's strips lie outside the selection (1 px gap) by
    // construction; at the image border they are cut, never moved inward.
    const t = FRAME_PX;
    const ox1 = x - t - 1, oy1 = y - t - 1;
    const ox2 = x + w + 1 + t, oy2 = y + h + 1 + t;
    const strip = (sx, sy, sw, sh) => {
        const cx1 = Math.max(sx, 0), cy1 = Math.max(sy, 0);
        const cx2 = Math.min(sx + sw, W), cy2 = Math.min(sy + sh, H);
        if (cx2 > cx1 && cy2 > cy1)  // sub-pixbufs share the parent's pixels
            image.new_subpixbuf(cx1, cy1, cx2 - cx1, cy2 - cy1)
                .fill(FRAME_RGBA);
    };
    strip(ox1, oy1, ox2 - ox1, t);          // top
    strip(ox1, oy2 - t, ox2 - ox1, t);      // bottom
    strip(ox1, oy1, t, oy2 - oy1);          // left
    strip(ox2 - t, oy1, t, oy2 - oy1);      // right
    return {...jpeg(image), rect: [x, y, w, h]};
}

/**
 * Small crops stay PNG (crisp text); big or huge ones are downscaled to
 * MAX_EDGE and sent as JPEG.
 */
function encode(pixbuf) {
    const width = pixbuf.get_width();
    const height = pixbuf.get_height();
    if (Math.max(width, height) <= MAX_EDGE) {
        const [, png] = pixbuf.save_to_bufferv('png', [], []);
        if (png.length <= JPEG_OVER)
            return {bytes: png, mediaType: 'image/png', width, height};
    }
    return toJpeg(pixbuf);
}

function toJpeg(pixbuf) {
    return jpeg(shrink(pixbuf));
}

/** Downscaled to MAX_EDGE and flattened onto white (JPEG needs RGB). */
function shrink(pixbuf) {
    let width = pixbuf.get_width();
    let height = pixbuf.get_height();
    const longEdge = Math.max(width, height);
    if (longEdge > MAX_EDGE) {
        const k = MAX_EDGE / longEdge;
        width = Math.max(1, Math.round(width * k));
        height = Math.max(1, Math.round(height * k));
        pixbuf = pixbuf.scale_simple(width, height,
            GdkPixbuf.InterpType.BILINEAR);
    }
    // The JPEG encoder (glycin) rejects RGBA.
    if (pixbuf.get_has_alpha()) {
        const rgb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, false,
            8, width, height);
        rgb.fill(0xffffffff);
        pixbuf.composite(rgb, 0, 0, width, height, 0, 0, 1, 1,
            GdkPixbuf.InterpType.NEAREST, 255);
        pixbuf = rgb;
    }
    return pixbuf;
}

function jpeg(pixbuf) {
    const [, bytes] = pixbuf.save_to_bufferv('jpeg', ['quality'], ['85']);
    return {bytes, mediaType: 'image/jpeg', width: pixbuf.get_width(),
        height: pixbuf.get_height()};
}

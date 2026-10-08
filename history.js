// history.js — persistent history: history.json + image copies, capped.

import GdkPixbuf from 'gi://GdkPixbuf';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

Gio._promisify(GdkPixbuf.Pixbuf, 'new_from_stream_at_scale_async',
    'new_from_stream_finish');
Gio._promisify(Gio.File.prototype, 'read_async');

// Thumbnail box at 2x, so previews stay sharp on HiDPI.
export const THUMB_W = 640;
export const THUMB_H = 280;

export class History {
    constructor(settings) {
        this._settings = settings;
        this._dir = GLib.build_filenamev(
            [GLib.get_user_data_dir(), 'zehntage-gnome@lyka']);
        GLib.mkdir_with_parents(this._dir, 0o700);
        this.entries = this._load(); // newest first
        this._cancellable = new Gio.Cancellable();
    }

    /**
     * Creates missing thumbnails (Gemini-era entries) off the main thread;
     * decoding full-size PNGs synchronously would freeze the shell.
     */
    async ensureThumbs(onReady) {
        for (const entry of [...this.entries]) {
            if (entry.thumbPath &&
                GLib.file_test(entry.thumbPath, GLib.FileTest.EXISTS))
                continue;
            try {
                const stream = await Gio.File.new_for_path(entry.imagePath)
                    .read_async(GLib.PRIORITY_LOW, this._cancellable);
                const pixbuf = await GdkPixbuf.Pixbuf
                    .new_from_stream_at_scale_async(stream, THUMB_W, THUMB_H,
                        true, this._cancellable);
                stream.close(null);
                if (this._cancellable.is_cancelled())
                    return;
                this._writeThumb(entry, pixbuf);
                this.save();
                onReady(entry);
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
                console.warn(`zehntage-gnome: no thumbnail for ${entry.id}: ${e}`);
            }
        }
    }

    _writeThumb(entry, pixbuf) {
        const thumbPath = entry.imagePath.replace(/\.[^./]+$/, '') +
            '.thumb.png';
        const [, bytes] = pixbuf.save_to_bufferv('png', [], []);
        GLib.file_set_contents(thumbPath, bytes);
        entry.thumbPath = thumbPath;
    }

    get _jsonPath() {
        return GLib.build_filenamev([this._dir, 'history.json']);
    }

    _load() {
        try {
            const [ok, data] = GLib.file_get_contents(this._jsonPath);
            if (!ok)
                return [];
            const list = JSON.parse(new TextDecoder().decode(data));
            if (!Array.isArray(list))
                return [];
            for (const entry of list) {
                // Gemini-era entries: adopt current settings once, and drop
                // errors that were stored as answers back then.
                entry.backend ??= this._settings.get_string('backend');
                entry.model ??= this._settings.get_string('model').trim() ||
                    'claude-haiku-5-5';
                entry.system ??= this._settings.get_string('system-prompt');
                entry.turns = (entry.turns ?? []).filter(t =>
                    !String(t.answer ?? '').startsWith('⚠'));
                // A shell restart mid-request leaves entries stuck pending.
                if (entry.status === 'pending') {
                    entry.status = 'error';
                    entry.error = 'Interrupted';
                }
                delete entry.followUpPending;
            }
            return list;
        } catch {
            return [];
        }
    }

    save() {
        try {
            GLib.file_set_contents(this._jsonPath,
                JSON.stringify(this.entries, null, 1));
        } catch (e) {
            console.error(`zehntage-gnome: failed to save history: ${e}`);
        }
    }

    /**
     * Stores the screenshot and returns the new entry.
     *
     * @param {object} shot {bytes, mediaType}
     * @param {object} meta {backend, model, system} pinned for the whole
     *   conversation
     */
    addEntry(shot, {backend, model, system}) {
        const id = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
        const ext = shot.mediaType === 'image/jpeg' ? 'jpg' : 'png';
        const imagePath = GLib.build_filenamev([this._dir, `${id}.${ext}`]);
        GLib.file_set_contents(imagePath, shot.bytes);

        const entry = {
            id,
            time: new Date().toISOString(),
            imagePath,
            mediaType: shot.mediaType,
            backend,
            model,
            system,
            turns: [],          // [{question?, answer, content}]
            status: 'pending',  // pending | ok | error
            error: null,
        };
        try {
            const loader = new GdkPixbuf.PixbufLoader();
            loader.write_bytes(new GLib.Bytes(shot.bytes));
            loader.close();
            const full = loader.get_pixbuf();
            const k = Math.min(1, THUMB_W / full.get_width(),
                THUMB_H / full.get_height());
            this._writeThumb(entry, k < 1
                ? full.scale_simple(Math.max(1, Math.round(full.get_width() * k)),
                    Math.max(1, Math.round(full.get_height() * k)),
                    GdkPixbuf.InterpType.BILINEAR)
                : full);
        } catch (e) {
            console.warn(`zehntage-gnome: thumbnail failed: ${e}`);
        }
        this.entries.unshift(entry);
        this._evict();
        this.save();
        return entry;
    }

    _evict() {
        const cap = Math.max(1, this._settings.get_int('history-size'));
        while (this.entries.length > cap) {
            const old = this.entries.pop();
            for (const path of [old.imagePath, old.thumbPath]) {
                try {
                    if (path)
                        Gio.File.new_for_path(path).delete(null);
                } catch {
                    // already gone
                }
            }
        }
    }

    loadImageBytes(entry) {
        const [ok, data] = GLib.file_get_contents(entry.imagePath);
        if (!ok)
            throw new Error('Screenshot file missing');
        return data;
    }

    destroy() {
        this._cancellable.cancel();
        this.save();
        this._settings = null;
    }
}

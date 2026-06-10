// history.js — persistent history: history.json + PNG copies, capped.

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

export class History {
    constructor(settings) {
        this._settings = settings;
        this._dir = GLib.build_filenamev(
            [GLib.get_user_data_dir(), 'zehntage-gnome@lyka']);
        GLib.mkdir_with_parents(this._dir, 0o700);
        this.entries = this._load(); // newest first
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
            return Array.isArray(list) ? list : [];
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

    /** Copies the screenshot into the history dir; returns the new entry. */
    addEntry(sourceFile) {
        const id = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
        const imagePath = GLib.build_filenamev([this._dir, `${id}.png`]);
        sourceFile.copy(Gio.File.new_for_path(imagePath),
            Gio.FileCopyFlags.OVERWRITE, null, null);

        const entry = {
            id,
            time: new Date().toISOString(),
            imagePath,
            turns: [],          // [{question?: string, answer?: string}]
            status: 'pending',  // pending | ok | error
            error: null,
        };
        this.entries.unshift(entry);
        this._evict();
        this.save();
        return entry;
    }

    _evict() {
        const cap = Math.max(1, this._settings.get_int('history-size'));
        while (this.entries.length > cap) {
            const old = this.entries.pop();
            try {
                Gio.File.new_for_path(old.imagePath).delete(null);
            } catch {
                // already gone
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
        this.save();
        this._settings = null;
    }
}

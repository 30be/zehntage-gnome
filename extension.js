// zehntage-gnome — context-aware screen assistant.
// Hotkey → native screenshot UI (red marker at cursor) → Gemini → panel popup.

import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {Capture} from './capture.js';
import {GeminiClient} from './gemini.js';
import {History} from './history.js';
import {Indicator} from './indicator.js';

const KEYBINDING = 'capture-hotkey';

export default class ZehntageExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._gemini = new GeminiClient(this._settings);
        this._history = new History(this._settings);
        this._capture = new Capture(this._settings);
        this._timeoutId = 0;

        this._indicator = new Indicator({
            onCapture: () => this._captureFromMenu(),
            onFollowUp: (entry, q) => this._followUp(entry, q),
            onRetry: entry => this._retry(entry),
            onOpenPrefs: () => this.openPreferences(),
            hasApiKey: () => this._gemini.hasApiKey,
        });
        this._indicator.setEntries(this._history.entries);
        Main.panel.addToStatusArea(this.uuid, this._indicator);

        Main.wm.addKeybinding(KEYBINDING, this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this._startCapture());
    }

    disable() {
        Main.wm.removeKeybinding(KEYBINDING);
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
        this._capture?.destroy();
        this._capture = null;
        this._gemini?.destroy();
        this._gemini = null;
        this._history?.destroy();
        this._history = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._settings = null;
    }

    _captureFromMenu() {
        // Close the menu first, then capture once the close animation is done
        // so the popup is not part of the screenshot.
        this._indicator.menu.close();
        if (this._timeoutId)
            GLib.source_remove(this._timeoutId);
        this._timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 350, () => {
            this._timeoutId = 0;
            this._startCapture();
            return GLib.SOURCE_REMOVE;
        });
    }

    _startCapture() {
        if (!this._gemini.hasApiKey) {
            this._indicator.openWith(this._history.entries);
            return;
        }
        this._capture.take().then(result => {
            if (result)
                this._handleShot(result.file).catch(logError);
        }).catch(logError);
    }

    async _handleShot(file) {
        const entry = this._history.addEntry(file);
        this._indicator.openWith(this._history.entries);
        await this._askInitial(entry);
    }

    async _askInitial(entry) {
        entry.status = 'pending';
        entry.error = null;
        this._indicator.refresh();
        try {
            const answer = await this._gemini.generate(
                this._buildContents(entry));
            entry.turns = [{answer}, ...entry.turns.slice(1)];
            entry.status = 'ok';
        } catch (e) {
            entry.status = 'error';
            entry.error = String(e.message ?? e);
        }
        this._history.save();
        this._indicator.refresh();
    }

    _retry(entry) {
        this._askInitial(entry).catch(logError);
    }

    /** Multi-turn contents: prompt+image, then alternating answers/questions. */
    _buildContents(entry, pendingQuestion = null) {
        const prompt = this._settings.get_string('prompt');
        const png = this._history.loadImageBytes(entry);
        const contents = [{
            role: 'user',
            parts: [{text: prompt}, this._gemini.imagePart(png)],
        }];
        for (const turn of entry.turns) {
            if (turn.question)
                contents.push({role: 'user', parts: [{text: turn.question}]});
            if (turn.answer)
                contents.push({role: 'model', parts: [{text: turn.answer}]});
        }
        if (pendingQuestion)
            contents.push({role: 'user', parts: [{text: pendingQuestion}]});
        return contents;
    }

    async _followUp(entry, question) {
        entry.followUpPending = true;
        this._indicator.refresh();
        try {
            const answer = await this._gemini.generate(
                this._buildContents(entry, question));
            entry.turns.push({question, answer});
        } catch (e) {
            entry.turns.push({question, answer: `⚠ ${e.message ?? e}`});
        }
        delete entry.followUpPending;
        this._history.save();
        this._indicator.refresh();
    }
}

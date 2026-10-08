// zehntage-gnome — screen assistant.
// Hotkey → frozen screen, drag an area → Claude explains it → panel popup.

import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as BoxPointer from 'resource:///org/gnome/shell/ui/boxpointer.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {ClaudeClient} from './claude.js';
import {CliClient} from './cli.js';
import {History} from './history.js';
import {Indicator} from './indicator.js';
import {AreaSelector} from './selector.js';

const KEYBINDING = 'capture-hotkey';
const MENU_SETTLE_MS = 120; // let a closed menu disappear from the frame

export default class ZehntageExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._claude = new ClaudeClient(this._settings);
        this._cli = new CliClient(this._settings);
        this._history = new History(this._settings);
        this._selector = new AreaSelector();
        this._timeoutId = 0;

        this._indicator = new Indicator({
            onCapture: () => this._startCapture(),
            onFollowUp: (entry, q) => {
                if (entry.followUpPending)
                    return false;
                this._followUp(entry, q).catch(logError);
                return true;
            },
            onRetry: entry => this._retry(entry),
            onOpenPrefs: () => this.openPreferences(),
            setupHint: () => this._setupHint(),
        });
        this._indicator.setEntries(this._history.entries);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._history.ensureThumbs(() => this._indicator?.refresh())
            .catch(logError);

        Main.wm.addKeybinding(KEYBINDING, this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW |
                Shell.ActionMode.POPUP,
            () => this._startCapture());
    }

    disable() {
        Main.wm.removeKeybinding(KEYBINDING);
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
        this._selector?.destroy();
        this._selector = null;
        this._claude?.destroy();
        this._claude = null;
        this._cli?.destroy();
        this._cli = null;
        this._history?.destroy();
        this._history = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._settings = null;
    }

    get _backend() {
        return this._settings.get_string('backend');
    }

    /** What the user must fix before capturing, or null when ready. */
    _setupHint() {
        if (this._backend === 'cli') {
            return this._cli.available ? null
                : 'claude CLI not found (set its path in settings).';
        }
        return this._claude.hasApiKey ? null : 'Claude API key is not set.';
    }

    /** Entry point for the hotkey and the menu item. */
    _startCapture() {
        if (this._selector.active || this._timeoutId)
            return;
        if (this._setupHint()) {
            this._indicator.openWith(this._history.entries);
            return;
        }
        // Our own popup must not end up in the frozen frame.
        if (this._indicator.menu.isOpen) {
            this._indicator.menu.close(BoxPointer.PopupAnimation.NONE);
            this._timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT,
                MENU_SETTLE_MS, () => {
                    this._timeoutId = 0;
                    this._selectSafely();
                    return GLib.SOURCE_REMOVE;
                });
            return;
        }
        this._selectSafely();
    }

    /** Capture failures must be visible, not just logged. */
    _selectSafely() {
        this._select().catch(e => {
            logError(e, 'zehntage-gnome: capture failed');
            Main.notifyError('Zehntage: capture failed', String(e.message ?? e));
        });
    }

    async _select() {
        const shot = await this._selector.select();
        if (!shot || !this._history)
            return;
        const entry = this._history.addEntry(shot, {
            backend: this._backend,
            model: this._claude.model,
            system: this._settings.get_string('system-prompt'),
        });
        this._indicator.openWith(this._history.entries);
        await this._askInitial(entry);
    }

    async _askInitial(entry) {
        entry.status = 'pending';
        entry.error = null;
        entry.turns = [];
        this._indicator.refresh();
        try {
            const {content, text} = await this._ask(entry);
            entry.turns = [{answer: text, content}];
            entry.status = 'ok';
        } catch (e) {
            entry.status = 'error';
            entry.error = String(e.message ?? e);
        }
        this._history?.save();
        this._indicator?.refresh();
    }

    _retry(entry) {
        this._askInitial(entry).catch(logError);
    }

    /** Sends via the backend the entry was created with. */
    _ask(entry, question = null) {
        if (entry.backend === 'cli') {
            return this._cli.send({
                model: entry.model,
                system: entry.system,
                image: {
                    bytes: this._history.loadImageBytes(entry),
                    mediaType: entry.mediaType ?? 'image/png',
                },
                turns: entry.turns,
                question,
            });
        }
        return this._claude.send(this._request(entry, question));
    }

    /**
     * API backend, append-only conversation: image, then each answer verbatim (thinking
     * blocks included — Haiku 5.5 rejects edited history), then questions.
     */
    _request(entry, pendingQuestion = null) {
        const image = this._claude.imageBlock(
            this._history.loadImageBytes(entry),
            entry.mediaType ?? 'image/png');
        const messages = [{role: 'user', content: [image]}];
        for (const turn of entry.turns) {
            if (turn.question)
                messages.push({role: 'user', content: turn.question});
            messages.push({
                role: 'assistant',
                content: turn.content ?? [{type: 'text', text: turn.answer}],
            });
        }
        if (pendingQuestion)
            messages.push({role: 'user', content: pendingQuestion});
        return {
            model: entry.model ?? this._claude.model,
            system: entry.system ?? this._settings.get_string('system-prompt'),
            messages,
        };
    }

    async _followUp(entry, question) {
        if (entry.followUpPending)
            return;
        entry.followUpPending = true;
        delete entry.followUpError;
        this._indicator.refresh();
        try {
            const {content, text} = await this._ask(entry, question);
            entry.turns.push({question, answer: text, content});
        } catch (e) {
            // Shown in the UI but never sent back to the API.
            entry.followUpError = `${question}: ${e.message ?? e}`;
        }
        delete entry.followUpPending;
        this._history?.save();
        this._indicator?.refresh();
    }
}

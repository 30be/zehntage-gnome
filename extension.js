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
                if (entry.followUpPending || entry.upgradePending)
                    return false;
                this._followUp(entry, q).catch(logError);
                return true;
            },
            onRetry: entry => this._retry(entry),
            onUpgrade: entry => this._upgrade(entry).catch(logError),
            onOpenPrefs: () => this.openPreferences(),
            setupHint: () => this._setupHint(),
            strongModel: () => this._strong.model,
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
        return this._claude.hasApiKey ? null
            : 'Claude API key is not set (or switch the backend to the ' +
              'Claude Code CLI in settings).';
    }

    /** Entry point for the hotkey and the menu item. */
    _startCapture() {
        if (this._selector.active || this._timeoutId)
            return;
        if (this._setupHint()) {
            this._indicator.openWith(this._history.entries);
            return;
        }
        // TLS handshake happens while the user is still selecting.
        if (this._backend === 'api')
            this._claude.warmUp();
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

    async _askInitial(entry, {strong = false} = {}) {
        entry.status = 'pending';
        entry.error = null;
        entry.turns = [];
        this._indicator.refresh();
        try {
            const {content, text, model} = await this._ask(entry, null,
                partial => {
                    entry.partial = partial;
                    this._indicator?.updateLive(entry);
                }, {strong});
            entry.turns = [{answer: text, content, ...strong ? {model} : {}}];
            entry.status = 'ok';
        } catch (e) {
            entry.status = 'error';
            entry.error = String(e.message ?? e);
        }
        delete entry.partial;
        this._history?.save();
        this._indicator?.refresh();
    }

    _retry(entry) {
        this._askInitial(entry).catch(logError);
    }

    /** [Opus]: re-ask the last question with the strong model, replace it. */
    async _upgrade(entry) {
        if (entry.status === 'pending' || entry.followUpPending ||
            entry.upgradePending)
            return;
        if (entry.status === 'error' || entry.turns.length === 0) {
            await this._askInitial(entry, {strong: true});
            return;
        }
        const last = entry.turns.length - 1;
        const question = entry.turns[last].question ?? null;
        entry.upgradePending = true;
        delete entry.upgradeError;
        this._indicator.refresh();
        try {
            const {content, text, model} = await this._ask(entry, question,
                partial => {
                    entry.partialUpgrade = partial;
                    this._indicator?.updateLive(entry);
                }, {turns: entry.turns.slice(0, last), strong: true});
            entry.turns[last] = {...question ? {question} : {},
                answer: text, content, model};
        } catch (e) {
            // The previous answer stays; show why the upgrade failed.
            entry.upgradeError = String(e.message ?? e);
        }
        delete entry.upgradePending;
        delete entry.partialUpgrade;
        this._history?.save();
        this._indicator?.refresh();
    }

    get _strong() {
        return {
            model: this._settings.get_string('strong-model').trim() ||
                'claude-opus-5-5',
            effort: this._settings.get_string('strong-effort'),
            thinking: true,
        };
    }

    /**
     * Sends via the backend the entry was created with. Resolves with
     * {content, text, model}.
     */
    async _ask(entry, question = null, onText = null,
        {turns = entry.turns, strong = false} = {}) {
        const {model, effort, thinking} = strong ? this._strong
            : {model: entry.model ?? this._claude.model};
        if (entry.backend === 'cli') {
            const {text} = await this._cli.send({
                model,
                effort,
                system: entry.system,
                image: {
                    bytes: this._history.loadImageBytes(entry),
                    mediaType: entry.mediaType ?? 'image/png',
                },
                turns,
                question,
            });
            return {text, model};
        }
        const {content, text} = await this._claude.send({
            ...this._request(entry, question, turns, model),
            effort,
            thinking,
        }, onText);
        return {content, text, model};
    }

    /**
     * API backend, append-only conversation: image, then each answer
     * verbatim (thinking blocks included — Haiku 5.5 rejects edited
     * history), then questions. Thinking blocks are bound to the model that
     * wrote them, so answers from another model (Opus rewrites) are replayed
     * as plain text.
     */
    _request(entry, pendingQuestion = null, turns = entry.turns,
        model = entry.model ?? this._claude.model) {
        const image = this._claude.imageBlock(
            this._history.loadImageBytes(entry),
            entry.mediaType ?? 'image/png');
        const messages = [{role: 'user', content: [image]}];
        for (const turn of turns) {
            if (turn.question)
                messages.push({role: 'user', content: turn.question});
            const sameModel = (turn.model ?? entry.model) === model;
            messages.push({
                role: 'assistant',
                content: sameModel && turn.content ? turn.content
                    : [{type: 'text', text: turn.answer}],
            });
        }
        if (pendingQuestion)
            messages.push({role: 'user', content: pendingQuestion});
        return {
            model,
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
            const {content, text} = await this._ask(entry, question,
                partial => {
                    entry.partialFollowUp = partial;
                    this._indicator?.updateLive(entry);
                });
            entry.turns.push({question, answer: text, content});
        } catch (e) {
            // Shown in the UI but never sent back to the API.
            entry.followUpError = `${question}: ${e.message ?? e}`;
        }
        delete entry.followUpPending;
        delete entry.partialFollowUp;
        this._history?.save();
        this._indicator?.refresh();
    }
}

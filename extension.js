// zehntage-gnome — screen assistant.
// Hotkey → frozen screen, drag an area → Claude explains it → panel popup.

import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as BoxPointer from 'resource:///org/gnome/shell/ui/boxpointer.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {ClaudeClient, DEFAULT_STRONG_MODEL, imageBlock} from './claude.js';
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
        // entry id -> in-flight job {kind, partial}; one job per entry.
        // Kept out of the entries so history.json never stores it.
        this._jobs = new Map();

        this._indicator = new Indicator({
            onCapture: () => this._startCapture(),
            onFollowUp: (entry, q) => this._followUp(entry, q),
            onRetry: entry => this._askInitial(entry),
            onStrong: entry => this._askStrong(entry),
            onOpenPrefs: () => this.openPreferences(),
            setupHint: () => this._setupHint(),
            strongModel: () => this._strong.model,
            busy: entry => this._jobs?.get(entry.id) ?? null,
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
        this._jobs = null;
        this._settings = null;
    }

    get _backend() {
        return this._settings.get_string('backend');
    }

    get _strong() {
        return {
            model: this._settings.get_string('strong-model').trim() ||
                DEFAULT_STRONG_MODEL,
            effort: this._settings.get_string('strong-effort'),
            thinking: true,
        };
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
            this._indicator.open();
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
                    this._select();
                    return GLib.SOURCE_REMOVE;
                });
            return;
        }
        this._select();
    }

    /** Capture failures must be visible, not just logged. */
    async _select() {
        try {
            const shot = await this._selector.select({
                context: this._settings.get_boolean('context'),
            });
            if (!shot || !this._history)
                return;
            const entry = this._history.addEntry(shot, {
                backend: this._backend,
                model: this._claude.model,
                system: this._settings.get_string('system-prompt'),
                suffix: this._settings.get_string('followup-suffix').trim(),
            });
            this._indicator.open();
            this._askInitial(entry);
        } catch (e) {
            logError(e, 'zehntage-gnome: capture failed');
            Main.notifyError('Zehntage: capture failed',
                String(e.message ?? e));
        }
    }

    /**
     * Runs one request for an entry: streams partial text into the popup,
     * then applies the result or the error, saves and re-renders.
     *
     * @returns {boolean} false if the entry already has a request in flight
     */
    _run(entry, kind, ask, onDone, onError) {
        if (this._jobs.has(entry.id))
            return false;
        const jobs = this._jobs;
        const job = {kind, partial: ''};
        jobs.set(entry.id, job);
        this._indicator.refresh();
        ask(partial => {
            job.partial = partial;
            this._indicator?.updateLive(entry);
        }).then(onDone, e => onError(String(e.message ?? e)))
            .catch(logError)
            .finally(() => {
                // A re-enable starts a fresh map; never touch its jobs.
                if (jobs.get(entry.id) === job)
                    jobs.delete(entry.id);
                this._history?.save();
                this._indicator?.refresh();
            });
        return true;
    }

    /** First answer (also Retry; strong: the [Opus] button on an error). */
    _askInitial(entry, strong = false) {
        if (this._jobs.has(entry.id))
            return false;
        entry.status = 'pending';
        entry.error = null;
        entry.turns = [];
        this._clearErrors(entry);
        return this._run(entry, strong ? 'strong' : 'initial',
            onText => this._ask(entry, {strong, onText}),
            ({content, text, model}) => {
                entry.turns = [{answer: text, content,
                    ...strong ? {model} : {}}];
                entry.status = 'ok';
            },
            error => {
                entry.status = 'error';
                entry.error = error;
            });
    }

    _followUp(entry, question) {
        if (this._jobs.has(entry.id))
            return false;
        this._clearErrors(entry);
        return this._run(entry, 'followUp',
            onText => this._ask(entry, {question, onText}),
            ({content, text}) => entry.turns.push({question, answer: text,
                content}),
            error => {
                // Shown in the UI but never sent back to the API; [Opus]
                // can answer it instead.
                entry.followUpError = `${question}: ${error}`;
                entry.failedQuestion = question;
            });
    }

    /**
     * [Opus]: re-ask with the strong model. Answers a just-failed follow-up
     * if there is one, otherwise replaces the last answer.
     */
    _askStrong(entry) {
        if (entry.status === 'error' || entry.turns.length === 0)
            return this._askInitial(entry, true);
        if (this._jobs.has(entry.id))
            return false;
        const failed = entry.failedQuestion;
        const last = entry.turns.length - 1;
        const question = failed ?? entry.turns[last].question ?? null;
        const turns = failed ? entry.turns : entry.turns.slice(0, last);
        this._clearErrors(entry);
        return this._run(entry, 'strong',
            onText => this._ask(entry, {question, turns, strong: true,
                onText}),
            ({content, text, model}) => {
                const turn = {...question ? {question} : {}, answer: text,
                    content, model};
                if (failed)
                    entry.turns.push(turn);
                else
                    entry.turns[last] = turn;
            },
            error => {
                // The previous answer stays; show why the rewrite failed.
                entry.strongError = failed ? `${failed}: ${error}` : error;
                if (failed)
                    entry.failedQuestion = failed;
            });
    }

    _clearErrors(entry) {
        delete entry.followUpError;
        delete entry.failedQuestion;
        delete entry.strongError;
    }

    /**
     * Sends via the backend the entry was created with.
     *
     * @returns {Promise<{content?, text, model}>}
     */
    async _ask(entry, {question = null, turns = entry.turns, strong = false,
        onText}) {
        const {model, effort, thinking} = strong ? this._strong
            : {model: entry.model};
        const screenshot = this._screenshotContent(entry);
        if (entry.backend === 'cli') {
            const {text} = await this._cli.send({model, effort,
                system: entry.system, content: screenshot, turns, question,
                suffix: entry.suffix}, onText);
            return {text, model};
        }
        const {content, text} = await this._claude.send({
            model,
            effort,
            thinking,
            system: entry.system,
            messages: this._messages(entry, screenshot, turns, question,
                model),
        }, onText);
        return {content, text, model};
    }

    /**
     * The first user message: the whole monitor with the selection
     * outlined in red (for context) plus the selection at full resolution;
     * or just the one image for full-screen captures and older entries.
     */
    _screenshotContent(entry) {
        const focus = imageBlock(this._history.loadImageBytes(entry),
            entry.mediaType);
        if (!entry.contextPath)
            return [focus];
        const [x, y, w, h] = entry.rect;
        return [
            {type: 'text', text: 'Image 1: the whole screen. The red ' +
                `rectangle (x=${x}, y=${y}, width=${w}, height=${h} in ` +
                'pixels of this image) marks the selected region.'},
            imageBlock(this._history.loadImageBytes(entry, true),
                entry.contextMediaType),
            {type: 'text', text: 'Image 2: the selected region at full ' +
                'resolution.'},
            focus,
        ];
    }

    /**
     * API conversation, append-only: image, then each answer verbatim
     * (thinking blocks included — Haiku 5.5 rejects edited history), then
     * the question. Thinking blocks are bound to the model that wrote them,
     * so answers from another model (strong rewrites) go back as text.
     */
    _messages(entry, screenshot, turns, question, model) {
        const messages = [{role: 'user', content: screenshot}];
        // Follow-ups get the entry's suffix ("(ответ по-русски)"): without
        // it Haiku answers in the question's language. Same text on every
        // replay, so the history stays append-only.
        const ask = q => entry.suffix ? `${q}\n\n${entry.suffix}` : q;
        for (const turn of turns) {
            if (turn.question)
                messages.push({role: 'user', content: ask(turn.question)});
            const sameModel = (turn.model ?? entry.model) === model;
            messages.push({
                role: 'assistant',
                content: sameModel && turn.content ? turn.content
                    : [{type: 'text', text: turn.answer}],
            });
        }
        if (question)
            messages.push({role: 'user', content: ask(question)});
        return messages;
    }
}

// indicator.js — top-bar button with scrollable history popup.

import Clutter from 'gi://Clutter';
import Cogl from 'gi://Cogl';
import GdkPixbuf from 'gi://GdkPixbuf';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {THUMB_H, THUMB_W} from './history.js';

const THINKING = 'Thinking…';

// Inline Markdown, in order (code first so its contents stay unstyled):
// [pattern, Pango tag]. The pattern's last group is the text; an optional
// first group is a preceding character that must be kept.
const INLINE_MD = [
    [/`([^`\n]+)`/g, 'tt'],
    [/\*\*([^*\n]+)\*\*/g, 'b'],
    [/__([^_\n]+)__/g, 'b'],
    [/(^|[^*])\*([^*\n]+)\*(?!\*)/g, 'i'],
    [/(^|[^_\w])_([^_\n]+)_(?!\w)/g, 'i'],
];

function inlineMd(text, wrap) {
    for (const [re, tag] of INLINE_MD) {
        text = text.replace(re, (...m) => {
            const groups = m.slice(1, -2);
            const inner = groups.pop();
            return `${groups[0] ?? ''}${wrap(tag, inner)}`;
        });
    }
    return text;
}

/** Plain text of a Markdown line (collapsed history rows). */
function stripMd(text) {
    return inlineMd(text, (_tag, inner) => inner).replace(/^#{1,6}\s+/, '');
}

/** Minimal Markdown → Pango markup converter (defensive). */
function mdToPango(text) {
    const s = inlineMd(GLib.markup_escape_text(text, -1),
        (tag, inner) => `<${tag}>${inner}</${tag}>`);
    // Headers and bullets, per line.
    return s.split('\n').map(line => {
        const h = line.match(/^#{1,6}\s+(.*)$/);
        if (h)
            return `<b>${h[1]}</b>`;
        return line.replace(/^(\s*)[-*]\s+/, '$1• ');
    }).join('\n');
}

function wrappedLabel(text, styleClass, markdown = false) {
    const label = new St.Label({text, style_class: styleClass});
    label.clutter_text.line_wrap = true;
    label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    if (markdown)
        setMarkdown(label, text);
    return label;
}

function setMarkdown(label, text) {
    // set_markup() never throws on bad markup (it just logs), so check
    // first and fall back to plain text. Partial (streaming) Markdown such
    // as an unclosed ** simply renders literally until it closes.
    const markup = mdToPango(text);
    try {
        Pango.parse_markup(markup, -1, '');
        label.clutter_text.set_markup(markup);
    } catch {
        label.clutter_text.set_text(text);
    }
}

function button(label, onClick, extraClass = '') {
    const b = new St.Button({
        label,
        style_class: `button zehntage-button ${extraClass}`.trim(),
        x_align: Clutter.ActorAlign.START,
    });
    b.connect('clicked', onClick);
    return b;
}

/** 'claude-opus-5-5' -> 'Opus 5.5' (falls back to the raw id). */
export function modelName(id) {
    const m = /^claude-([a-z]+)-(\d+(?:-\d+)*)$/.exec(id ?? '');
    if (!m)
        return id ?? '';
    const family = `${m[1][0].toUpperCase()}${m[1].slice(1)}`;
    return `${family} ${m[2].replaceAll('-', '.')}`;
}

/**
 * A menu row that never activates but is not drawn as disabled: inactive
 * PopupBaseMenuItems become non-reactive, and St dims non-reactive widgets
 * (:insensitive), which made answers hard to read.
 */
const StaticItem = GObject.registerClass(
class ZehntageStaticItem extends PopupMenu.PopupBaseMenuItem {
    _init() {
        super._init({activate: false, hover: false, can_focus: false});
        this.remove_style_class_name('popup-inactive-menu-item');
    }

    getSensitive() {
        return this._parent?.sensitive ?? true;
    }

    syncSensitive() {
        const sensitive = super.syncSensitive();
        this.can_focus = false; // focus belongs to the follow-up entry
        return sensitive;
    }
});

export const Indicator = GObject.registerClass(
class ZehntageIndicator extends PanelMenu.Button {
    /**
     * @param {object} cb callbacks: onCapture(), onFollowUp(entry, q) ->
     *   bool (false: busy, keep the text), onRetry(entry), onStrong(entry),
     *   onOpenPrefs(), setupHint() -> string|null, strongModel() -> id,
     *   busy(entry) -> {kind: 'initial'|'followUp'|'strong', partial}|null
     */
    _init(cb) {
        super._init(0.5, 'Zehntage');
        this._cb = cb;
        this._entries = [];
        this._expandedId = null;
        this._thumbs = new Map(); // thumbPath -> St.ImageContent | null
        this._drafts = new Map(); // entry id -> unsent follow-up text
        this._live = null;        // {id, label, streaming} being streamed into
        this._focusTarget = null; // follow-up entry to focus after a render

        this.add_child(new St.Icon({
            icon_name: 'camera-photo-symbolic',
            style_class: 'system-status-icon',
        }));

        const captureItem = new PopupMenu.PopupImageMenuItem(
            'Capture & explain', 'camera-photo-symbolic');
        captureItem.connect('activate', () => this._cb.onCapture());
        const prefsButton = new St.Button({
            child: new St.Icon({
                icon_name: 'emblem-system-symbolic',
                icon_size: 16,
            }),
            style_class: 'button zehntage-prefs-button',
            x_align: Clutter.ActorAlign.END,
            x_expand: true,
        });
        prefsButton.connect('clicked', () => this._openPrefs());
        captureItem.add_child(prefsButton);
        this.menu.addMenuItem(captureItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Scrollable history.
        this._historySection = new PopupMenu.PopupMenuSection();
        const scrollView = new St.ScrollView({
            style_class: 'zehntage-scroll',
            overlay_scrollbars: true,
        });
        scrollView.child = this._historySection.actor;
        const scrollItem = new PopupMenu.PopupMenuSection();
        scrollItem.actor.add_child(scrollView);
        this.menu.addMenuItem(scrollItem);

        this.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this._render();
        });
    }

    /** The history list (newest first); the newest entry is expanded. */
    setEntries(entries) {
        this._entries = entries;
        this._expandedId = entries[0]?.id ?? null;
        this.refresh();
    }

    /** Show the popup with the newest entry expanded. */
    open() {
        this._expandedId = this._entries[0]?.id ?? null;
        if (this.menu.isOpen)
            this._render();
        else
            this.menu.open(); // renders via open-state-changed
    }

    refresh() {
        if (this.menu.isOpen)
            this._render();
    }

    /**
     * Streaming update: patch the one live label instead of rebuilding the
     * menu on every token. Falls back to a full render when the label is
     * missing or still the placeholder.
     */
    updateLive(entry) {
        // Collapsed rows just say "Thinking…": nothing to update per token.
        if (!this.menu.isOpen || entry.id !== this._expandedId)
            return;
        const partial = this._cb.busy(entry)?.partial;
        const live = this._live;
        if (live?.id === entry.id && live.streaming &&
            live.label.get_stage() && partial) {
            setMarkdown(live.label, partial);
            return;
        }
        this._render();
    }

    _openPrefs() {
        this.menu.close();
        this._cb.onOpenPrefs();
    }

    _render() {
        // Forget thumbnails and drafts of evicted entries.
        const ids = new Set(this._entries.map(e => e.id));
        const thumbs = new Set(this._entries.map(e => e.thumbPath));
        for (const path of this._thumbs.keys()) {
            if (!thumbs.has(path))
                this._thumbs.delete(path);
        }
        for (const id of this._drafts.keys()) {
            if (!ids.has(id))
                this._drafts.delete(id);
        }
        this._historySection.removeAll();
        this._live = null;
        this._focusTarget = null;
        this._renderItems();
        // Ready for a follow-up right away: just type.
        if (this._focusTarget && this.menu.isOpen)
            this._focusTarget.grab_key_focus();
    }

    _renderItems() {
        const hint = this._cb.setupHint();
        if (hint) {
            this._renderSetupHint(hint);
            return;
        }
        if (this._entries.length === 0) {
            this._historySection.addMenuItem(new PopupMenu.PopupMenuItem(
                'No captures yet — press the hotkey or “Capture & explain”.',
                {reactive: false}));
            return;
        }
        for (const entry of this._entries) {
            if (entry.id === this._expandedId)
                this._renderExpanded(entry);
            else
                this._renderCollapsed(entry);
        }
    }

    _renderSetupHint(hint) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'zehntage-setup',
        });
        box.add_child(wrappedLabel(hint, 'zehntage-error'));
        box.add_child(button('Open settings…', () => this._openPrefs()));
        item.add_child(box);
        this._historySection.addMenuItem(item);
    }

    /** Cached St.ImageContent of the entry's small thumbnail, or null. */
    _thumbContent(entry) {
        if (!entry.thumbPath)
            return null;
        let content = this._thumbs.get(entry.thumbPath);
        if (content !== undefined)
            return content;
        content = null;
        try {
            const pixbuf = GdkPixbuf.Pixbuf.new_from_file(entry.thumbPath);
            content = St.ImageContent.new_with_preferred_size(
                pixbuf.width, pixbuf.height);
            content.set_bytes(
                global.stage.context.get_backend().get_cogl_context(),
                pixbuf.read_pixel_bytes(),
                pixbuf.has_alpha ? Cogl.PixelFormat.RGBA_8888
                    : Cogl.PixelFormat.RGB_888,
                pixbuf.width, pixbuf.height, pixbuf.rowstride);
        } catch (e) {
            console.warn(`zehntage-gnome: thumbnail load failed: ${e}`);
        }
        this._thumbs.set(entry.thumbPath, content);
        return content;
    }

    /** Aspect-correct thumbnail; clicking opens the image in the viewer. */
    _thumbnail(entry, width, height, clickable = true) {
        // Small in-memory content instead of a CSS background-image: St's
        // texture cache would keep every full-size PNG for the session.
        const image = new St.Widget({
            style_class: 'zehntage-thumb',
            width,
            height,
            content: this._thumbContent(entry),
            content_gravity: Clutter.ContentGravity.RESIZE_ASPECT,
        });
        if (!clickable)
            return image;
        const b = new St.Button({
            child: image,
            style_class: 'zehntage-thumb-button',
            x_align: Clutter.ActorAlign.START,
        });
        b.connect('clicked', () => {
            this.menu.close();
            // The context (selection outlined) if there is one.
            const uri = Gio.File.new_for_path(
                entry.contextPath ?? entry.imagePath).get_uri();
            try {
                Gio.AppInfo.launch_default_for_uri(uri, null);
            } catch (e) {
                console.error(`zehntage-gnome: failed to open image: ${e}`);
            }
        });
        return b;
    }

    _renderCollapsed(entry) {
        const item = new PopupMenu.PopupBaseMenuItem();
        item.add_child(this._thumbnail(entry, 48, 32, false));
        const firstLine = text => text.trim().split('\n')[0]
            .replace(/\s+/g, ' ');
        const text = entry.status === 'error'
            ? `⚠ ${firstLine(entry.error ?? 'Error')}`
            : entry.status === 'pending'
                ? THINKING
                : stripMd(firstLine(entry.turns[0]?.answer ?? ''));
        const label = new St.Label({
            text,
            style_class: 'zehntage-collapsed-label',
        });
        label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        item.add_child(label);
        item.connect('activate', () => {
            this._expandedId = entry.id;
            this._render();
        });
        this._historySection.addMenuItem(item);
    }

    /** Streaming answer so far, or a placeholder before the first token. */
    _addLive(box, entry, partial, placeholder = THINKING) {
        const label = partial
            ? wrappedLabel(partial, 'zehntage-answer', true)
            : wrappedLabel(placeholder, 'zehntage-pending');
        this._live = {id: entry.id, label, streaming: !!partial};
        box.add_child(label);
    }

    _renderExpanded(entry) {
        const job = this._cb.busy(entry);
        const strongName = modelName(this._cb.strongModel());
        const item = new StaticItem();
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'zehntage-entry',
        });
        box.add_child(this._thumbnail(entry, THUMB_W / 2, THUMB_H / 2));

        if (entry.status === 'pending') {
            this._addLive(box, entry, job?.partial,
                job?.kind === 'strong' ? `${strongName} is thinking…`
                    : THINKING);
        } else if (entry.status === 'error') {
            box.add_child(wrappedLabel(
                entry.error ?? 'Unknown error', 'zehntage-error'));
            const row = new St.BoxLayout({style_class: 'zehntage-buttons'});
            row.add_child(button('Retry', () => this._cb.onRetry(entry)));
            row.add_child(this._strongButton(entry, strongName));
            box.add_child(row);
        } else {
            this._renderTurns(box, entry, job, strongName);
            if (!job)
                box.add_child(this._strongButton(entry, strongName));
            const followUp = this._followUpEntry(entry, !!job);
            box.add_child(followUp);
            this._focusTarget = followUp;
        }

        item.add_child(box);
        this._historySection.addMenuItem(item);
    }

    _renderTurns(box, entry, job, strongName) {
        entry.turns.forEach((turn, i) => {
            if (turn.question) {
                box.add_child(wrappedLabel(
                    `❯ ${turn.question}`, 'zehntage-question'));
            }
            // The strong model is rewriting the last answer: stream it here.
            if (job?.kind === 'strong' && i === entry.turns.length - 1) {
                this._addLive(box, entry, job.partial,
                    `${strongName} is thinking…`);
                return;
            }
            if (turn.answer) {
                box.add_child(wrappedLabel(
                    turn.answer, 'zehntage-answer', true));
            }
            if (turn.model) {
                box.add_child(wrappedLabel(`— ${modelName(turn.model)}`,
                    'zehntage-model-tag'));
            }
        });
        if (entry.strongError) {
            box.add_child(wrappedLabel(`⚠ ${strongName}: ${entry.strongError}`,
                'zehntage-error'));
        }
        if (job?.kind === 'followUp')
            this._addLive(box, entry, job.partial);
        if (entry.followUpError) {
            box.add_child(wrappedLabel(
                `⚠ ${entry.followUpError}`, 'zehntage-error'));
        }
    }

    /** [Opus]: re-ask the last question with the strong model. */
    _strongButton(entry, strongName) {
        return button(strongName, () => this._cb.onStrong(entry),
            'zehntage-strong');
    }

    _followUpEntry(entry, busy) {
        const stEntry = new St.Entry({
            hint_text: busy ? 'waiting for the answer…' : 'follow-up…',
            style_class: 'zehntage-followup',
            can_focus: true,
            x_expand: true,
            text: this._drafts.get(entry.id) ?? '',
        });
        // Drafts outlive re-renders (every finished answer rebuilds the menu).
        stEntry.clutter_text.connect('text-changed', () =>
            this._drafts.set(entry.id, stEntry.get_text()));
        stEntry.clutter_text.connect('activate', () => {
            const text = stEntry.get_text().trim();
            if (!text)
                return;
            // Accepting re-renders synchronously and destroys this entry, so
            // drop the draft first (the new field starts empty) and do not
            // touch stEntry afterwards. Rejected (busy): keep it.
            const draft = stEntry.get_text();
            this._drafts.delete(entry.id);
            if (!this._cb.onFollowUp(entry, text))
                this._drafts.set(entry.id, draft);
        });
        return stEntry;
    }

    destroy() {
        this._thumbs.clear();
        super.destroy();
    }
});

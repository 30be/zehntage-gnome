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

/** Minimal Markdown → Pango markup converter (defensive). */
export function stripMd(text) {
    return text
        .replace(/\*\*([^*\n]+)\*\*/g, '$1')
        .replace(/__([^_\n]+)__/g, '$1')
        .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1$2')
        .replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1$2')
        .replace(/`([^`\n]+)`/g, '$1')
        .replace(/^#{1,6}\s+/, '');
}

export function mdToPango(text) {
    let s = GLib.markup_escape_text(text, -1);
    // Inline code first, so its contents are not styled further.
    s = s.replace(/`([^`\n]+)`/g, '<tt>$1</tt>');
    // Bold: **x** or __x__
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
    s = s.replace(/__([^_\n]+)__/g, '<b>$1</b>');
    // Italic: *x* or _x_ (single markers)
    s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<i>$2</i>');
    s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<i>$2</i>');
    // Headers and bullets, per line.
    s = s.split('\n').map(line => {
        const h = line.match(/^#{1,6}\s+(.*)$/);
        if (h)
            return `<b>${h[1]}</b>`;
        return line.replace(/^(\s*)[-*]\s+/, '$1• ');
    }).join('\n');
    return s;
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

/** Streaming answer so far, or the placeholder before the first token. */
function liveLabel(partial, placeholder = 'Thinking…') {
    return partial
        ? wrappedLabel(partial, 'zehntage-answer', true)
        : wrappedLabel(placeholder, 'zehntage-pending');
}

/** 'claude-opus-5-5' -> 'Opus 5.5' (falls back to the raw id). */
export function modelName(id) {
    const m = /^claude-([a-z]+)-(\d+(?:-\d+)*)$/.exec(id ?? '');
    if (!m)
        return id ?? '';
    return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2].replaceAll('-', '.')}`;
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
     * @param {object} callbacks {onCapture, onFollowUp(entry, q) -> bool,
     *   onUpgrade(entry), strongModel(),
     *   onRetry(entry), onOpenPrefs, setupHint()}
     */
    _init(callbacks) {
        super._init(0.5, 'Zehntage');
        this._cb = callbacks;
        this._entries = [];
        this._expandedId = null;
        this._thumbs = new Map(); // thumbPath -> St.ImageContent | null
        this._drafts = new Map(); // entry id -> unsent follow-up text

        this.add_child(new St.Icon({
            icon_name: 'camera-photo-symbolic',
            style_class: 'system-status-icon',
        }));

        // Capture action.
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
        prefsButton.connect('clicked', () => {
            this.menu.close();
            this._cb.onOpenPrefs();
        });
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

    setEntries(entries) {
        this._entries = entries;
        if (entries.length > 0)
            this._expandedId = entries[0].id;
        if (this.menu.isOpen)
            this._render();
    }

    /**
     * Streaming update: patch the one live label instead of rebuilding the
     * menu on every token. Falls back to a full render when the label is
     * missing or still the "Thinking…" placeholder.
     */
    updateLive(entry) {
        // Collapsed rows just say "Thinking…": nothing to update per token.
        if (!this.menu.isOpen || entry.id !== this._expandedId)
            return;
        const live = this._live;
        const partial = entry.upgradePending ? entry.partialUpgrade
            : entry.followUpPending ? entry.partialFollowUp
                : entry.partial;
        if (live?.id === entry.id && live.streaming &&
            live.label.get_stage() && partial) {
            setMarkdown(live.label, partial);
            return;
        }
        this._render();
    }

    refresh() {
        if (this.menu.isOpen)
            this._render();
    }

    openWith(entries) {
        this.setEntries(entries);
        this.menu.open();
        this._render();
    }

    _render() {
        // Forget thumbnails of evicted entries.
        const live = new Set(this._entries.map(e => e.thumbPath));
        for (const path of this._thumbs.keys()) {
            if (!live.has(path))
                this._thumbs.delete(path);
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
            this._renderNoKey(hint);
            return;
        }

        if (this._entries.length === 0) {
            const item = new PopupMenu.PopupMenuItem(
                'No captures yet — press the hotkey or “Capture & explain”.',
                {reactive: false});
            this._historySection.addMenuItem(item);
            return;
        }

        for (const entry of this._entries)
            this._renderEntry(entry);
    }

    _renderNoKey(hint) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'zehntage-nokey',
        });
        box.add_child(wrappedLabel(hint, 'zehntage-error'));
        const button = new St.Button({
            label: 'Open settings…',
            style_class: 'button zehntage-button',
            x_align: Clutter.ActorAlign.START,
        });
        button.connect('clicked', () => {
            this.menu.close();
            this._cb.onOpenPrefs();
        });
        box.add_child(button);
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

    /** Aspect-correct thumbnail; clicking opens the PNG in the viewer. */
    _thumbnail(entry, width, height, clickable = true) {
        const file = Gio.File.new_for_path(entry.imagePath);
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
        const button = new St.Button({
            child: image,
            style_class: 'zehntage-thumb-button',
            x_align: Clutter.ActorAlign.START,
        });
        button.connect('clicked', () => {
            this.menu.close();
            try {
                Gio.AppInfo.launch_default_for_uri(file.get_uri(), null);
            } catch (e) {
                console.error(`zehntage-gnome: failed to open image: ${e}`);
            }
        });
        return button;
    }

    _renderEntry(entry) {
        if (entry.id === this._expandedId)
            this._renderExpanded(entry);
        else
            this._renderCollapsed(entry);
    }

    _renderCollapsed(entry) {
        const item = new PopupMenu.PopupBaseMenuItem();
        item.add_child(this._thumbnail(entry, 48, 32, false));
        const firstLine = text => text.trim().split('\n')[0]
            .replace(/\s+/g, ' ');
        const first = entry.status === 'error'
            ? `⚠ ${firstLine(entry.error ?? 'Error')}`
            : entry.status === 'pending'
                ? 'Thinking…'
                : stripMd(firstLine(entry.turns[0]?.answer ?? ''));
        const label = new St.Label({
            text: first,
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

    _renderExpanded(entry) {
        const item = new StaticItem();
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'zehntage-entry',
        });

        box.add_child(this._thumbnail(entry, 320, 140));

        if (entry.status === 'pending') {
            const label = liveLabel(entry.partial);
            this._live = {id: entry.id, label, streaming: !!entry.partial};
            box.add_child(label);
        } else if (entry.status === 'error') {
            box.add_child(wrappedLabel(
                entry.error ?? 'Unknown error', 'zehntage-error'));
            const retry = new St.Button({
                label: 'Retry',
                style_class: 'button zehntage-button',
                x_align: Clutter.ActorAlign.START,
            });
            retry.connect('clicked', () => this._cb.onRetry(entry));
            const row = new St.BoxLayout({style_class: 'zehntage-buttons'});
            row.add_child(retry);
            row.add_child(this._upgradeButton(entry));
            box.add_child(row);
        } else {
            entry.turns.forEach((turn, i) => {
                if (turn.question) {
                    box.add_child(wrappedLabel(
                        `❯ ${turn.question}`, 'zehntage-question'));
                }
                const last = i === entry.turns.length - 1;
                if (last && entry.upgradePending) {
                    const name = modelName(this._cb.strongModel());
                    const label = liveLabel(entry.partialUpgrade,
                        `${name} is thinking…`);
                    this._live = {id: entry.id, label,
                        streaming: !!entry.partialUpgrade};
                    box.add_child(label);
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
            if (entry.upgradeError) {
                box.add_child(wrappedLabel(
                    `⚠ ${modelName(this._cb.strongModel())}: ` +
                    `${entry.upgradeError}`, 'zehntage-error'));
            }
            if (entry.followUpPending) {
                const label = liveLabel(entry.partialFollowUp);
                this._live = {id: entry.id, label,
                    streaming: !!entry.partialFollowUp};
                box.add_child(label);
            }
            if (entry.followUpError) {
                box.add_child(wrappedLabel(
                    `⚠ ${entry.followUpError}`, 'zehntage-error'));
            }
            if (!entry.followUpPending && !entry.upgradePending)
                box.add_child(this._upgradeButton(entry));
            const followUp = this._followUpEntry(entry);
            box.add_child(followUp);
            this._focusTarget = followUp;
        }

        item.add_child(box);
        this._historySection.addMenuItem(item);
    }

    /** [Opus]: re-ask the last question with the strong model. */
    _upgradeButton(entry) {
        const button = new St.Button({
            label: modelName(this._cb.strongModel()),
            style_class: 'button zehntage-button zehntage-upgrade',
            x_align: Clutter.ActorAlign.START,
        });
        button.connect('clicked', () => this._cb.onUpgrade(entry));
        return button;
    }

    _followUpEntry(entry) {
        const stEntry = new St.Entry({
            hint_text: entry.followUpPending
                ? 'waiting for the answer…' : 'follow-up…',
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
            // touch stEntry afterwards. Rejected (answer pending): keep it.
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

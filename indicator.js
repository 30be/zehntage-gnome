// indicator.js — top-bar button with scrollable history popup.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

/** Minimal Markdown → Pango markup converter (defensive). */
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
    if (markdown) {
        try {
            label.clutter_text.set_markup(mdToPango(text));
        } catch (e) {
            console.warn(`zehntage-gnome: markup failed: ${e}`);
            label.clutter_text.set_text(text);
        }
    }
    return label;
}

export const Indicator = GObject.registerClass(
class ZehntageIndicator extends PanelMenu.Button {
    /**
     * @param {object} callbacks {onCapture, onFollowUp(entry, q),
     *   onRetry(entry), onOpenPrefs, hasApiKey()}
     */
    _init(callbacks) {
        super._init(0.5, 'Zehntage');
        this._cb = callbacks;
        this._entries = [];
        this._expandedId = null;

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
        this._historySection.removeAll();

        if (!this._cb.hasApiKey()) {
            this._renderNoKey();
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

    _renderNoKey() {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'zehntage-nokey',
        });
        box.add_child(wrappedLabel(
            'Gemini API key is not set.', 'zehntage-error'));
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

    _thumbnail(entry, size) {
        return new St.Icon({
            gicon: new Gio.FileIcon({
                file: Gio.File.new_for_path(entry.imagePath),
            }),
            icon_size: size,
            style_class: 'zehntage-thumb',
        });
    }

    _renderEntry(entry) {
        if (entry.id === this._expandedId)
            this._renderExpanded(entry);
        else
            this._renderCollapsed(entry);
    }

    _renderCollapsed(entry) {
        const item = new PopupMenu.PopupBaseMenuItem();
        item.add_child(this._thumbnail(entry, 32));
        const first = entry.status === 'error'
            ? `⚠ ${entry.error ?? 'Error'}`
            : entry.status === 'pending'
                ? 'Thinking…'
                : (entry.turns[0]?.answer ?? '').split('\n')[0];
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
        const item = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
        });
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'zehntage-entry',
        });

        box.add_child(this._thumbnail(entry, 96));

        if (entry.status === 'pending') {
            box.add_child(wrappedLabel('Thinking…', 'zehntage-pending'));
        } else if (entry.status === 'error') {
            box.add_child(wrappedLabel(
                entry.error ?? 'Unknown error', 'zehntage-error'));
            const retry = new St.Button({
                label: 'Retry',
                style_class: 'button zehntage-button',
                x_align: Clutter.ActorAlign.START,
            });
            retry.connect('clicked', () => this._cb.onRetry(entry));
            box.add_child(retry);
        } else {
            for (const turn of entry.turns) {
                if (turn.question) {
                    box.add_child(wrappedLabel(
                        `❯ ${turn.question}`, 'zehntage-question'));
                }
                if (turn.answer) {
                    box.add_child(wrappedLabel(
                        turn.answer, 'zehntage-answer', true));
                }
            }
            if (entry.followUpPending)
                box.add_child(wrappedLabel('Thinking…', 'zehntage-pending'));
            box.add_child(this._followUpEntry(entry));
        }

        item.add_child(box);
        this._historySection.addMenuItem(item);
    }

    _followUpEntry(entry) {
        const stEntry = new St.Entry({
            hint_text: 'follow-up…',
            style_class: 'zehntage-followup',
            can_focus: true,
            x_expand: true,
        });
        stEntry.clutter_text.connect('activate', () => {
            const text = stEntry.get_text().trim();
            if (text) {
                stEntry.set_text('');
                this._cb.onFollowUp(entry, text);
            }
        });
        return stEntry;
    }
});

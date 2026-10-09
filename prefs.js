// prefs.js — single Adwaita preferences page.

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];

export default class ZehntagePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        /** Text row bound to a string key (trimmed). */
        const entryRow = (title, key, Row = Adw.EntryRow) => {
            const row = new Row({title, text: settings.get_string(key)});
            row.connect('changed', () =>
                settings.set_string(key, row.text.trim()));
            return row;
        };
        /** Drop-down bound to a string key with fixed values. */
        const comboRow = (title, key, values, {labels = values,
            subtitle = null} = {}) => {
            const row = new Adw.ComboRow({
                title,
                subtitle,
                model: Gtk.StringList.new(labels),
                selected: Math.max(0,
                    values.indexOf(settings.get_string(key))),
            });
            row.connect('notify::selected', () =>
                settings.set_string(key, values[row.selected]));
            return row;
        };
        /** Switch bound to a boolean key. */
        const switchRow = (title, key, subtitle) => {
            const row = new Adw.SwitchRow({title, subtitle});
            settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
            return row;
        };
        const group = (title, description = null) => {
            const g = new Adw.PreferencesGroup({title, description});
            page.add(g);
            return g;
        };

        const page = new Adw.PreferencesPage({
            title: 'Zehntage',
            icon_name: 'camera-photo-symbolic',
        });
        window.add(page);

        const claude = group('Claude');
        claude.add(comboRow('Backend', 'backend', ['api', 'cli'], {
            labels: ['Claude API key', 'Claude Code CLI'],
            subtitle: 'api: API key (fastest); cli: your Claude Code login',
        }));
        claude.add(entryRow('claude CLI path', 'claude-path'));
        claude.add(entryRow('API key', 'claude-api-key',
            Adw.PasswordEntryRow));
        claude.add(entryRow('Model', 'model'));
        claude.add(comboRow('Effort', 'effort', EFFORTS, {
            subtitle: 'How much the model thinks; low is fastest',
        }));
        claude.add(switchRow('Whole screen as context', 'context',
            'Send the monitor with the selection outlined in red, plus the ' +
            'selection itself'));
        claude.add(switchRow('Streaming', 'stream',
            'API backend only. Show the answer while it is written'));
        claude.add(switchRow('Thinking', 'thinking',
            'API backend only. Off is about 0.4 s faster'));

        const strong = group('Opus button',
            'Re-asks with a stronger model and replaces the last answer ' +
            '(or answers a failed follow-up). Thinking is always on.');
        strong.add(entryRow('Model', 'strong-model'));
        strong.add(comboRow('Effort', 'strong-effort', EFFORTS));

        const prompt = group('Prompt', 'Sent together with every screenshot.');
        const promptView = new Gtk.TextView({
            wrap_mode: Gtk.WrapMode.WORD_CHAR,
            top_margin: 8, bottom_margin: 8,
            left_margin: 8, right_margin: 8,
        });
        promptView.buffer.text = settings.get_string('system-prompt');
        promptView.buffer.connect('changed', () =>
            settings.set_string('system-prompt', promptView.buffer.text));
        prompt.add(new Gtk.ScrolledWindow({
            min_content_height: 140,
            child: promptView,
            has_frame: true,
        }));
        const suffixGroup = group('Follow-up suffix',
            'Appended to follow-up questions; keeps answers in your ' +
            'language when you ask in another one. Empty: none.');
        suffixGroup.add(entryRow('Suffix', 'followup-suffix'));

        const behaviour = group('Behaviour');
        // Applied on Enter / apply button only: writing on every keystroke
        // would briefly bind partial accelerators (typing <Super>F1 binds
        // <Super>F) system-wide.
        const hotkeyRow = new Adw.EntryRow({
            title: 'Hotkeys, comma-separated (e.g. <Super>z), Enter to apply',
            show_apply_button: true,
            text: settings.get_strv('capture-hotkey').join(', '),
        });
        hotkeyRow.connect('apply', () => {
            const accels = hotkeyRow.text.split(',')
                .map(a => a.trim()).filter(a => a);
            const valid = accels.every(a => {
                const [ok, key, mods] = Gtk.accelerator_parse(a);
                return ok && (key !== 0 || mods !== 0);
            });
            if (!valid) {
                hotkeyRow.add_css_class('error');
                return;
            }
            hotkeyRow.remove_css_class('error');
            settings.set_strv('capture-hotkey', accels);
        });
        behaviour.add(hotkeyRow);

        const historyRow = new Adw.SpinRow({
            title: 'History size',
            adjustment: new Gtk.Adjustment({
                lower: 1, upper: 200, step_increment: 1,
            }),
        });
        settings.bind('history-size', historyRow, 'value',
            Gio.SettingsBindFlags.DEFAULT);
        behaviour.add(historyRow);
    }
}

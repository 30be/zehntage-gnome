// prefs.js — single Adwaita preferences page.

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class ZehntagePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        const page = new Adw.PreferencesPage({
            title: 'Zehntage',
            icon_name: 'camera-photo-symbolic',
        });
        window.add(page);

        // --- Claude ---
        const apiGroup = new Adw.PreferencesGroup({title: 'Claude'});
        page.add(apiGroup);

        const backends = ['api', 'cli'];
        const backendRow = new Adw.ComboRow({
            title: 'Backend',
            subtitle: 'api: API key (fastest); cli: your Claude Code login',
            model: Gtk.StringList.new(
                ['Claude API key', 'Claude Code CLI']),
            selected: Math.max(0,
                backends.indexOf(settings.get_string('backend'))),
        });
        backendRow.connect('notify::selected', () =>
            settings.set_string('backend', backends[backendRow.selected]));
        apiGroup.add(backendRow);

        const pathRow = new Adw.EntryRow({title: 'claude CLI path'});
        pathRow.text = settings.get_string('claude-path');
        pathRow.connect('changed', () =>
            settings.set_string('claude-path', pathRow.text.trim()));
        apiGroup.add(pathRow);

        const keyRow = new Adw.PasswordEntryRow({title: 'API key'});
        keyRow.text = settings.get_string('claude-api-key');
        keyRow.connect('changed', () =>
            settings.set_string('claude-api-key', keyRow.text.trim()));
        apiGroup.add(keyRow);

        const modelRow = new Adw.EntryRow({title: 'Model'});
        modelRow.text = settings.get_string('model');
        modelRow.connect('changed', () =>
            settings.set_string('model', modelRow.text.trim()));
        apiGroup.add(modelRow);

        const efforts = ['low', 'medium', 'high', 'xhigh', 'max'];
        const effortRow = new Adw.ComboRow({
            title: 'Effort',
            subtitle: 'How much the model thinks; low is fastest',
            model: Gtk.StringList.new(efforts),
            selected: Math.max(0,
                efforts.indexOf(settings.get_string('effort'))),
        });
        effortRow.connect('notify::selected', () =>
            settings.set_string('effort', efforts[effortRow.selected]));
        apiGroup.add(effortRow);

        const streamRow = new Adw.SwitchRow({
            title: 'Streaming',
            subtitle: 'API backend only. Show the answer while it is written',
        });
        settings.bind('stream', streamRow, 'active',
            Gio.SettingsBindFlags.DEFAULT);
        apiGroup.add(streamRow);

        const thinkingRow = new Adw.SwitchRow({
            title: 'Thinking',
            subtitle: 'API backend only. Off is about 0.4 s faster',
        });
        settings.bind('thinking', thinkingRow, 'active',
            Gio.SettingsBindFlags.DEFAULT);
        apiGroup.add(thinkingRow);

        // --- [Opus] button ---
        const strongGroup = new Adw.PreferencesGroup({
            title: 'Opus button',
            description: 'Re-asks with a stronger model and replaces ' +
                'the last answer. Thinking is always on.',
        });
        page.add(strongGroup);

        const strongModelRow = new Adw.EntryRow({title: 'Model'});
        strongModelRow.text = settings.get_string('strong-model');
        strongModelRow.connect('changed', () =>
            settings.set_string('strong-model', strongModelRow.text.trim()));
        strongGroup.add(strongModelRow);

        const strongEffortRow = new Adw.ComboRow({
            title: 'Effort',
            model: Gtk.StringList.new(efforts),
            selected: Math.max(0,
                efforts.indexOf(settings.get_string('strong-effort'))),
        });
        strongEffortRow.connect('notify::selected', () =>
            settings.set_string('strong-effort',
                efforts[strongEffortRow.selected]));
        strongGroup.add(strongEffortRow);

        // --- Prompt ---
        const promptGroup = new Adw.PreferencesGroup({
            title: 'Prompt',
            description: 'Sent together with every screenshot.',
        });
        page.add(promptGroup);

        const promptView = new Gtk.TextView({
            wrap_mode: Gtk.WrapMode.WORD_CHAR,
            top_margin: 8, bottom_margin: 8,
            left_margin: 8, right_margin: 8,
        });
        promptView.buffer.text = settings.get_string('system-prompt');
        promptView.buffer.connect('changed', () =>
            settings.set_string('system-prompt', promptView.buffer.text));
        const promptScroll = new Gtk.ScrolledWindow({
            min_content_height: 140,
            child: promptView,
            has_frame: true,
        });
        promptGroup.add(promptScroll);

        // --- Behaviour ---
        const behaviourGroup = new Adw.PreferencesGroup({title: 'Behaviour'});
        page.add(behaviourGroup);

        // Applied on Enter / apply button only: writing on every keystroke
        // would briefly bind partial accelerators (typing <Super>F1 binds
        // <Super>F) system-wide.
        const hotkeyRow = new Adw.EntryRow({
            title: 'Hotkeys, comma-separated (e.g. <Super>z), Enter to apply',
            show_apply_button: true,
        });
        hotkeyRow.text = settings.get_strv('capture-hotkey').join(', ');
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
        behaviourGroup.add(hotkeyRow);

        const historyRow = new Adw.SpinRow({
            title: 'History size',
            adjustment: new Gtk.Adjustment({
                lower: 1, upper: 200, step_increment: 1,
            }),
        });
        settings.bind('history-size', historyRow, 'value',
            Gio.SettingsBindFlags.DEFAULT);
        behaviourGroup.add(historyRow);
    }
}

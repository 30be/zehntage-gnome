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

        // --- Gemini ---
        const apiGroup = new Adw.PreferencesGroup({title: 'Gemini'});
        page.add(apiGroup);

        const keyRow = new Adw.PasswordEntryRow({title: 'API key'});
        keyRow.text = settings.get_string('api-key');
        keyRow.connect('changed', () =>
            settings.set_string('api-key', keyRow.text));
        apiGroup.add(keyRow);

        const modelRow = new Adw.EntryRow({title: 'Model'});
        modelRow.text = settings.get_string('model');
        modelRow.connect('changed', () =>
            settings.set_string('model', modelRow.text));
        apiGroup.add(modelRow);

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
        promptView.buffer.text = settings.get_string('prompt');
        promptView.buffer.connect('changed', () =>
            settings.set_string('prompt', promptView.buffer.text));
        const promptScroll = new Gtk.ScrolledWindow({
            min_content_height: 140,
            child: promptView,
            has_frame: true,
        });
        promptGroup.add(promptScroll);

        // --- Behaviour ---
        const behaviourGroup = new Adw.PreferencesGroup({title: 'Behaviour'});
        page.add(behaviourGroup);

        const hotkeyRow = new Adw.EntryRow({
            title: 'Hotkey (e.g. <Super><Shift>g)',
        });
        hotkeyRow.text = settings.get_strv('capture-hotkey')[0] ?? '';
        hotkeyRow.connect('changed', () => {
            const accel = hotkeyRow.text.trim();
            settings.set_strv('capture-hotkey', accel ? [accel] : []);
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

        const markerRow = new Adw.SpinRow({
            title: 'Marker size (px)',
            adjustment: new Gtk.Adjustment({
                lower: 16, upper: 128, step_increment: 2,
            }),
        });
        settings.bind('marker-size', markerRow, 'value',
            Gio.SettingsBindFlags.DEFAULT);
        behaviourGroup.add(markerRow);
    }
}

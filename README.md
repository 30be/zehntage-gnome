# zehntage-gnome

Context-aware screen assistant for GNOME Shell 50. Press the hotkey — the
whole screen is captured instantly with a red ring marking where your cursor
was, and Gemini explains what's there. Answers live in a top-bar popup with
history and follow-up questions. (Optionally, the native interactive
area-selection UI can be used instead — see Settings.)

## Install

```nu
glib-compile-schemas schemas/
gnome-extensions pack --force --extra-source=capture.js --extra-source=gemini.js --extra-source=history.js --extra-source=indicator.js
gnome-extensions install --force zehntage-gnome@lyka.shell-extension.zip
gnome-extensions enable zehntage-gnome@lyka
```

On Wayland the extension fully activates after re-login.

## Usage

- Hotkey: `<Super><Shift>G` (configurable; any accelerator works, e.g.
  `XF86Favorites`) — capture & explain.
- Panel camera icon → history popup; "Capture & explain" menu item, gear
  button in the same row opens settings.
- Answers render basic Markdown (**bold**, *italic*, `code`, headers,
  bullets).
- Newest answer is expanded; type into "follow-up…" and press Enter to
  continue the conversation about that screenshot.
- Click a screenshot preview to open the PNG in your image viewer.

## Settings

`gnome-extensions prefs zehntage-gnome@lyka` — Gemini API key
(aistudio.google.com), model (default `gemini-3.1-flash-lite`), prompt,
hotkey, history size (default 20), marker size, marker glow (px, default
10), and "Interactive capture (area selection)" (default off — full-screen
capture without any UI). The gear button in the panel menu opens the same
dialog. Hotkey example via CLI:

```nu
gsettings --schemadir ~/.local/share/gnome-shell/extensions/zehntage-gnome@lyka/schemas set org.gnome.shell.extensions.zehntage-gnome capture-hotkey "['XF86Favorites']"
```

History is stored in `~/.local/share/zehntage-gnome@lyka/` (history.json +
PNG copies; oldest entries and their PNGs are evicted past the cap).

## Development: iterate without relogin

`gnome-shell --nested` was removed in GNOME 50; its replacement is
`--devkit` — a full shell running in a window inside your session, which
loads extensions fresh on every launch. `./dev.sh` packs, installs, and
starts it in one go:

```nu
./dev.sh
```

The devkit shell shares your extension dir and dconf, so the API key and
`enabled-extensions` are already in place; load errors appear on stderr.
Close the window, edit, re-run. A relogin is only needed to roll a new
version into the *real* session.

Headless smoke test (logs only):

```nu
dbus-run-session -- gnome-shell --headless --wayland --virtual-monitor 1280x720
```

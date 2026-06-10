# zehntage-gnome

Context-aware screen assistant for GNOME Shell 50. Press the hotkey — the
native screenshot UI opens with a red ring marking where your cursor was —
select an area, and Gemini explains what's there. Answers live in a top-bar
popup with history and follow-up questions.

## Install

```nu
glib-compile-schemas schemas/
gnome-extensions pack --force --extra-source=capture.js --extra-source=gemini.js --extra-source=history.js --extra-source=indicator.js
gnome-extensions install --force zehntage-gnome@lyka.shell-extension.zip
gnome-extensions enable zehntage-gnome@lyka
```

On Wayland the extension fully activates after re-login.

## Usage

- Hotkey: `<Super><Shift>G` (configurable) — capture & explain.
- Panel camera icon → history popup; "Capture & explain" menu item.
- Newest answer is expanded; type into "follow-up…" and press Enter to
  continue the conversation about that screenshot.

## Settings

`gnome-extensions prefs zehntage-gnome@lyka` — Gemini API key
(aistudio.google.com), model (default `gemini-3.1-flash-lite`), prompt,
hotkey, history size (default 20), marker size.

History is stored in `~/.local/share/zehntage-gnome@lyka/` (history.json +
PNG copies; oldest entries and their PNGs are evicted past the cap).

## Testing in a nested session

```nu
$env.MUTTER_DEBUG_DUMMY_MODE_SPECS = "1280x720"
dbus-run-session -- gnome-shell --nested --wayland
```

The nested shell picks up the installed extension; check stderr for
`zehntage` errors. Headless alternative:

```nu
dbus-run-session -- gnome-shell --headless --wayland --virtual-monitor 1280x720
```

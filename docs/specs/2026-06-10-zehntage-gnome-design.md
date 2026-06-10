# zehntage-gnome — design spec (2026-06-10)

Context-aware screen assistant for GNOME Shell. Press a hotkey, grab a screenshot
with GNOME's native screenshot UI (with a red marker at the cursor position at
hotkey time), send it to Gemini, read the answer in a top-bar popup with history.

## Form

Pure GNOME Shell extension (GJS, ESM), `shell-version: ["50"]`, uuid
`zehntage-gnome@lyka`. No external processes, no portals (extension runs inside
the compositor, so `Shell.Screenshot` and the screenshot UI are directly usable
on Wayland).

## Main flow

1. Hotkey (default `<Super><Shift>G`, configurable, registered via
   `Main.wm.addKeybinding` with a GSettings `as` key).
2. Record `global.get_pointer()`; place a red marker actor (ring ~40px, drawn
   with St/Cairo) at that point on the stage.
3. Open `Main.screenshotUI.open()` (the native UI freezes the stage including
   the marker). Connect a **one-shot** handler to `Main.screenshotUI`'s
   `screenshot-taken` signal (guard flag so user-initiated Print screenshots
   are ignored); remove the marker actor when the shot is taken or the UI is
   dismissed.
4. Read the resulting `Gio.File` PNG, POST to Gemini
   (`generativelanguage.googleapis.com`, model default `gemini-3.1-flash-lite`)
   via Soup 3 with `inline_data` base64 PNG. Prompt (configurable): explain
   what is in this screen fragment; the red ring marks the user's point of
   interest; answer in Russian.
5. Open the panel menu automatically with the answer.

Fallback if the frozen frame turns out not to include overlay actors: custom
pipeline via `Screenshot.SelectArea` + `screenshot_stage_to_content()` +
`composite_to_stream()`, drawing the red ring with Cairo at the returned
cursor point.

## Panel indicator

`PanelMenu.Button` in the top bar; menu is a scrollable history (Clipboard
Indicator style): newest entry expanded — thumbnail (`St.Icon` with
`Gio.FileIcon`) + wrapped multi-line label (max-width ~34em); older entries
collapsed (thumbnail + first line, click to expand). An expanded entry has an
understated "follow-up…" `St.Entry`: Enter sends image + previous answer +
question, response appended to the entry. A camera button in the menu triggers
the same capture without the hotkey.

## Storage & settings

- History: `~/.local/share/zehntage-gnome@lyka/` — `history.json` + PNG copies;
  cap 20 entries (setting), evicted entries delete their PNGs.
- GSettings: API key, model, hotkey, prompt, history size, marker style.
- Prefs window: single Adwaita page (key, model, prompt, hotkey, history size).

## Errors & cleanup

- Missing API key → menu opens with a hint + button to open prefs.
- Network/API errors → shown in the history entry with a retry button.
- `disable()`: remove keybinding, disconnect signals, `session.abort()`,
  destroy indicator and marker, null fields.

## Testing

- `glib-compile-schemas` + syntax check of all JS.
- Nested session for live testing without logout:
  `dbus-run-session -- gnome-shell --nested --wayland` with the extension
  installed and enabled; inspect stderr for load errors.
- Final: `gnome-extensions install`, enable; full activation in the real
  session needs re-login (Wayland).

## Out of scope (YAGNI)

Custom SelectArea as primary path, Anki integration, libsecret, structured
JSON response schemas, pre-50 GNOME support.

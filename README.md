# zehntage-gnome

Screen assistant for GNOME Shell 50. Press the hotkey, drag a rectangle over
anything on screen, and Claude (Haiku 5.5, effort `low`) explains it. The
answer pops up from the top bar, with history and follow-up questions.

Two backends:

- `api` (default, fastest): the Claude API with an API key. Streams the
  answer: first words after ~0.8 s, done after ~1.2 s.
- `cli` (no key needed): the locally installed Claude Code (`claude -p`)
  with your existing Claude login. About 2 s per answer.

## Install

```nu
glib-compile-schemas schemas/
gnome-extensions pack --force --extra-source=selector.js --extra-source=claude.js --extra-source=cli.js --extra-source=history.js --extra-source=indicator.js
gnome-extensions install --force zehntage-gnome@lyka.shell-extension.zip
gnome-extensions enable zehntage-gnome@lyka
```

On Wayland the new version activates after re-login.

## Usage

- `<Super>z` (configurable, several accelerators allowed, e.g.
  `<Super>z, XF86Favorites`): the screen freezes and dims.
  - drag: explain that area
  - single click: explain the whole screen
  - Escape / right click: cancel
- The panel popup opens with "Thinking…", then the answer (basic Markdown).
- The follow-up field is focused: type and press Enter.
- Panel camera icon: history; "Capture & explain"; gear opens settings.
- Click a screenshot preview to open the image in your viewer.
- **[Opus 5.5]** button under the last answer (and next to Retry on
  errors): re-asks the same question with Claude Opus 5.5 at effort `high`
  (thinking on) and replaces that answer, streamed. For when Haiku gets it
  wrong. Follow-ups afterwards go back to the fast model. Model and effort:
  `strong-model`, `strong-effort`.

## Settings

`gnome-extensions prefs zehntage-gnome@lyka`: backend (`api` / `cli`),
claude CLI path, API key (platform.claude.com), model (default
`claude-haiku-5-5`), effort (default `low`), streaming (default on),
thinking (default off), system prompt, hotkeys, history size (default 20).
Hidden: `cli-timeout` (default 60 s).

The API backend is tuned for latency (measured on Haiku 5.5): thinking off
by default (~0.4 s faster; switch `thinking` on for hard images), streamed
SSE rendered token by token (`stream`, on by default), the TLS connection is opened while you are
still selecting, and captures over 300 KB go out as JPEG (a full 1568 px
screen: ~1 MB PNG vs ~80 KB JPEG, ~0.25 s faster).

The CLI backend is tuned for latency: `--safe-mode` (no plugins, hooks, MCP
or CLAUDE.md), `--tools ""`, a short custom `--system-prompt`, `--effort low`,
`--no-session-persistence`, and no background traffic. Most of the remaining
time is output tokens, so the default prompt asks for at most 3 short lines;
a longer prompt makes answers noticeably slower. CLI follow-ups are
stateless: the image plus the transcript so far are sent again.

```nu
gsettings --schemadir ~/.local/share/gnome-shell/extensions/zehntage-gnome@lyka/schemas set org.gnome.shell.extensions.zehntage-gnome capture-hotkey "['<Super>z', 'XF86Favorites']"
```

History lives in `~/.local/share/zehntage-gnome@lyka/` (history.json + images;
oldest entries are evicted past the cap). Each entry pins its model and
system prompt, and stores Claude's raw reply blocks, so follow-ups replay the
conversation unchanged (Haiku 5.5 rejects edited history).

## Tests (autonomous, headless)

```nu
./test/run.py                 # all scenarios against a mock Claude API
./test/run.py drag followup   # only scenarios matching these names
./test/run.py --screens 1920x1080@1.25,1920x1080@1  # monitors + scales
./test/run.py --live-cli      # real claude CLI, 3 timed runs + follow-up
# real API, timed: first visible words and full answer
with-env {ANTHROPIC_API_KEY: "sk-ant-..."} { ./test/run.py --live }
```

`test/run.py` starts a throwaway `gnome-shell --headless` on a private D-Bus
session with isolated XDG dirs and the GSettings keyfile backend. It cannot
touch your real settings or history, and it fails loudly if
`~/.config/dconf/user` changes during a run. A test-only helper extension
(`test/helper/`, never installed for real) enables `org.gnome.Shell.Eval` and
drives the shell with virtual pointer/keyboard devices: real hotkey, real
drag, real typing. The Claude API is a local mock and `claude` a fake
(`test/fake-claude.py`). Crops are checked pixel by pixel against colored
boxes on every monitor (fractional scales included), and against real app
windows (`test/color-app.py`): Wayland and X11 (Xwayland) clients, a
fullscreen app, and an open dropdown menu, which must be in the frozen
frame even though the grab closes it. Capturing runs inside the
compositor, so GNOME's screenshot restrictions for other programs (D-Bus
allowlist, portal dialog) don't apply. Screenshots of each
scenario go to `test/out/`.

## Development: windowed shell

`./dev.sh` packs, installs, and starts `gnome-shell --devkit` (a full shell in
a window; the GNOME 50 replacement for `--nested`). It shares your real
extension dir and dconf.

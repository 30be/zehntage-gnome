# zehntage-gnome

Screen assistant for GNOME Shell 50. Press the hotkey, drag a rectangle over
anything on screen, and Claude (Haiku 5.5, effort `low`) explains it. The
answer pops up from the top bar, with history and follow-up questions.

Two backends:

- `cli` (default): the locally installed Claude Code (`claude -p`), using
  your existing Claude login. No API key. About 2 s per answer.
- `api`: the Claude API with an API key.

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

## Settings

`gnome-extensions prefs zehntage-gnome@lyka`: backend (`cli` / `api`),
claude CLI path, API key (platform.claude.com), model (default
`claude-haiku-5-5`), effort (default `low`), system prompt, hotkeys, history
size (default 20). Hidden: `cli-timeout` (default 60 s).

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
boxes on every monitor (fractional scales included). Screenshots of each
scenario go to `test/out/`.

## Development: windowed shell

`./dev.sh` packs, installs, and starts `gnome-shell --devkit` (a full shell in
a window; the GNOME 50 replacement for `--nested`). It shares your real
extension dir and dconf.

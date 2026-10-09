# zehntage-gnome

Screen assistant for GNOME Shell 50. Press the hotkey, drag a rectangle over
anything on screen, and Claude (Haiku 5.5, effort `low`) explains it — with
the whole screen as context: Claude gets the monitor with your selection
outlined in red, plus the selection at full resolution. The answer streams
into a top-bar popup, with history and follow-up questions.

Two backends:

- `api` (default, fastest): the Claude API with an API key. First words
  after ~0.8 s, done after ~1.2 s (measured live on Haiku 5.5).
- `cli` (no key needed): the locally installed Claude Code (`claude -p`)
  with your existing Claude login. First words after ~1.1 s, done ~1.7 s.

## Install

```nu
./install.sh
gnome-extensions enable zehntage-gnome@lyka
```

`install.sh` packs and installs for the current user (`gnome-extensions
install` compiles the schemas). On Wayland a new version activates after
re-login; `./dev.sh` installs and opens a windowed dev shell
(`gnome-shell --devkit`, the GNOME 50 replacement for `--nested`) to try it
without relogin. The dev shell shares your real extension dir and dconf.

## Usage

- `<Super>z` (configurable, several accelerators allowed, e.g.
  `<Super>z, XF86Favorites`): the screen freezes and dims.
  - drag: explain that area (a thin drag, e.g. underlining a line, grabs
    that line), read in the context of the whole monitor
  - single click: explain the whole monitor under the pointer
  - Escape / right click: cancel
- The popup opens with "Thinking…", then the answer streams in (basic
  Markdown).
- The follow-up field is focused: type and press Enter. Text typed while
  an answer is still coming is kept.
- **[Opus 5.5]** button: re-asks with Claude Opus 5.5 at effort `high`
  (thinking on) and replaces the last answer, streamed — or, right after a
  failed follow-up, answers that question. Also next to Retry on errors.
  Follow-ups afterwards go back to the fast model.
- Panel camera icon: history; "Capture & explain"; gear opens settings.
- Click a screenshot preview to open the image in your viewer.

## Settings

`gnome-extensions prefs zehntage-gnome@lyka`:

- Claude: backend (`api` / `cli`), claude CLI path (`~` works), API key
  (platform.claude.com), model (default `claude-haiku-5-5`), effort
  (default `low`), whole screen as context (default on), streaming
  (default on), thinking (default off).
- Opus button: model (default `claude-opus-5-5`), effort (default `high`).
- Prompt, follow-up suffix (default `(ответ по-русски)`, appended to
  follow-up questions: without it Haiku answers in the question's
  language), hotkeys (applied on Enter), history size (default 20).
- Hidden: `cli-timeout` (default 60 s), `api-base-url` (tests).

```nu
gsettings --schemadir ~/.local/share/gnome-shell/extensions/zehntage-gnome@lyka/schemas set org.gnome.shell.extensions.zehntage-gnome capture-hotkey "['<Super>z', 'XF86Favorites']"
```

### What Claude gets

For a drag, the first message is: "Image 1: the whole screen; the red
rectangle (x, y, width, height in pixels of this image) marks the selected
region", the monitor (downscaled to 1568 px, JPEG) with a crimson frame
drawn just outside the selection, then "Image 2: the selected region at full
resolution" and the crop. The system prompt says to explain what is inside
the rectangle and use the rest only to understand it. Measured on Haiku
5.5: ~1850 more input tokens but no slower to the first word, and answers
read the selection in its sentence (one word: "frisst — ест (о животных)…
Das Eichhörnchen frisst Nüsse"). A click sends just the monitor.

### Why it is fast

API backend (each measured on Haiku 5.5):

- thinking off (~0.4 s faster). Models that cannot run without thinking
  (Opus 5.5) are detected via the Models API and keep it on.
- streamed SSE, rendered token by token into one live label.
- the TLS connection is opened while you are still selecting, and the
  request reuses it (libsoup needs the POST marked idempotent for that).
- the monitor is captured while you are still dragging (GNOME's
  `composite_to_stream` always PNG-encodes: ~0.65 s for a 1080p monitor);
  after release only cropping, the frame and a JPEG remain (~65 ms).
- captures over 300 KB go out as JPEG (a full 1568 px screen: ~1 MB PNG vs
  ~80 KB JPEG, ~0.25 s faster). Small crops stay PNG.
- the prompt asks for at most 3 short lines: output tokens are most of
  the time.
- no prompt caching: small crops are below the minimum, and for full
  screens a cache hit was ~0.3 s *slower* to the first token.

CLI backend: `--safe-mode` (no plugins, hooks, MCP or CLAUDE.md),
`--tools ""`, a short `--system-prompt`, `--effort low`,
`--no-session-persistence`, `--include-partial-messages` (streamed), and
no background traffic. Follow-ups are stateless: the image plus the
transcript so far are sent again.

### History

`~/.local/share/zehntage-gnome@lyka/` (history.json, images, small
thumbnails; oldest entries are evicted past the cap). Each entry pins its
backend, model and system prompt and stores Claude's raw reply blocks, so
follow-ups replay the conversation unchanged (Haiku 5.5 rejects edited
history). In-flight state is never saved. Old Gemini-era entries are
migrated on load.

## Tests (autonomous, headless)

```nu
./test/run.py                 # all mock scenarios (~80)
./test/run.py drag followup   # only scenarios whose name matches
./test/run.py --screens 1920x1080@1.25,1920x1080@1  # monitors + scales
./test/run.py --live-cli      # real claude CLI: timed runs + follow-up
with-env {ANTHROPIC_API_KEY: "sk-ant-..."} { ./test/run.py --live }
```

`test/run.py` re-runs itself on a private D-Bus session with isolated XDG
dirs, its own runtime dir (sockets), the GSettings keyfile backend and, in
mock runs, a throwaway `$HOME` with a fake `claude` on `$PATH`. It starts a
headless `gnome-shell`, proves the settings isolation before testing, and
fails loudly if any key it wrote changes in your real dconf. The API key
for `--live` is passed via a 0600 file, never argv or the environment, and
the temp dir is removed after a passing run.

- `test/harness.py`: mock Claude API (SSE, keep-alive), shell control,
  helpers. `test/scenarios.py`: the mock scenarios. `test/live.py`: timed
  live runs.
- `test/helper/` (never installed for real) enables
  `org.gnome.Shell.Eval` and drives the shell with virtual pointer and
  keyboard: real hotkey, real drag, real typing.
- Crops are checked pixel by pixel against colored boxes on every monitor
  (fractional scales included) and against real app windows
  (`test/color-app.py`): Wayland and X11 (Xwayland) clients, a fullscreen
  app, and an open dropdown menu, which must be in the frozen frame even
  though the grab closes it. Capturing runs inside the compositor, so
  GNOME's screenshot restrictions for other programs (D-Bus allowlist,
  portal dialog) don't apply.
- Screenshots of each scenario go to `test/out/`.

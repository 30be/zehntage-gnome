// cli.js — ask the locally installed Claude Code CLI (`claude -p`).
//
// Uses the user's existing Claude login, no API key. Tuned for latency:
// safe mode (no plugins/hooks/MCP/CLAUDE.md), no tools, a short custom system
// prompt, the configured (low) effort, no session files, no background
// traffic. One process
// per request, streamed; stateless follow-ups carry the transcript as text.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {imageBlock, readAll} from './claude.js';

Gio._promisify(Gio.Subprocess.prototype, 'wait_async');
Gio._promisify(Gio.OutputStream.prototype, 'write_all_async');
Gio._promisify(Gio.OutputStream.prototype, 'close_async');
// Already promisified by GNOME Shell (bytes finish): lines may be bytes.
Gio._promisify(Gio.DataInputStream.prototype, 'read_line_async');

const QUIET_ENV = {
    DISABLE_AUTOUPDATER: '1',
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
    DISABLE_TELEMETRY: '1',
    DISABLE_ERROR_REPORTING: '1',
};

export class CliClient {
    constructor(settings) {
        this._settings = settings;
        this._procs = new Set();
    }

    get _binary() {
        let path = this._settings.get_string('claude-path').trim() ||
            'claude';
        if (path === '~' || path.startsWith('~/'))
            path = GLib.get_home_dir() + path.slice(1);
        return path.includes('/') ? path : GLib.find_program_in_path(path);
    }

    get available() {
        const bin = this._binary;
        return !!bin && GLib.file_test(bin, GLib.FileTest.IS_EXECUTABLE);
    }

    /**
     * @param {object} req {model, system, image: {bytes, mediaType},
     *   turns: [{question?, answer}], question?, effort?} (effort overrides
     *   the setting: the [Opus] button)
     * @param {Function} [onText] called with the answer text so far
     * @returns {Promise<{text: string}>}
     */
    async send({model, system, image, turns, question, effort},
        onText = null) {
        const bin = this._binary;
        if (!bin)
            throw new Error('claude CLI not found');

        const content = [imageBlock(image.bytes, image.mediaType)];
        if (question)
            content.push({type: 'text', text: transcript(turns, question)});
        const input = `${JSON.stringify({
            type: 'user',
            message: {role: 'user', content},
        })}\n`;

        const launcher = new Gio.SubprocessLauncher({
            flags: Gio.SubprocessFlags.STDIN_PIPE |
                Gio.SubprocessFlags.STDOUT_PIPE |
                Gio.SubprocessFlags.STDERR_PIPE,
        });
        for (const [k, v] of Object.entries(QUIET_ENV))
            launcher.setenv(k, v, true);
        launcher.set_cwd(GLib.get_tmp_dir());
        const proc = launcher.spawnv([bin, '-p',
            '--safe-mode',
            '--model', model,
            '--effort', effort ?? this._settings.get_string('effort'),
            '--tools', '',
            '--strict-mcp-config',
            '--no-session-persistence',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            '--include-partial-messages',
            '--system-prompt', system,
        ]);
        this._procs.add(proc);
        let timedOut = false;
        const timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,
            this._settings.get_int('cli-timeout'), () => {
                timedOut = true;
                proc.force_exit();
                return GLib.SOURCE_REMOVE;
            });
        let result = null;
        let stderr = '';
        try {
            // Write stdin while already reading stdout: a multi-MB image
            // must not deadlock against a full stdout pipe.
            const writing = writeAll(proc.get_stdin_pipe(),
                new TextEncoder().encode(input));
            const errText = readAll(proc.get_stderr_pipe())
                .catch(e => `(stderr unreadable: ${e.message})`);
            result = await readStream(proc.get_stdout_pipe(), onText);
            await writing.catch(() => {}); // EPIPE if claude died early
            stderr = await errText;
            await proc.wait_async(null);
        } finally {
            GLib.source_remove(timer);
            this._procs.delete(proc);
        }

        if (!result) {
            const why = timedOut ? 'timed out'
                : stderr.trim().split('\n').pop();
            throw new Error(`claude CLI failed: ${why || 'no result'}`);
        }
        const text = String(result.result ?? '').trim();
        if (result.is_error || result.subtype !== 'success')
            throw new Error(`claude CLI: ${text || result.subtype}`);
        if (!text)
            throw new Error('Empty answer from claude CLI');
        return {text};
    }

    destroy() {
        for (const proc of this._procs)
            proc.force_exit();
        this._procs.clear();
        this._settings = null;
    }
}

async function writeAll(stream, bytes) {
    try {
        await stream.write_all_async(bytes, GLib.PRIORITY_DEFAULT, null);
    } finally {
        await stream.close_async(GLib.PRIORITY_DEFAULT, null).catch(() => {});
    }
}

/**
 * Reads claude's stream-json stdout: text deltas go to onText, the final
 * "result" message is returned (null if the process ended without one).
 */
async function readStream(stream, onText) {
    const lines = new Gio.DataInputStream({base_stream: stream});
    let text = '';
    let result = null;
    for (;;) {
        const [raw] = await lines.read_line_async(GLib.PRIORITY_DEFAULT,
            null);
        if (raw === null)
            return result;
        let msg;
        try {
            msg = JSON.parse(typeof raw === 'string' ? raw
                : new TextDecoder().decode(raw));
        } catch {
            continue; // not JSON (warnings etc.)
        }
        const delta = msg.event?.delta;
        if (msg.type === 'stream_event' && delta?.type === 'text_delta') {
            text += delta.text;
            onText?.(text);
        } else if (msg.type === 'result') {
            result = msg;
        }
    }
}

/** Follow-up prompt for a stateless call: prior Q&A, then the question. */
function transcript(turns, question) {
    const lines = ['Conversation so far about this screenshot:'];
    for (const turn of turns) {
        if (turn.question)
            lines.push(`User: ${turn.question}`);
        lines.push(`You: ${turn.answer}`);
    }
    lines.push('', `New question: ${question}`);
    return lines.join('\n');
}

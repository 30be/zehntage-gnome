// cli.js — ask the locally installed Claude Code CLI (`claude -p`).
//
// Uses the user's existing Claude login, no API key. Tuned for latency:
// safe mode (no plugins/hooks/MCP/CLAUDE.md), no tools, a short custom system
// prompt, the configured (low) effort, no session files, no background
// traffic. One process
// per request, streamed; stateless follow-ups carry the transcript as text.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GLibUnix from 'gi://GLibUnix';

import {readAll} from './claude.js';

Gio._promisify(Gio.Subprocess.prototype, 'wait_async');
Gio._promisify(Gio.OutputStream.prototype, 'write_bytes_async');
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
     * @param {object} req {model, system, content: the screenshot message
     *   blocks, turns: [{question?, answer}], question?, effort?} (effort
     *   overrides the setting: the [Opus] button)
     * @param {Function} [onText] called with the answer text so far
     * @returns {Promise<{text: string}>}
     */
    async send({model, system, content: screenshot, turns, question, effort,
        suffix = ''},
        onText = null) {
        const content = [...screenshot];
        if (question)
            content.push({type: 'text',
                text: transcript(turns, question, suffix)});
        const result = await this._run({
            model,
            effort: effort ?? this._settings.get_string('effort'),
            system,
            content,
            extra: ['--tools', '', '--include-partial-messages'],
        }, onText);
        const text = String(result.result ?? '').trim();
        if (!text)
            throw new Error('Empty answer from claude CLI');
        return {text};
    }

    /**
     * Structured run (the fact-check): the answer must match schema;
     * optional web tools, allowed without prompts.
     *
     * @returns {Promise<object>} the structured output
     */
    async check({model, effort, system, content, schema, web = true}) {
        const tools = web ? 'WebSearch,WebFetch' : '';
        const result = await this._run({
            model, effort, system, content,
            extra: ['--tools', tools, '--allowed-tools', tools,
                '--permission-mode', 'dontAsk',
                '--json-schema', JSON.stringify(schema)],
            timeout: this._settings.get_int('factcheck-timeout'),
        });
        if (!result.structured_output)
            throw new Error('claude CLI: no structured output');
        return result.structured_output;
    }

    /** One `claude -p` process; resolves with its final "result" message. */
    async _run({model, effort, system, content, extra, timeout},
        onText = null) {
        const bin = this._binary;
        if (!bin)
            throw new Error('claude CLI not found');
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
            '--effort', effort,
            '--strict-mcp-config',
            '--no-session-persistence',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            ...extra,
            '--system-prompt', system,
        ]);
        this._procs.add(proc);
        let timedOut = false;
        const timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,
            timeout ?? this._settings.get_int('cli-timeout'), () => {
                timedOut = true;
                proc.force_exit();
                return GLib.SOURCE_REMOVE;
            });
        let result = null;
        let stderr = '';
        try {
            // Write stdin while already reading stdout: a multi-MB image
            // must not deadlock against a full stdout pipe.
            // EPIPE if claude dies early: handled now, not when awaited.
            const writing = writeAll(proc.get_stdin_pipe(),
                new TextEncoder().encode(input)).catch(() => {});
            const errText = readAll(proc.get_stderr_pipe())
                .catch(e => `(stderr unreadable: ${e.message})`);
            result = await readStream(proc.get_stdout_pipe(), onText);
            await writing;
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
        if (result.is_error || result.subtype !== 'success') {
            throw new Error(`claude CLI: ${
                String(result.result ?? '').trim() || result.subtype}`);
        }
        return result;
    }

    destroy() {
        for (const proc of this._procs)
            proc.force_exit();
        this._procs.clear();
        this._settings = null;
    }
}

async function writeAll(stream, data) {
    // Gio.Subprocess's stdin pipe is a blocking fd, and GIO's async write on
    // a pipe first writes in the calling thread: it would block the whole
    // shell until claude starts reading (~0.3 s of Node start-up). So make
    // it non-blocking — and then hand GIO a GLib.Bytes, which it keeps a
    // reference to: a plain Uint8Array is only valid during the call, so
    // the part written later (past the 64 KB pipe buffer) would be garbage.
    GLibUnix.set_fd_nonblocking(stream.get_fd(), true);
    let bytes = new GLib.Bytes(data);
    try {
        while (bytes.get_size() > 0) {
            const n = await stream.write_bytes_async(bytes,
                GLib.PRIORITY_DEFAULT, null);
            bytes = GLib.Bytes.new_from_bytes(bytes, n,
                bytes.get_size() - n);
        }
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
function transcript(turns, question, suffix) {
    const lines = ['Conversation so far about this screenshot:'];
    for (const turn of turns) {
        if (turn.question)
            lines.push(`User: ${turn.question}`);
        lines.push(`You: ${turn.answer}`);
    }
    lines.push('', `New question: ${question}`);
    if (suffix)
        lines.push(suffix);
    return lines.join('\n');
}

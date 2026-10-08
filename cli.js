// cli.js — ask the locally installed Claude Code CLI (`claude -p`).
//
// Uses the user's existing Claude login, no API key. Tuned for latency:
// safe mode (no plugins/hooks/MCP/CLAUDE.md), no tools, a short custom system
// prompt, low effort, no session files, no background traffic. One process
// per request; stateless follow-ups carry the transcript as text.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

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
     *   turns: [{question?, answer}], question?}
     * @returns {Promise<{text: string}>}
     */
    async send({model, system, image, turns, question}) {
        const bin = this._binary;
        if (!bin)
            throw new Error('claude CLI not found');

        const content = [{
            type: 'image',
            source: {
                type: 'base64',
                media_type: image.mediaType,
                data: GLib.base64_encode(image.bytes),
            },
        }];
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
            '--effort', this._settings.get_string('effort'),
            '--tools', '',
            '--strict-mcp-config',
            '--no-session-persistence',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            '--system-prompt', system,
        ]);
        this._procs.add(proc);
        const timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,
            this._settings.get_int('cli-timeout'), () => {
                proc.force_exit();
                return GLib.SOURCE_REMOVE;
            });
        let stdout, stderr;
        try {
            [stdout, stderr] = await proc.communicate_utf8_async(input, null);
        } finally {
            GLib.source_remove(timer);
            this._procs.delete(proc);
        }

        const result = (stdout ?? '').split('\n').reverse()
            .map(line => {
                try {
                    return JSON.parse(line);
                } catch {
                    return null;
                }
            })
            .find(msg => msg?.type === 'result');
        if (!result) {
            const why = proc.get_if_signaled() ? 'timed out'
                : (stderr ?? '').trim().split('\n').pop();
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

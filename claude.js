// claude.js — minimal Claude Messages API client over Soup 3, streaming.
//
// GJS has no official Anthropic SDK, so this speaks raw HTTP + SSE.
// Tuned for latency (measured on Haiku 5.5): thinking off by default
// (~0.4 s faster), streamed so the first words show after ~0.7 s, and a
// pre-warmed keep-alive connection. Both are settings (thinking, stream).

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Soup from 'gi://Soup';

Gio._promisify(Soup.Session.prototype, 'send_async');
Gio._promisify(Soup.Session.prototype, 'send_and_read_async');
// GNOME Shell already promisifies read_line_async with the bytes finish
// function, so a second _promisify (e.g. with read_line_finish_utf8) is a
// no-op: lines may come back as Uint8Array.
Gio._promisify(Gio.DataInputStream.prototype, 'read_line_async');
Gio._promisify(Gio.InputStream.prototype, 'read_bytes_async');

const DEFAULT_BASE = 'https://api.anthropic.com';
const DEFAULT_MODEL = 'claude-haiku-5-5';
// Thinking tokens count toward max_tokens, so higher effort needs more room.
const MAX_TOKENS = {low: 8000, medium: 8000, high: 16000, xhigh: 32000,
    max: 32000};

export class ClaudeClient {
    constructor(settings) {
        this._settings = settings;
        // Inactivity timeout; streaming sends pings, so this rarely bites.
        this._session = new Soup.Session({timeout: 300});
        this._cancellable = new Gio.Cancellable();
    }

    get hasApiKey() {
        return this._apiKey !== '';
    }

    get model() {
        return this._settings.get_string('model').trim() || DEFAULT_MODEL;
    }

    get _apiKey() {
        return this._settings.get_string('claude-api-key').trim();
    }

    get _base() {
        return (this._settings.get_string('api-base-url').trim() ||
            DEFAULT_BASE).replace(/\/+$/, '');
    }

    _message(method, path) {
        const message = Soup.Message.new(method, `${this._base}${path}`);
        message.request_headers.append('x-api-key', this._apiKey);
        message.request_headers.append('anthropic-version', '2023-06-01');
        return message;
    }

    /**
     * Opens (DNS + TCP + TLS) the keep-alive connection while the user is
     * still selecting, with a free metadata request. Errors are ignored.
     */
    warmUp() {
        if (!this.hasApiKey)
            return;
        const message = this._message('GET',
            `/v1/models/${encodeURIComponent(this.model)}`);
        this._session.send_and_read_async(message, GLib.PRIORITY_LOW,
            this._cancellable).catch(() => {});
    }

    /** User content block for a screenshot. */
    imageBlock(bytes, mediaType) {
        return {
            type: 'image',
            source: {
                type: 'base64',
                media_type: mediaType,
                data: GLib.base64_encode(bytes),
            },
        };
    }

    /**
     * @param {object} req {model, system, messages, effort?, thinking?}
     *   effort/thinking override the settings (the [Opus] button).
     * @param {Function} [onText] called with the answer text so far
     * @returns {Promise<{content: object[], text: string}>} raw assistant
     *   content (kept verbatim for follow-ups) and its joined text.
     */
    async send({model, system, messages, effort, thinking}, onText = null) {
        if (!this.hasApiKey)
            throw new Error('Claude API key not set');

        const message = this._message('POST', '/v1/messages');
        // No temperature/top_p/top_k: Haiku 5.5 rejects non-default values.
        effort ??= this._settings.get_string('effort');
        thinking ??= this._settings.get_boolean('thinking');
        const stream = this._settings.get_boolean('stream');
        const body = {
            model,
            max_tokens: MAX_TOKENS[effort] ?? 8000,
            output_config: {effort},
            stream,
            messages,
        };
        // Thinking on = omit the field (adaptive is the default; Opus 5.5
        // rejects any explicit "disabled").
        if (!thinking)
            body.thinking = {type: 'disabled'};
        if (system)
            body.system = system;
        message.set_request_body_from_bytes('application/json',
            new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body))));

        const input = await this._session.send_async(message,
            GLib.PRIORITY_DEFAULT, this._cancellable);
        const status = message.get_status();
        if (status !== Soup.Status.OK) {
            const raw = await readAll(input, this._cancellable);
            let detail = raw.slice(0, 300);
            try {
                detail = JSON.parse(raw).error?.message ?? detail;
            } catch {}
            throw new Error(`Claude API ${status}: ${detail}`);
        }

        let content, stopReason;
        if (stream) {
            ({content, stopReason} = await readEvents(input,
                this._cancellable, onText));
        } else {
            const data = JSON.parse(await readAll(input, this._cancellable));
            content = data.content ?? [];
            stopReason = data.stop_reason;
        }
        if (stopReason === 'refusal')
            throw new Error('Claude declined to answer this one.');

        // Responses can start with thinking blocks: select by type.
        const text = content.filter(b => b.type === 'text')
            .map(b => b.text).join('').trim();
        if (!text) {
            throw new Error(stopReason === 'max_tokens'
                ? 'Answer was cut off (max_tokens)'
                : 'Empty answer from Claude');
        }
        return {content, text};
    }

    abort() {
        this._cancellable?.cancel();
        this._session?.abort();
        this._cancellable = new Gio.Cancellable();
    }

    destroy() {
        this.abort();
        this._session = null;
        this._settings = null;
    }
}

async function readAll(stream, cancellable) {
    const chunks = [];
    for (;;) {
        const bytes = await stream.read_bytes_async(65536,
            GLib.PRIORITY_DEFAULT, cancellable);
        if (bytes.get_size() === 0)
            break;
        chunks.push(new TextDecoder().decode(bytes.toArray()));
    }
    return chunks.join('');
}

/**
 * Parses the SSE stream back into content blocks (thinking blocks keep their
 * signature, so follow-ups can replay them verbatim).
 */
async function readEvents(stream, cancellable, onText) {
    const lines = new Gio.DataInputStream({
        base_stream: stream,
        newline_type: Gio.DataStreamNewlineType.ANY,
    });
    const content = [];
    let stopReason = null;
    let text = '';
    for (;;) {
        const [raw] = await lines.read_line_async(GLib.PRIORITY_DEFAULT,
            cancellable);
        if (raw === null)
            break;
        const line = typeof raw === 'string' ? raw
            : new TextDecoder().decode(raw);
        if (!line.startsWith('data:'))
            continue;
        const event = JSON.parse(line.slice(5));
        switch (event.type) {
        case 'content_block_start':
            content[event.index] = {...event.content_block};
            break;
        case 'content_block_delta': {
            const block = content[event.index];
            const d = event.delta;
            if (d.type === 'text_delta') {
                block.text = (block.text ?? '') + d.text;
                text += d.text;
                onText?.(text);
            } else if (d.type === 'thinking_delta') {
                block.thinking = (block.thinking ?? '') + d.thinking;
            } else if (d.type === 'signature_delta') {
                block.signature = d.signature;
            }
            break;
        }
        case 'message_delta':
            stopReason = event.delta?.stop_reason ?? stopReason;
            break;
        case 'error':
            throw new Error(`Claude API: ${event.error?.message ??
                'stream error'}`);
        }
    }
    lines.close(null);
    return {content: content.filter(Boolean), stopReason};
}

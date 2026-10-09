// claude.js — minimal Claude Messages API client over Soup 3, streaming.
//
// GJS has no official Anthropic SDK, so this speaks raw HTTP + SSE.
// Tuned for latency (measured on Haiku 5.5): thinking off by default
// (~0.4 s faster), streamed so the first words show after ~0.8 s, and a
// pre-warmed keep-alive connection. Both are settings (thinking, stream).
// No prompt caching on purpose: small crops are below the minimum cacheable
// size, and for full screens a cache hit measured ~0.3 s *slower* to the
// first token.

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
export const DEFAULT_MODEL = 'claude-haiku-5-5';
export const DEFAULT_STRONG_MODEL = 'claude-opus-5-5';
// Thinking tokens count toward max_tokens, so higher effort needs more room.
const MAX_TOKENS = {low: 8000, medium: 8000, high: 16000, xhigh: 32000,
    max: 32000};

export class ClaudeClient {
    constructor(settings) {
        this._settings = settings;
        // Inactivity timeout; streaming sends pings, so this rarely bites.
        this._session = new Soup.Session({timeout: 300});
        this._cancellable = new Gio.Cancellable();
        // model id -> whether it accepts thinking {type: "disabled"}
        // (Models API capability; Opus 5.5 does not).
        this._canDisableThinking = new Map();
        this._capsPending = new Map();
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
     * still selecting, with a free metadata request that also tells whether
     * the model can run without thinking. Errors are ignored.
     */
    warmUp() {
        if (this.hasApiKey)
            this._fetchCapabilities(this.model).catch(() => {});
    }

    /** One shared metadata request per model (warm-up and send). */
    _fetchCapabilities(model) {
        let pending = this._capsPending.get(model);
        if (!pending) {
            pending = this._doFetchCapabilities(model)
                .finally(() => this._capsPending.delete(model));
            this._capsPending.set(model, pending);
        }
        return pending;
    }

    async _doFetchCapabilities(model) {
        const message = this._message('GET',
            `/v1/models/${encodeURIComponent(model)}`);
        const bytes = await this._session.send_and_read_async(message,
            GLib.PRIORITY_LOW, this._cancellable);
        // Any HTTP answer is final for this session (a failed lookup must
        // not make every send wait for another one); network errors throw
        // and are retried.
        let ok;
        if (message.get_status() === Soup.Status.OK) {
            const info = JSON.parse(new TextDecoder().decode(bytes.toArray()));
            ok = info.capabilities?.thinking?.types?.disabled?.supported;
        }
        // No capability info counts as yes (the Haiku default).
        this._canDisableThinking.set(model, ok ?? true);
    }

    async _thinkingCanBeOff(model) {
        if (!this._canDisableThinking.has(model))
            await this._fetchCapabilities(model).catch(() => {});
        return this._canDisableThinking.get(model) ?? true;
    }

    /**
     * @param {object} req {model, system, messages, effort?, thinking?}
     *   effort/thinking override the settings (the [Opus] button).
     * @param {Function} [onText] called with the answer text so far
     * @param {Gio.Cancellable} [cancellable] aborts just this request
     * @returns {Promise<{content: object[], text: string}>} raw assistant
     *   content (kept verbatim for follow-ups) and its joined text.
     */
    async send({model, system, messages, effort, thinking}, onText = null,
        cancellable = null) {
        if (!this.hasApiKey)
            throw new Error('Claude API key not set');

        cancellable ??= this._cancellable;
        const message = this._message('POST', '/v1/messages');
        // libsoup never reuses an idle connection for a non-idempotent POST
        // unless told so; without this the warmed-up connection is wasted.
        message.add_flags(Soup.MessageFlags.IDEMPOTENT);
        // No temperature/top_p/top_k: Haiku 5.5 rejects non-default values.
        effort ??= this._settings.get_string('effort');
        thinking ??= this._settings.get_boolean('thinking');
        // e.g. Opus 5.5 as the main model rejects "disabled" with a 400.
        if (!thinking && !await this._thinkingCanBeOff(model))
            thinking = true;
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
            GLib.PRIORITY_DEFAULT, cancellable);
        let content, stopReason;
        try {
            const status = message.get_status();
            if (status !== Soup.Status.OK) {
                const raw = await readAll(input, cancellable);
                let detail = raw.slice(0, 300);
                try {
                    detail = JSON.parse(raw).error?.message ?? detail;
                } catch {}
                throw new Error(`Claude API ${status}: ${detail}`);
            }
            if (stream) {
                ({content, stopReason} = await readEvents(input, cancellable,
                    onText));
            } else {
                const data = JSON.parse(await readAll(input, cancellable));
                content = data.content ?? [];
                stopReason = data.stop_reason;
            }
        } finally {
            input.close(null);
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

    destroy() {
        this._cancellable.cancel();
        this._session.abort();
        this._session = null;
        this._settings = null;
    }
}

/** User content block for a screenshot (both backends). */
export function imageBlock(bytes, mediaType) {
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
 * Whole stream as text. Decoded once at the end: per-chunk decoding would
 * break UTF-8 sequences split across reads (GJS' TextDecoder has no
 * {stream: true}).
 */
export async function readAll(stream, cancellable = null) {
    const chunks = [];
    let size = 0;
    for (;;) {
        const bytes = await stream.read_bytes_async(65536,
            GLib.PRIORITY_DEFAULT, cancellable);
        if (bytes.get_size() === 0)
            break;
        chunks.push(bytes.toArray());
        size += bytes.get_size();
    }
    const all = new Uint8Array(size);
    let at = 0;
    for (const chunk of chunks) {
        all.set(chunk, at);
        at += chunk.length;
    }
    return new TextDecoder().decode(all);
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
    return {content: content.filter(Boolean), stopReason};
}

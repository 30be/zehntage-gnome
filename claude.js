// claude.js — minimal Claude Messages API client over Soup 3.
//
// GJS has no official Anthropic SDK, so this speaks raw HTTP.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Soup from 'gi://Soup';

Gio._promisify(Soup.Session.prototype, 'send_and_read_async');

const DEFAULT_BASE = 'https://api.anthropic.com';
const DEFAULT_MODEL = 'claude-haiku-5-5';
// Thinking tokens count toward max_tokens, so higher effort needs more room.
const MAX_TOKENS = {low: 8000, medium: 8000, high: 16000, xhigh: 32000,
    max: 32000};

export class ClaudeClient {
    constructor(settings) {
        this._settings = settings;
        // Non-streaming: nothing arrives until the answer is done, and this
        // is an inactivity timeout, so leave room for high effort levels.
        this._session = new Soup.Session({timeout: 300});
    }

    get hasApiKey() {
        return this._settings.get_string('claude-api-key').trim() !== '';
    }

    get model() {
        return this._settings.get_string('model').trim() || DEFAULT_MODEL;
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
     * @param {object} req {model, system, messages}
     * @returns {Promise<{content: object[], text: string}>} raw assistant
     *   content (kept verbatim for follow-ups) and its joined text.
     */
    async send({model, system, messages}) {
        const apiKey = this._settings.get_string('claude-api-key').trim();
        if (!apiKey)
            throw new Error('Claude API key not set');

        const base = (this._settings.get_string('api-base-url').trim() ||
            DEFAULT_BASE).replace(/\/+$/, '');
        const message = Soup.Message.new('POST', `${base}/v1/messages`);
        message.request_headers.append('x-api-key', apiKey);
        message.request_headers.append('anthropic-version', '2023-06-01');

        // No temperature/top_p/top_k: Haiku 5.5 rejects non-default values.
        const effort = this._settings.get_string('effort');
        const body = {
            model,
            max_tokens: MAX_TOKENS[effort] ?? 8000,
            output_config: {effort},
            messages,
        };
        if (system)
            body.system = system;
        message.set_request_body_from_bytes('application/json',
            new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body))));

        const bytes = await this._session.send_and_read_async(
            message, GLib.PRIORITY_DEFAULT, null);
        const raw = new TextDecoder().decode(bytes.get_data() ?? []);

        const status = message.get_status();
        if (status !== Soup.Status.OK) {
            let detail = raw.slice(0, 300);
            try {
                detail = JSON.parse(raw).error?.message ?? detail;
            } catch {}
            throw new Error(`Claude API ${status}: ${detail}`);
        }

        const data = JSON.parse(raw);
        if (data.stop_reason === 'refusal')
            throw new Error('Claude declined to answer this one.');

        // Responses can start with thinking blocks: select by type.
        const content = data.content ?? [];
        const text = content.filter(b => b.type === 'text')
            .map(b => b.text).join('').trim();
        if (!text) {
            throw new Error(data.stop_reason === 'max_tokens'
                ? 'Answer was cut off (max_tokens)'
                : 'Empty answer from Claude');
        }
        return {content, text};
    }

    abort() {
        this._session?.abort();
    }

    destroy() {
        this.abort();
        this._session = null;
        this._settings = null;
    }
}

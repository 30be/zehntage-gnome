// gemini.js — minimal Gemini generateContent client over Soup 3.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Soup from 'gi://Soup';

Gio._promisify(Soup.Session.prototype, 'send_and_read_async');

const BASE = 'https://generativelanguage.googleapis.com/v1beta/models';

export class GeminiClient {
    constructor(settings) {
        this._settings = settings;
        this._session = new Soup.Session({timeout: 60});
    }

    get hasApiKey() {
        return this._settings.get_string('api-key').trim() !== '';
    }

    /** Build the user part for a screenshot turn. */
    imagePart(pngBytes) {
        return {
            inline_data: {
                mime_type: 'image/png',
                data: GLib.base64_encode(pngBytes),
            },
        };
    }

    /**
     * @param {object[]} contents Gemini multi-turn contents array.
     * @returns {Promise<string>} plain-text answer.
     */
    async generate(contents) {
        const apiKey = this._settings.get_string('api-key').trim();
        if (!apiKey)
            throw new Error('API key not set');

        const model = this._settings.get_string('model').trim() ||
            'gemini-3.1-flash-lite';
        const uri = `${BASE}/${encodeURIComponent(model)}:generateContent`;

        const message = Soup.Message.new('POST', uri);
        message.request_headers.append('x-goog-api-key', apiKey);
        const body = JSON.stringify({
            contents,
            generationConfig: {temperature: 0.2},
        });
        message.set_request_body_from_bytes('application/json',
            new GLib.Bytes(new TextEncoder().encode(body)));

        const bytes = await this._session.send_and_read_async(
            message, GLib.PRIORITY_DEFAULT, null);
        const text = new TextDecoder().decode(bytes.get_data());

        if (message.get_status() !== Soup.Status.OK)
            throw new Error(`Gemini API error ${message.get_status()}: ${text.slice(0, 300)}`);

        const data = JSON.parse(text);
        const answer = data?.candidates?.[0]?.content?.parts
            ?.map(p => p.text ?? '')?.join('')?.trim();
        if (!answer)
            throw new Error('Unexpected Gemini response');
        return answer;
    }

    abort() {
        this._session.abort();
    }

    destroy() {
        this.abort();
        this._session = null;
        this._settings = null;
    }
}

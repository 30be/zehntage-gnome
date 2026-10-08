#!/usr/bin/env python3
"""Stand-in for `claude -p` in tests: logs each call, answers stream-json.

ZT_FAKE_DIR (from the test shell's environment) holds `mode`
(ok | error | crash | garbage | hang | slowstream) and receives one calls.jsonl line per invocation.
"""

import json
import os
import sys
from pathlib import Path

d = Path(os.environ['ZT_FAKE_DIR'])
stdin = sys.stdin.read()
mode = (d / 'mode').read_text().strip() if (d / 'mode').exists() else 'ok'
with open(d / 'calls.jsonl', 'a') as f:
    f.write(json.dumps({
        'argv': sys.argv[1:],
        'env': {k: v for k, v in os.environ.items()
                if k.startswith(('DISABLE_', 'CLAUDE_CODE_'))},
        'stdin': stdin,
    }) + '\n')
n = sum(1 for _ in open(d / 'calls.jsonl'))

if mode == 'hang':
    import time
    (d / 'hang.pid').write_text(str(os.getpid()))
    time.sleep(120)
if mode == 'garbage':
    print('this is not json')
    print('Error: weird failure', file=sys.stderr)
    sys.exit(0)
if mode == 'crash':
    print('Error: something exploded', file=sys.stderr)
    sys.exit(1)

out: list[dict] = [{'type': 'system', 'subtype': 'init'}]
if mode == 'error':
    out.append({'type': 'result', 'subtype': 'success', 'is_error': True,
                'result': 'Not logged in · Please run /login'})
else:
    text = ('**Eichhörnchen** — белка (cli).' if n == 1 else
            f'CLI Antwort {n}')
    if mode == 'slowstream':
        text = 'ANFANG ' + 'x' * 20 + ' ENDE'
    if '--include-partial-messages' in sys.argv:
        for i in range(0, len(text), 8):
            out.append({'type': 'stream_event', 'event': {
                'type': 'content_block_delta', 'index': 0,
                'delta': {'type': 'text_delta', 'text': text[i:i + 8]}}})
    out.append({'type': 'assistant', 'message': {
        'content': [{'type': 'text', 'text': text}]}})
    out.append({'type': 'result', 'subtype': 'success', 'is_error': False,
                'result': text, 'duration_ms': 5})
for msg in out:
    print(json.dumps(msg), flush=True)
    if mode == 'slowstream' and msg['type'] == 'stream_event':
        import time
        time.sleep(0.4)

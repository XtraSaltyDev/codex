#!/usr/bin/env python3
"""Diagnose Codex Spark; --live also runs one disposable model/tool task."""
import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import uuid

from health import ClientSession, ClientTimeout, RPC, SparkHealth, check, report


async def live_test(home, url, timeout):
    identity = None
    stage = 'connecting to the mobile backend'
    marker = 'CODEX-DOCTOR-' + uuid.uuid4().hex
    request_id = 'doctor-' + uuid.uuid4().hex
    try:
        token = (home / '.config/codex-spark/mobile.token').read_text().strip()
        async with asyncio.timeout(timeout):
            async with ClientSession(timeout=ClientTimeout(total=timeout)) as session:
                async with RPC(session, url, token, mobile=True) as rpc:
                    stage = 'creating a diagnostic chat'
                    created = await rpc.call('session/new', {'cwd': str(home), 'mcpServers': [], '_meta': {'workspace': {'kind': 'projectless'}}})
                    identity = created['sessionId']
                    cwd = created['_meta']['workingDir']
                    stage = 'running the model and terminal tool'
                    result = await rpc.call('session/prompt', {'sessionId': identity, '_meta': {'goose': {'requestId': request_id}},
                        'prompt': [{'type': 'text', 'text': "Run exactly one terminal command: printf '" + marker + "\\n'. Then reply with exactly " + marker + '. Do not edit files, access the network, or change services. This is a diagnostic check.'}]})
                    if result.get('stopReason') != 'end_turn':
                        raise ValueError('Turn did not finish')
                    streamed_tools = {p['params']['update']['toolCallId'] for p in rpc.events if p.get('params', {}).get('update', {}).get('sessionUpdate') == 'tool_call'}
                    if not streamed_tools:
                        raise ValueError('No tool activity')
                stage = 'reconnecting and loading saved work'
                async with RPC(session, url, token, mobile=True) as rpc:
                    loaded = await rpc.call('session/load', {'sessionId': identity, 'cwd': cwd, 'mcpServers': []})
                    runs = loaded['_meta']['hostRuns']
                    if len(runs) != 1 or runs[0]['state'] != 'finished' or not runs[0]['finalMessageId']:
                        raise ValueError('Saved turn is incomplete')
                    text = ''.join(p.get('params', {}).get('update', {}).get('content', {}).get('text', '') for p in rpc.events
                                   if p.get('params', {}).get('update', {}).get('sessionUpdate') == 'agent_message_chunk')
                    if marker not in text:
                        raise ValueError('Saved answer is missing')
                    receipt = await rpc.call('_goose/unstable/session/submission', {'sessionId': identity, 'requestId': request_id})
                    if receipt.get('state') != 'finished':
                        raise ValueError('Completion receipt missing')
                    stage = 'verifying the recorded command result'
                    verify_command(home, identity, marker, streamed_tools)
                    stage = 'archiving the diagnostic chat'
                    await rpc.call('_goose/unstable/session/archive', {'sessionId': identity})
        return {**check('live', 'Live model/tool test', 'ok', 'Model, terminal tool, durable receipt, and reconnect replay passed; diagnostic chat archived'),
                'sessionId': identity, 'toolActions': 1}
    except Exception:
        detail = 'Live test failed while ' + stage + '. No prompt or tool action was retried.'
        if identity:
            detail += ' Saved diagnostic chat: ' + identity
        return {**check('live', 'Live model/tool test', 'error', detail), 'sessionId': identity}


def verify_command(home, identity, marker, streamed_tools):
    database = home / '.local/state/codex-spark/mobile/recovery.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        snapshots = [json.loads(row[0]) for row in db.execute('SELECT snapshot FROM turns WHERE thread=?', (identity,))]
    commands = [item for turn in snapshots for item in turn['items'] if item['type'] == 'commandExecution']
    if len(commands) != 1 or commands[0].get('id') not in streamed_tools or commands[0].get('exitCode') != 0 or commands[0].get('aggregatedOutput', '').strip() != marker:
        raise ValueError('Expected exactly one successful marker command')


async def diagnose(args):
    home = Path.home()
    health = SparkHealth(home=home)
    result = await health.collect(fresh=True)
    if args.live:
        if result['status'] == 'ok':
            live = await live_test(home, args.mobile_url, args.timeout)
        else:
            live = check('live', 'Live model/tool test', 'skipped', 'Fix the failed checks before running the live test')
        result = report(result['checks'] + [live])
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print('Codex Spark: ' + ('healthy' if result['status'] == 'ok' else 'needs attention'))
        for item in result['checks']:
            print(f"  {item['status'].upper():7} {item['title']}: {item['detail']}")
        if not args.live:
            print('\nInference was not tested. Run codex-spark doctor --live for a model/tool and reconnect check.')
    return 0 if result['status'] == 'ok' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Print a machine-readable report')
    parser.add_argument('--live', action='store_true', help='Run one model/tool task in a new diagnostic chat, reconnect, verify, and archive it')
    parser.add_argument('--timeout', type=int, default=90, help='Total live-test deadline in seconds (10–180; default 90)')
    parser.add_argument('--mobile-url', default='ws://127.0.0.1:3285/acp', help='Private mobile WebSocket endpoint used by --live')
    args = parser.parse_args()
    if not 10 <= args.timeout <= 180:
        parser.error('--timeout must be between 10 and 180 seconds')
    # Diagnostics must not send the mobile secret to an arbitrary remote host.
    from urllib.parse import urlsplit
    url = urlsplit(args.mobile_url)
    if url.scheme != 'ws' or url.hostname not in ('127.0.0.1', '10.66.66.1') or url.username or url.password or url.query or url.fragment:
        parser.error('--mobile-url must be a local or private WireGuard ws:// endpoint')
    os.umask(0o077)
    raise SystemExit(asyncio.run(diagnose(args)))


if __name__ == '__main__':
    main()

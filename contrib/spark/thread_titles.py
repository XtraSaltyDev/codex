"""Generate mobile chat names through isolated Codex app-server turns."""

import asyncio
import contextlib
import json
import logging


MAX_TITLE_CHARS = 36
MAX_PROMPT_BYTES = 960
TITLE_TIMEOUT = 45
TITLE_SCHEMA = {'type': 'object', 'properties': {'title': {
    'type': 'string', 'minLength': 1, 'maxLength': MAX_TITLE_CHARS}},
    'required': ['title'], 'additionalProperties': False}


def title_prompt(text):
    prefix = (
        f'Generate a concise, single-line task title of at most {MAX_TITLE_CHARS} characters '
        'and under five words where possible. Start with an imperative verb. '
        'Capitalize only the first word unless proper nouns, acronyms, or code terms require otherwise. '
        "Preserve ticket references exactly. Write in the user's language. "
        'Do not use quotes, markdown, or trailing punctuation. Do not answer the request. '
        'Return only a JSON object with a title field.\n\nUser prompt:\n')
    remaining = MAX_PROMPT_BYTES - len(prefix.encode())
    return prefix + text.strip().encode()[:remaining].decode('utf-8', errors='ignore')


def parse_title(text):
    if not isinstance(text, str) or len(text.encode()) > 8192:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) != {'title'} or not isinstance(value['title'], str):
        return None
    title = ' '.join(value['title'].strip().strip('\"\'`“”‘’').split()).rstrip('.?!').rstrip()
    return title[:MAX_TITLE_CHARS] or None


def isolated_config(config):
    disabled = ('apps', 'code_mode', 'code_mode_only', 'context_management',
                'current_time_reminder', 'deferred_executor', 'enable_fanout', 'goals',
                'hooks', 'image_generation', 'memories', 'multi_agent', 'multi_agent_v2',
                'plugins', 'request_permissions_tool', 'shell_snapshot', 'shell_tool',
                'standalone_web_search', 'token_budget', 'tool_suggest', 'unified_exec', 'view_image')
    return {**{'features.' + name: False for name in disabled},
            'cloud.skills.enabled': False, 'skills.include_instructions': False,
            'tools.experimental_request_user_input.enabled': False, 'tools.update_plan.enabled': False,
            'web_search': 'disabled', 'default_permissions': ':read-only',
            'mcp_servers': {name: {'enabled': False} for name in config.get('mcp_servers', {})}}


class ThreadTitles:
    def __init__(self, bridge):
        self.bridge = bridge
        self.pending = {}
        self.hidden = {}

    def schedule(self, identity, text):
        if identity in self.pending or not text.strip():
            return
        cancelled = asyncio.Event()
        self.pending[identity] = cancelled
        self.bridge.spawn(self.generate(identity, text, cancelled))

    def cancel(self, identity):
        if identity in self.pending:
            self.pending[identity].set()

    def cancel_all(self):
        for cancelled in self.pending.values():
            cancelled.set()

    def event(self, packet):
        identity = packet.get('params', {}).get('threadId')
        queue = self.hidden.get(identity)
        if queue is not None:
            if packet.get('method') in ('item/completed', 'turn/completed') and 'id' not in packet:
                queue.put_nowait(packet)
            return True
        return False

    async def collect(self, queue, turn_id):
        text = None
        while True:
            packet = await queue.get()
            method, params = packet['method'], packet.get('params', {})
            if method == 'item/completed' and params.get('turnId') == turn_id:
                item = params['item']
                if item['type'] == 'agentMessage':
                    text = item.get('text')
                    if text is not None and len(text.encode()) > 8192:
                        raise ValueError('Title response too large')
            elif method == 'turn/completed' and params['turn']['id'] == turn_id:
                if params['turn']['status'] != 'completed':
                    return None
                return parse_title(text)

    async def generate(self, identity, text, cancelled):
        rpc = self.bridge.rpc
        epoch = rpc.epoch
        temporary = turn_id = None
        finished = False
        collectors = []
        try:
            thread = (await rpc.request('thread/read', {'threadId': identity, 'includeTurns': False}))['thread']
            if thread.get('name') or cancelled.is_set():
                return
            config = (await rpc.request('config/read', {'cwd': thread['cwd'], 'includeLayers': False}))['config']
            if cancelled.is_set():
                return
            model = self.bridge.session_settings(identity).get('modelId') or config.get('model')
            result = await rpc.request('thread/start', {
                'model': model, 'modelProvider': thread.get('modelProvider') or config.get('model_provider'),
                'cwd': thread['cwd'], 'sandbox': 'read-only', 'approvalPolicy': 'never',
                'ephemeral': True, 'threadSource': 'thread_title',
                'runtimeWorkspaceRoots': [], 'environments': [], 'dynamicTools': [],
                'selectedCapabilityRoots': [], 'config': isolated_config(config)})
            temporary = result['thread']['id']
            queue = self.hidden[temporary] = asyncio.Queue()
            if result.get('sandbox', {}).get('type') != 'readOnly':
                raise ValueError('Title thread must be read-only')
            if cancelled.is_set():
                return
            result = await rpc.request('turn/start', {
                'threadId': temporary, 'input': [{'type': 'text', 'text': title_prompt(text)}],
                'outputSchema': TITLE_SCHEMA})
            turn_id = result['turn']['id']
            response = asyncio.create_task(self.collect(queue, turn_id))
            cancellation = asyncio.create_task(cancelled.wait())
            collectors = [response, cancellation]
            done, _ = await asyncio.wait(collectors, timeout=TITLE_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            if response not in done or cancelled.is_set():
                return
            title = response.result()
            finished = True
            if not title:
                return
            thread = (await rpc.request('thread/read', {'threadId': identity, 'includeTurns': False}))['thread']
            if not thread.get('name') and not cancelled.is_set() and rpc.epoch == epoch:
                await rpc.request('thread/name/set', {'threadId': identity, 'name': title})
        except Exception as error:
            logging.debug('Automatic chat naming failed: %s', type(error).__name__)
        finally:
            for task in collectors:
                task.cancel()
            if collectors:
                await asyncio.gather(*collectors, return_exceptions=True)
            if temporary:
                if rpc.ready.is_set() and rpc.epoch == epoch:
                    if turn_id and not finished:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(rpc.request('turn/interrupt', {'threadId': temporary, 'turnId': turn_id}), 5)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(rpc.request('thread/unsubscribe', {'threadId': temporary}), 5)
                self.hidden.pop(temporary, None)
            self.pending.pop(identity, None)

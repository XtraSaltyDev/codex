#!/usr/bin/env python3
"""Verify Responses reasoning levels and optionally refresh their catalog entries."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import tomllib
import urllib.error
import urllib.request

from model_capabilities import CapabilityUnknown, atomic_json, provider_headers


LEVELS = {
    'none': 'Answer without a separate thinking phase.',
    'low': 'Use brief, focused reasoning.',
    'medium': 'Use reasoning for more involved tasks.',
    'high': 'Use extended reasoning.',
    'xhigh': 'Think carefully, validate assumptions, and consider alternatives.',
}


def discover(base_url, model, headers=None, timeout=60):
    evidence = {'model': model['id'], 'base_url': base_url.rstrip('/'),
                'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'probes': {}}
    supported = []
    for level, description in LEVELS.items():
        payload = {'model': model['id'], 'store': False, 'stream': True,
                   'max_output_tokens': 2048, 'reasoning': {'effort': level},
                   'input': [{'role': 'user', 'content': 'Compute 17 multiplied by 19. If thinking is enabled, reason before answering. Reply only with the integer.'}]}
        request = urllib.request.Request(base_url.rstrip('/') + '/responses', data=json.dumps(payload).encode(),
                                         headers={**(headers or {}), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                body = error.read().decode(errors='replace')
            if error.code in (400, 422) and re.search(
                    r'(?:unexpected|unsupported|invalid) reasoning effort|reasoning[._ ]effort.{0,80}(?:not supported|must be|should be)', body, re.I):
                evidence['probes'][level] = {'status': 'unsupported', 'http_status': error.code}
                continue
            raise CapabilityUnknown(f'Reasoning capability is unknown: HTTP {error.code} for {level}. Catalog was not changed.') from error
        except (OSError, TimeoutError) as error:
            raise CapabilityUnknown(f'Reasoning capability is unknown: provider unavailable for {level}. Catalog was not changed.') from error
        try:
            events = [json.loads(line[5:].strip()) for line in raw.splitlines()
                      if line.startswith('data:') and line[5:].strip() != '[DONE]']
            terminal = next(event for event in reversed(events)
                            if event.get('type') in ('response.completed', 'response.incomplete', 'response.failed', 'error'))
            result = terminal['response']
            answer = ''.join(event['delta'] for event in events if event.get('type') == 'response.output_text.delta')
            reasoning_tokens = result['usage']['output_tokens_details']['reasoning_tokens']
            if (terminal['type'] != 'response.completed' or result.get('status') != 'completed'
                    or answer.strip() != '323' or type(reasoning_tokens) is not int
                    or reasoning_tokens < 0 or (reasoning_tokens == 0) != (level == 'none')):
                raise ValueError('The response did not verify the requested thinking mode')
        except (ValueError, KeyError, StopIteration, TypeError) as error:
            raise CapabilityUnknown(f'Reasoning capability is unknown: inconclusive {level} probe. Catalog was not changed.') from error
        supported.append({'effort': level, 'description': description})
        evidence['probes'][level] = {'status': 'verified', 'reasoning_tokens': reasoning_tokens,
                                    'response_id': result.get('id'), 'answer': answer.strip()}
    if not any(option['effort'] == 'none' for option in supported):
        raise CapabilityUnknown('Could not verify the current no-thinking default. Catalog was not changed.')
    return {**evidence, 'supported_reasoning_levels': supported}


def refresh(runtime, apply=False):
    config_text = (runtime / 'config.toml').read_text()
    config = tomllib.loads(config_text)
    provider = config['model_providers'][config['model_provider']]
    headers = provider_headers(provider)
    request = urllib.request.Request(provider['base_url'].rstrip('/') + '/models', headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        model = next((model for model in json.load(response)['data'] if model['id'] == config['model']), None)
    if model is None:
        raise ValueError('The configured model is not served; no files were changed.')
    path = Path(config['model_catalog_json']).expanduser()
    if not path.is_absolute():
        path = runtime / path
    catalog_text = path.read_text()
    catalog = json.loads(catalog_text)
    entry = next((entry for entry in catalog['models'] if entry['slug'] == model['id']), None)
    if entry is None:
        raise ValueError('The configured model is missing from the catalog; no files were changed.')
    evidence = discover(provider['base_url'], model, headers)
    levels = {option['effort'] for option in evidence['supported_reasoning_levels']}
    if any(level is not None and level not in levels for level in
           (entry.get('default_reasoning_level'), config.get('model_reasoning_effort'))):
        raise ValueError('The configured reasoning default was not verified; no files were changed.')
    previous = entry.get('supported_reasoning_levels')
    entry['supported_reasoning_levels'] = evidence['supported_reasoning_levels']
    if apply:
        if path.read_text() != catalog_text or (runtime / 'config.toml').read_text() != config_text:
            raise ValueError('The profile changed during verification; no files were changed. Retry the probe.')
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        shutil.copy2(path, path.with_name(path.name + '.before-reasoning-' + stamp))
        atomic_json(path, catalog)
        atomic_json(runtime / 'model-reasoning.json', evidence)
    return {**evidence, 'previous_supported_reasoning_levels': previous, 'applied': apply, 'catalog': str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path.home() / '.codex-spark')
    parser.add_argument('--refresh', action='store_true', help='Back up and update only the selected model reasoning choices')
    args = parser.parse_args()
    try:
        print(json.dumps(refresh(args.runtime, args.refresh), indent=2))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error


if __name__ == '__main__':
    main()

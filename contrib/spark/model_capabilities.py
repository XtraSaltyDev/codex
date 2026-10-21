#!/usr/bin/env python3
"""Discover served input modalities and optionally refresh only the model catalog."""

import argparse
import base64
import binascii
import datetime as dt
import json
import os
from pathlib import Path
import random
import re
import shutil
import struct
import tomllib
import urllib.error
import urllib.request
import zlib


class CapabilityUnknown(ValueError):
    pass


def parse_modalities(value):
    modalities = value.split(',') if isinstance(value, str) else value
    if (not isinstance(modalities, list) or not modalities or 'text' not in modalities
            or any(m not in ('text', 'image', 'audio') for m in modalities)):
        raise ValueError('Input modalities must include text and use only text,image,audio.')
    return list(dict.fromkeys(modalities))


def image_probe():
    """A small lossless image whose color order is absent from the text prompt."""
    colors = [('blue', (0, 0, 255)), ('red', (255, 0, 0)), ('green', (0, 255, 0))]
    random.SystemRandom().shuffle(colors)
    row = b'\0' + b''.join(bytes(rgb) * 128 for _, rgb in colors)

    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', binascii.crc32(kind + data) & 0xffffffff)

    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('!IIBBBBB', 384, 128, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(row * 128)) + chunk(b'IEND', b''))
    return png, [name for name, _ in colors]


def discover(base_url, model, headers=None, override=None, timeout=45):
    evidence = {'model': model['id'], 'base_url': base_url.rstrip('/'),
                'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    if override is not None:
        return {**evidence, 'input_modalities': parse_modalities(override), 'source': 'explicit_override'}
    if model.get('input_modalities') is not None:
        return {**evidence, 'input_modalities': parse_modalities(model['input_modalities']), 'source': 'models_metadata'}

    png, expected = image_probe()
    payload = {'model': model['id'], 'store': False, 'stream': True, 'max_output_tokens': 256,
               'input': [{'role': 'user', 'content': [
                   {'type': 'input_text', 'text': 'Name the three colored panels from left to right. Reply only with the three color names, separated by spaces.'},
                   {'type': 'input_image', 'detail': 'auto',
                    'image_url': 'data:image/png;base64,' + base64.b64encode(png).decode()}]}]}
    request = urllib.request.Request(base_url.rstrip('/') + '/responses', data=json.dumps(payload).encode(),
                                     headers={**(headers or {}), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors='replace')
        # Only an explicit modality rejection establishes lack of image support.
        rejection = re.search(r'not a multi[- ]?modal model|does not support (?:image|vision)|image inputs? (?:are |is )?(?:not supported|disabled|not allowed)|at most 0 image', body, re.I)
        if error.code in (400, 422) and rejection:
            return {**evidence, 'input_modalities': ['text'], 'source': 'responses_image_probe',
                    'image_probe': {'status': 'unsupported', 'http_status': error.code}}
        raise CapabilityUnknown(f'Image capability is unknown: provider returned HTTP {error.code}. Catalog was not changed.') from error
    except (OSError, TimeoutError) as error:
        raise CapabilityUnknown('Image capability is unknown: provider could not be reached. Catalog was not changed.') from error

    try:
        events = [json.loads(line[5:].strip()) for line in raw.splitlines()
                  if line.startswith('data:') and line[5:].strip() != '[DONE]']
        terminal = next(e for e in reversed(events) if e.get('type') in ('response.completed', 'response.incomplete', 'response.failed'))
        result = terminal['response']
        text = ''.join(e['delta'] for e in events if e.get('type') == 'response.output_text.delta')
        colors = re.findall(r'\b(?:blue|red|green)\b', text.lower())
        if terminal['type'] != 'response.completed' or result.get('status') != 'completed' or colors != expected:
            raise ValueError('Visual response was incomplete or did not identify the image')
    except (ValueError, KeyError, StopIteration, TypeError) as error:
        raise CapabilityUnknown('Image capability is unknown: visual probe was inconclusive. Catalog was not changed; use --input-modalities for an explicit override.') from error
    return {**evidence, 'input_modalities': ['text', 'image'], 'source': 'responses_image_probe',
            'image_probe': {'status': 'verified', 'response_id': result.get('id'), 'expected': expected, 'answer': text}}


def provider_headers(provider):
    headers = dict(provider.get('http_headers') or {})
    for key, variable in (provider.get('env_http_headers') or {}).items():
        headers[key] = os.environ[variable]
    if provider.get('env_key'):
        headers['Authorization'] = 'Bearer ' + os.environ[provider['env_key']]
    return headers


def atomic_json(path, data):
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def refresh(runtime, apply=False, override=None):
    config = tomllib.loads((runtime / 'config.toml').read_text())
    provider = config['model_providers'][config['model_provider']]
    headers = provider_headers(provider)
    request = urllib.request.Request(provider['base_url'].rstrip('/') + '/models', headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        served = json.load(response)['data']
    model = next((m for m in served if m['id'] == config['model']), None)
    if model is None:
        raise ValueError('The configured model is not served by this endpoint.')
    catalog_path = Path(config['model_catalog_json']).expanduser()
    if not catalog_path.is_absolute():
        catalog_path = runtime / catalog_path
    catalog = json.loads(catalog_path.read_text())
    entry = next((m for m in catalog['models'] if m['slug'] == model['id']), None)
    if entry is None:
        raise ValueError('The configured model is missing from the catalog; no files were changed.')
    evidence = discover(provider['base_url'], model, headers, override)
    previous = entry.get('input_modalities')
    entry['input_modalities'] = evidence['input_modalities']
    if apply:
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = catalog_path.with_name(catalog_path.name + '.before-capabilities-' + stamp)
        shutil.copy2(catalog_path, backup)
        atomic_json(catalog_path, catalog)
        atomic_json(runtime / 'model-capabilities.json', evidence)
    return {**evidence, 'previous_input_modalities': previous, 'applied': apply, 'catalog': str(catalog_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path.home() / '.codex-spark')
    parser.add_argument('--refresh', action='store_true', help='Back up and update catalog modalities; preserves config.toml and services')
    parser.add_argument('--input-modalities', help='Explicit override, such as text or text,image')
    args = parser.parse_args()
    try:
        result = refresh(args.runtime, args.refresh, args.input_modalities)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

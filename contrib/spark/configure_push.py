#!/usr/bin/env python3
"""Install a private Apple push key for Goose. Does not restart any service."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path.home() / '.local/share/codex-spark/mobile-python'))
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey, SECP256R1
from cryptography.hazmat.primitives.serialization import load_pem_private_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', required=True, type=Path)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--team-id', required=True)
    parser.add_argument('--topic', default='computer.goose.mobile.dev')
    parser.add_argument('--environment', choices=('sandbox', 'production'), default='sandbox')
    args = parser.parse_args()
    if not all(re.fullmatch('[A-Z0-9]{10}', value) for value in (args.key_id, args.team_id)):
        parser.error('Use the 10-character Apple team and key IDs.')
    if not re.fullmatch(r'[A-Za-z0-9.-]{3,200}', args.topic):
        parser.error('Use the app bundle identifier as the topic.')
    content = args.key_file.expanduser().read_bytes()
    key = load_pem_private_key(content, password=None)
    if not isinstance(key, EllipticCurvePrivateKey) or not isinstance(key.curve, SECP256R1):
        parser.error('An Apple APNs ES256 private key is required.')
    root = Path.home() / '.config/codex-spark'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    destination = root / ('apns-' + args.key_id + '.p8')
    if destination.exists() and destination.read_bytes() != content:
        raise SystemExit('The destination contains a different Apple key; it was preserved.')
    destination.write_bytes(content); destination.chmod(0o600)
    config = root / 'push.json'
    if config.exists():
        backup = root / ('push-before-' + args.key_id + '.json')
        if not backup.exists():
            backup.write_bytes(config.read_bytes()); backup.chmod(0o600)
    stage = config.with_suffix('.tmp')
    stage.write_text(json.dumps({'teamId': args.team_id, 'keyId': args.key_id, 'keyPath': str(destination),
                                'topic': args.topic, 'environment': args.environment}, indent=2) + '\n')
    stage.chmod(0o600); stage.replace(config)
    print('Private Apple push configuration installed. No services were restarted.')


if __name__ == '__main__':
    main()

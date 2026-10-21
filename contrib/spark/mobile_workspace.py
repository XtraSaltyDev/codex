"""Bounded, read-only project browsing for the authenticated mobile connection."""
import difflib
import os
from pathlib import Path
import subprocess
import time

from work_review import MAX_FILE, MAX_DIFF, Tree, visible_text


def project_path(root, relative='', exists=True):
    root = Path(root).resolve(strict=True)
    if not isinstance(relative, str) or '\x00' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Choose a path inside this conversation’s folder.')
    path = (root / relative).resolve(strict=exists)
    if not path.is_relative_to(root) or '.git' in Path(relative).parts:
        raise ValueError('This path is outside the browsable project.')
    return root, path


def private_file(path):
    return path.name.startswith('.env') or path.suffix.lower() in ('.pem', '.key', '.p12', '.pfx')


def browse(root, params):
    root, path = project_path(root, params.get('path', ''))
    offset = params.get('offset', 0)
    if type(offset) is not int or offset < 0 or not path.is_dir():
        raise ValueError('Choose a project folder and a valid page.')
    entries = []
    with os.scandir(path) as children:
        for child in children:
            if child.name == '.git' or (child.name.startswith('.') and not params.get('showHidden')):
                continue
            entries.append({'name': child.name, 'path': str((path / child.name).relative_to(root)),
                            'kind': 'link' if child.is_symlink() else 'directory' if child.is_dir() else 'file'})
            if len(entries) > 20000:
                raise ValueError('This folder has too many entries to browse on mobile.')
    entries.sort(key=lambda item: (item['kind'] != 'directory', item['name'].casefold(), item['name']))
    return {'path': str(path.relative_to(root)) if path != root else '', 'root': str(root),
            'parent': str(path.parent.relative_to(root)) if path != root else None,
            'entries': entries[offset:offset + 200], 'nextOffset': offset + 200 if offset + 200 < len(entries) else None}


def read(root, relative):
    root, path = project_path(root, relative)
    if private_file(path):
        raise ValueError('Credential and private-key files cannot be previewed on mobile.')
    state = Tree(root).state(str(path.relative_to(root)))
    if state['kind'] != 'text':
        return {'path': relative, 'text': '', 'kind': state['kind'], 'truncated': False}
    text, truncated = visible_text(state['text'], MAX_FILE)
    return {'path': relative, 'text': text, 'kind': 'text', 'truncated': truncated, 'redacted': text != state['text'],
            'markdown': path.suffix.lower() in ('.md', '.markdown', '.mdown')}


def changes(root, params):
    root = Path(root).resolve(strict=True)
    scope = params.get('scope', 'unstaged')
    if scope not in ('unstaged', 'staged'):
        raise ValueError('Choose unstaged or staged changes.')
    tree = Tree(root)
    try:
        tree.git('rev-parse', '--show-toplevel')
    except subprocess.CalledProcessError:
        return {'files': [], 'git': False, 'truncated': False, 'scope': scope}
    args = ['diff', '--relative', '--no-ext-diff', '--no-textconv', '--no-renames']
    if scope == 'staged':
        args.append('--cached')
    files = []
    for record in tree.git(*args, '--numstat', '-z', '--', '.').split(b'\0'):
        if not record:
            continue
        added, removed, name = record.split(b'\t', 2)
        path = os.fsdecode(name)
        if private_file(Path(path)) or '.git' in Path(path).parts:
            continue
        files.append({'path': path, 'kind': 'modified', 'added': int(added) if added.isdigit() else None,
                      'removed': int(removed) if removed.isdigit() else None, 'binary': added == b'-'})
    limited = len(files) > 200
    files = files[:201]
    if scope == 'unstaged':
        for name in sorted(tree.names('ls-files', '--others', '--exclude-standard', '-z', '--', '.')):
            if len(files) > 200 or time.monotonic() > tree.deadline:
                limited = True
                break
            if private_file(Path(name)):
                continue
            state = tree.state(name)
            files.append({'path': name, 'kind': 'added', 'added': len(state.get('text', '').splitlines()),
                          'removed': 0, 'binary': state['kind'] != 'text'})
    files.sort(key=lambda item: item['path'])
    if not params.get('path'):
        return {'files': files[:200], 'git': True, 'truncated': limited, 'scope': scope}
    relative = params['path']
    project_path(root, relative, exists=False)
    if private_file(Path(relative)):
        raise ValueError('Credential and private-key files cannot be previewed on mobile.')
    entry = next((item for item in files if item['path'] == relative), None)
    if entry is None:
        raise ValueError('This file no longer has changes in the selected view. Refresh the list.')
    if (entry.get('added') or 0) + (entry.get('removed') or 0) > 5000 or ((root / relative).is_file() and (root / relative).stat().st_size > MAX_FILE):
        return {'file': {**entry, 'diff': '', 'truncated': True, 'hadExistingChanges': False, 'note': 'This diff is too large for mobile preview.'}}
    if entry['kind'] == 'added':
        patch = ''.join(difflib.unified_diff([], tree.state(relative).get('text', '').splitlines(keepends=True),
                                           '/dev/null', 'b/' + relative))
    else:
        patch = tree.git(*args, '--unified=3', '--', relative).decode('utf-8', errors='replace')
    diff, truncated = visible_text(patch, MAX_DIFF)
    return {'file': {**entry, 'diff': diff, 'truncated': truncated, 'hadExistingChanges': False}}

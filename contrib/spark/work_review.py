"""Read-only working-tree snapshots and bounded review payloads for mobile runs."""
import difflib
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import time

MAX_FILE = 128 * 1024
MAX_DIFF = 24 * 1024
MAX_REVIEW = 128 * 1024
MAX_FILES = 80
MAX_BASELINE = 8 * 1024 * 1024


def visible_text(text, limit=MAX_DIFF):
    # Strip terminal control sequences and common credential forms before transport.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', '[REDACTED PRIVATE KEY]', text, flags=re.S)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[\"\x27]?)[^\s\"\x27,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    encoded = text.encode('utf-8', errors='replace')
    return encoded[:limit].decode('utf-8', errors='ignore'), len(encoded) > limit


class Tree:
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.deadline = time.monotonic() + 6

    def git(self, *args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Review deadline exceeded')
        env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_LITERAL_PATHSPECS': '1'}
        result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', self.cwd, *args], capture_output=True,
                                timeout=min(3, remaining), check=True, env=env)
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError('Project listing too large')
        return result.stdout

    def names(self, *args):
        return {os.fsdecode(name) for name in self.git(*args).split(b'\0') if name}

    def folder_names(self):
        names = set()
        ignored = {'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', '__pycache__', 'target', '.build', 'build', 'dist'}
        partial = False
        for directory, folders, files in os.walk(self.cwd, followlinks=False):
            links = [name for name in folders if (Path(directory) / name).is_symlink()]
            folders[:] = [name for name in folders if name not in ignored and name not in links]
            for name in files + links:
                if name.startswith('.env') or name.endswith(('.pem', '.key')):
                    continue
                if len(names) >= 500 or time.monotonic() >= self.deadline:
                    return names, True
                names.add(str((Path(directory) / name).relative_to(self.cwd)))
        return names, partial

    def state(self, path):
        candidate = Path(self.cwd) / path
        try:
            if not candidate.parent.resolve().is_relative_to(Path(self.cwd)):
                return {'kind': 'omitted'}
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                return {'kind': 'symlink', 'text': os.readlink(candidate), 'mode': False}
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                return {'kind': 'omitted'}
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return {'kind': 'omitted'}
                data = stream.read(MAX_FILE + 1)
            return self.content(data, bool(info.st_mode & 0o111))
        except FileNotFoundError:
            return {'kind': 'missing'}
        except (OSError, ValueError):
            return {'kind': 'omitted'}

    @staticmethod
    def content(data, executable=False):
        if len(data) > MAX_FILE:
            return {'kind': 'omitted'}
        if b'\0' in data:
            return {'kind': 'binary', 'digest': hashlib.sha256(data).hexdigest(), 'mode': executable}
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            return {'kind': 'binary', 'digest': hashlib.sha256(data).hexdigest(), 'mode': executable}
        if len(text.splitlines()) > 5000:
            return {'kind': 'omitted'}
        return {'kind': 'text', 'text': text, 'mode': executable}

    def committed(self, commit, path):
        if not commit:
            return {'kind': 'missing'}
        entry = self.git('ls-tree', '-z', commit, '--', path)
        if not entry:
            return {'kind': 'missing'}
        mode, kind, identity = entry.split(b'\t', 1)[0].split()
        if kind != b'blob' or int(self.git('cat-file', '-s', identity.decode())) > MAX_FILE:
            return {'kind': 'omitted'}
        content = self.git('cat-file', 'blob', identity.decode())
        result = self.content(content, mode == b'100755')
        if mode == b'120000' and result['kind'] == 'text':
            result['kind'] = 'symlink'
        return result


def capture(cwd):
    try:
        tree = Tree(cwd)
        folder = False
        try:
            root = tree.git('rev-parse', '--show-toplevel').decode().removesuffix('\n')
            tree.cwd = str(Path(root).resolve())
        except subprocess.CalledProcessError:
            tree.cwd = str(Path(cwd).resolve(strict=True))
            folder = True
        try:
            commit = tree.git('rev-parse', '--verify', 'HEAD').decode().strip() if not folder else None
        except subprocess.CalledProcessError:
            commit = None
        partial = False
        if folder:
            dirty, partial = tree.folder_names()
        else:
            dirty = tree.names('diff', '--no-ext-diff', '--no-textconv', '--name-only', '--no-renames', '-z', commit, '--') if commit else tree.names('ls-files', '-z')
            dirty |= tree.names('ls-files', '--others', '--exclude-standard', '-z')
        states = {}
        used = 0
        for path in sorted(dirty):
            if time.monotonic() >= tree.deadline or used >= MAX_BASELINE or len(states) >= 500:
                partial = True
                break
            state = tree.state(path)
            used += len(state.get('text', '').encode('utf-8'))
            states[path] = state
            partial |= state['kind'] == 'omitted'
        return {'root': tree.cwd, 'commit': commit, 'dirty': sorted(dirty), 'states': states, 'partial': partial, 'folder': folder}
    except Exception:
        return {'unavailable': True}


def finish(baseline, interrupted=False):
    if baseline.get('unavailable'):
        return {'status': 'unavailable', 'files': [], 'notice': 'File review was not captured for this project. Command results remain available.'}
    tree = Tree(baseline['root'])
    files = []
    omitted = 0
    partial = baseline.get('partial', False)
    budget = 0
    try:
        dirty = set(baseline['dirty'])
        if baseline.get('folder'):
            current, limited = tree.folder_names()
            candidates = dirty | current
            partial |= limited
        else:
            candidates = dirty | tree.names('ls-files', '--others', '--exclude-standard', '-z')
            candidates |= (tree.names('diff', '--no-ext-diff', '--no-textconv', '--name-only', '--no-renames', '-z', baseline['commit'], '--')
                           if baseline['commit'] else tree.names('ls-files', '-z'))
        for path in sorted(candidates):
            if time.monotonic() >= tree.deadline or len(files) >= MAX_FILES or budget >= MAX_REVIEW:
                partial = True
                omitted += 1
                break
            if path in dirty:
                before = baseline['states'].get(path, {'kind': 'omitted'})
            else:
                before = {'kind': 'missing'} if baseline.get('folder') else tree.committed(baseline['commit'], path)
            after = tree.state(path)
            if 'omitted' in (before['kind'], after['kind']):
                omitted += 1
                partial = True
                continue
            if before == after:
                continue
            binary = 'binary' in (before['kind'], after['kind'])
            entry = {'path': path, 'kind': 'added' if before['kind'] == 'missing' else 'deleted' if after['kind'] == 'missing' else 'modified',
                     'hadExistingChanges': path in dirty and not baseline.get('folder'), 'binary': binary, 'added': None, 'removed': None, 'diff': '', 'truncated': False}
            if not binary:
                old, new = before.get('text', '').splitlines(keepends=True), after.get('text', '').splitlines(keepends=True)
                opcodes = difflib.SequenceMatcher(None, old, new).get_opcodes()
                entry['added'] = sum(j2-j1 for tag, i1, i2, j1, j2 in opcodes if tag in ('insert', 'replace'))
                entry['removed'] = sum(i2-i1 for tag, i1, i2, j1, j2 in opcodes if tag in ('delete', 'replace'))
                patch = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                                for line in difflib.unified_diff(old, new, 'a/' + path, 'b/' + path))
                entry['diff'], entry['truncated'] = visible_text(patch, min(MAX_DIFF, MAX_REVIEW-budget))
                budget += len(entry['diff'].encode('utf-8'))
                if before.get('mode') != after.get('mode') and before['kind'] != 'missing' and after['kind'] != 'missing':
                    entry['note'] = 'Executable permission changed'
                if before['kind'] == 'symlink' or after['kind'] == 'symlink':
                    entry['note'] = 'Symbolic link target changed; target contents were not read'
            files.append(entry)
    except Exception:
        partial = True
    notices = ['Changes observed in this project during the task. Ignored files and edits outside the project are not included.']
    if baseline.get('folder'):
        notices = ['Changes observed in this folder during the task. Generated folders, credential files, and edits outside it are not included.']
    if any(f['hadExistingChanges'] for f in files):
        notices.append('Some files already had changes; these diffs start from their state before this task.')
    if baseline.get('overlap'):
        partial = True
        notices.append('Another task overlapped in this project; changes cannot be attributed to one task.')
    if interrupted:
        partial = True
        notices.append('Captured after an interruption; later edits may be included. Actions were not repeated.')
    partial |= any(f['truncated'] for f in files)
    if partial:
        notices.append('Review is partial. Large, inaccessible, or omitted content may need inspection on the host.')
    notices.append('Common credential patterns are redacted.')
    return {'status': 'partial' if partial else 'captured', 'files': files, 'omitted': omitted, 'notice': ' '.join(notices)}

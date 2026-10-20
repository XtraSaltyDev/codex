"""Shared Codex Spark release identity for host helpers and the mobile adapter."""

from pathlib import Path
import tomllib


def release_version():
    source_manifest = Path(__file__).resolve().parents[2] / 'codex-rs/Cargo.toml'
    if source_manifest.is_file():
        return tomllib.loads(source_manifest.read_text())['workspace']['package']['version']
    # Installed diagnostics carry a copy of the source release version.
    return Path(__file__).with_name('VERSION').read_text().strip()


VERSION = release_version()

#!/usr/bin/env python3
"""Install a pinned, checksum-verified official restic binary without sudo."""
import argparse
import bz2
import hashlib
import os
import platform
import shutil
import tempfile
import urllib.request
from pathlib import Path

VERSION = '0.19.1'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=Path(__file__).resolve().parent.parent / 'bin/restic')
    args = parser.parse_args()
    system = {'Linux': 'linux', 'Darwin': 'darwin'}.get(platform.system())
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if not system or not arch:
        parser.error('Supported platforms: Linux/macOS on x86_64 or ARM64')
    name = f'restic_{VERSION}_{system}_{arch}.bz2'
    base = f'https://github.com/restic/restic/releases/download/v{VERSION}'
    destination = args.destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Stage next to the destination so the final replacement is atomic.
    with tempfile.TemporaryDirectory(prefix='.restic-install-', dir=destination.parent) as directory:
        directory = Path(directory)
        checksum_request = urllib.request.urlopen(base + '/SHA256SUMS', timeout=60)
        with checksum_request as response:
            checksums = response.read().decode()
        expected = next((line.split()[0] for line in checksums.splitlines()
                         if len(line.split()) == 2 and line.split()[1] == name), None)
        if not expected:
            raise RuntimeError('Release checksum not found')
        archive = directory / name
        digest = hashlib.sha256()
        with urllib.request.urlopen(base + '/' + name, timeout=60) as response, archive.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError('Release checksum mismatch; binary was not installed')
        binary = directory / 'restic'
        with bz2.open(archive, 'rb') as source, binary.open('wb') as output:
            shutil.copyfileobj(source, output)
        binary.chmod(0o755)
        os.replace(binary, destination)
    print(f'Installed restic {VERSION}: {destination}')


if __name__ == '__main__':
    main()

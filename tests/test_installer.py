import bz2
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_restic


class InstallerTests(unittest.TestCase):
    def test_verified_install_and_mismatch_preserves_existing_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'bin/restic'
            binary = b'test executable'
            archive = bz2.compress(binary)
            name = f'restic_{install_restic.VERSION}_linux_amd64.bz2'
            checksum = hashlib.sha256(archive).hexdigest()
            def download(url, **kwargs):
                if url.endswith('SHA256SUMS'):
                    return io.BytesIO(f'{checksum}  {name}\n'.encode())
                return io.BytesIO(archive)
            with patch.object(sys, 'argv', ['installer', '--destination', str(destination)]), patch('platform.system', return_value='Linux'), patch('platform.machine', return_value='x86_64'), patch('urllib.request.urlopen', side_effect=download):
                install_restic.main()
                self.assertEqual(destination.read_bytes(), binary)
                self.assertEqual(destination.stat().st_mode & 0o777, 0o755)
                checksum = '0' * 64
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    install_restic.main()
                self.assertEqual(destination.read_bytes(), binary)
                self.assertEqual(list(destination.parent.glob('.restic-install-*')), [])

"""Self-check for the parts that can break without a Pi: python3 launcher/test_server.py"""
import io
import tarfile
import tempfile
from pathlib import Path

import server


def make_tarball(path, files):
    """Build a tarball shaped like GitHub's source downloads (everything under one top folder)."""
    with tarfile.open(path, 'w:gz') as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f'forestlxrd-RPI-Media-Console-abc1234/{name}' if not name.startswith('/') else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_nm_split():
    assert server.nm_split('*:Kata-5G:80:WPA2:5180 MHz') == ['*', 'Kata-5G', '80', 'WPA2', '5180 MHz']
    assert server.nm_split(r'yes:Cafe\:Free:42') == ['yes', 'Cafe:Free', '42']
    assert server.nm_split(r'a\\:b') == ['a\\', 'b']
    assert server.nm_split('::') == ['', '', '']


def test_install_release():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        good = d / 'good.tar.gz'
        make_tarball(good, {'launcher/server.py': 'print(1)', 'README.md': 'hi'})
        server.install_release('v1.0.0', good.as_uri(), install=d / 'opt')
        current = d / 'opt/current'
        assert current.is_symlink() and (current / 'launcher/server.py').read_text() == 'print(1)'
        assert server.read_version(current) == 'v1.0.0'

        server.install_release('v1.1.0', good.as_uri(), install=d / 'opt')
        assert server.read_version(current) == 'v1.1.0'
        assert (d / 'opt/releases/v1.0.0').exists()  # previous release kept for rollback

        empty = d / 'empty.tar.gz'
        make_tarball(empty, {'README.md': 'no launcher here'})
        try:
            server.install_release('v2.0.0', empty.as_uri(), install=d / 'opt')
            raise AssertionError('installed a release without the launcher')
        except server.Oops:
            pass
        assert server.read_version(current) == 'v1.1.0'  # still on the last good one

        evil = d / 'evil.tar.gz'
        make_tarball(evil, {'launcher/server.py': 'x', '../../escape.txt': 'x'})
        try:
            server.install_release('v3.0.0', evil.as_uri(), install=d / 'opt')
            raise AssertionError('extracted a path outside the release folder')
        except tarfile.FilterError:
            pass
        assert not (d / 'escape.txt').exists() and server.read_version(current) == 'v1.1.0'


if __name__ == '__main__':
    test_nm_split()
    test_install_release()
    print('ok')

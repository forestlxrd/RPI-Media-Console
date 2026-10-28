"""Self-check for the parts that can break without a Pi: python3 launcher/test_server.py"""
import io
import os
import subprocess
import tarfile
import tempfile
import threading
import time
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


def test_hdmi_audio_card():
    with tempfile.TemporaryDirectory() as d:
        for connector, status in (('HDMI-A-1', 'disconnected'), ('HDMI-A-2', 'connected')):
            (Path(d) / f'card1-{connector}').mkdir()
            (Path(d) / f'card1-{connector}/status').write_text(status + '\n')
        assert server.hdmi_audio_card(d) == 'vc4hdmi1'
        (Path(d) / 'card1-HDMI-A-1/status').write_text('connected\n')
        assert server.hdmi_audio_card(d) == 'vc4hdmi0'  # both plugged in: the port next to USB-C wins
        assert server.hdmi_audio_card(d + '/nothing') is None


def test_guide_hold():
    """A fake controller (a FIFO) presses the Xbox button; a short tap must not count, a hold must."""
    with tempfile.TemporaryDirectory() as d:
        fifo = Path(d) / 'event0'
        os.mkfifo(fifo)
        held, stop = threading.Event(), threading.Event()
        t = threading.Thread(target=server.watch_guide_hold, args=(stop, held.set, f'{d}/event*', 0.6))
        t.start()
        time.sleep(0.3)  # let the watcher open the device
        w = os.open(fifo, os.O_WRONLY)
        event = lambda value: os.write(w, server.INPUT_EVENT.pack(0, 0, server.EV_KEY, server.BTN_MODE, value))
        event(1), time.sleep(0.2), event(0)  # a tap
        time.sleep(0.8)
        assert not held.is_set()
        event(1)  # a hold
        assert held.wait(2)
        t.join(2)
        os.close(w)


def test_close_app_leaves_nothing():
    """Kodi starts as a wrapper script plus kodi.bin; closing must end both, even one that ignores SIGTERM."""
    proc = subprocess.Popen(['sh', '-c', 'trap "" TERM; sleep 60 & sleep 60'])
    time.sleep(0.3)
    pids = server.process_tree(proc.pid)
    assert len(pids) >= 3, pids  # sh and both sleeps
    threading.Thread(target=server.close_app, args=(proc, 1)).start()
    proc.wait(5)
    time.sleep(1.5)
    for pid in pids:
        state = Path(f'/proc/{pid}/stat')
        assert not state.exists() or state.read_text().split(') ')[1][0] == 'Z', f'{pid} still running'


if __name__ == '__main__':
    test_nm_split()
    test_install_release()
    test_hdmi_audio_card()
    test_guide_hold()
    test_close_app_leaves_nothing()
    print('ok')

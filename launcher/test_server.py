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


def test_kms_device():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / 'card0').mkdir()  # the Pi 4's 3D-only GPU has no connectors
        (Path(d) / 'card1-HDMI-A-1').mkdir()
        (Path(d) / 'card1-HDMI-A-2').mkdir()
        assert server.kms_device(d) == '/dev/dri/card1'
        assert server.kms_device(d + '/nothing') is None


def gone(pid):
    stat = server.proc_stat(pid)
    return stat is None or stat[0] == 'Z'


def test_run_fullscreen_leaves_nothing():
    """A wrapper that exits first must not leave its child running (that child would keep the screen)."""
    marker = f'sleep {60 + os.getpid() % 7}.123'  # a command line we can find again
    started = time.monotonic()
    server.run_fullscreen(['sh', '-c', f'{marker} & sleep 1.5'])
    orphans = [pid for pid in os.listdir('/proc') if pid.isdigit() and not gone(int(pid))
               and marker.encode() in Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ')]
    assert not orphans, f'left running: {orphans}'
    assert time.monotonic() - started < 10


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
        assert gone(pid), f'{pid} still running'


def test_parse_addresses():
    out = ('1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n'
           '3: wlan0    inet 192.0.2.10/24 brd 192.0.2.255 scope global dynamic noprefixroute wlan0\\       valid_lft 86000sec\n'
           '5: tun0    inet 198.51.100.6/24 scope global noprefixroute tun0\\       valid_lft forever\n'
           '2: eth0    inet 203.0.113.4/24 brd 203.0.113.255 scope global eth0\n')
    assert server.parse_addresses(out) == [
        {'kind': 'Wi-Fi', 'iface': 'wlan0', 'addr': '192.0.2.10'},
        {'kind': 'VPN', 'iface': 'tun0', 'addr': '198.51.100.6'},
        {'kind': 'Cable', 'iface': 'eth0', 'addr': '203.0.113.4'},
    ]
    assert server.parse_addresses('') == []


def test_shell():
    r = server.shell('echo hi; echo oops >&2; exit 3')
    assert r['output'] == 'hi\noops\n' and r['code'] == 3 and not r['timed_out'], r
    assert server.shell('read x; echo "got [$x]"')['output'] == 'got []\n'  # nothing to type into, no hang
    assert server.shell("printf '\\033[1;31mred\\033[0m'")['output'] == 'red'
    try:
        server.shell('  ')
        raise AssertionError('ran an empty command')
    except server.Oops:
        pass
    started = time.monotonic()
    r = server.shell('echo before; sleep 30 | cat', timeout=1)
    assert r['timed_out'] and r['output'] == 'before\n', r
    assert time.monotonic() - started < 5


def test_quick_commands():
    for label, cmd in server.DEFAULTS['quick_commands']:
        check = subprocess.run(['bash', '-n', '-c', cmd], capture_output=True, text=True)
        assert check.returncode == 0, (label, check.stderr)
    find_logs = dict(server.DEFAULTS['quick_commands'])['Steam Link logs']
    with tempfile.TemporaryDirectory() as home:
        saved = os.environ['HOME']
        os.environ['HOME'] = home
        try:
            assert server.shell(find_logs)['output'] == 'No Steam Link logs from the last 4 hours\n'
            logs = Path(home, '.local/share/Valve Corporation/SteamLink')
            logs.mkdir(parents=True)
            (logs / 'streaming log.txt').write_text('line 1\nline 2\n')
            (Path(home, '.local/share') / 'other.log').write_text('not steam\n')
            (logs / 'sdr_config.txt').write_text('{"relays": []}\n')  # settings, not a log
            out = server.shell(find_logs)['output']
            assert out == f'== {logs}/streaming log.txt\nline 1\nline 2\n', out
        finally:
            os.environ['HOME'] = saved


def test_last_app_output():
    """Shows the journal from the last app start on, without journalctl itself."""
    awk = dict(server.DEFAULTS['quick_commands'])['Last app output'].split(' | ', 1)[1]
    journal = ('10:00:00 RPi Media Console v0.6\n10:01:00 starting kodi --standalone\nkodi says hi\n'
               '10:02:00 kodi exited with 0\n10:03:00 starting /usr/bin/steamlink\nSDL: no joysticks\n'
               '10:04:00 steamlink exited with 1\n')
    out = subprocess.run(['bash', '-c', awk], input=journal, capture_output=True, text=True).stdout
    assert out == '10:03:00 starting /usr/bin/steamlink\nSDL: no joysticks\n10:04:00 steamlink exited with 1\n', out


def test_api_wants_local_host():
    """Pages elsewhere can't use the API, even one whose name points at 127.0.0.1."""
    import http.client
    srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port, hosts = srv.server_address[1], server.LOCAL_HOSTS

    def post(host, console='1'):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        c.request('POST', '/api/shell', '{"cmd": "echo hi"}', {'Host': host, 'X-Console': console})
        r = c.getresponse()
        return r.status, r.read()
    try:
        server.LOCAL_HOSTS = (f'localhost:{port}',)
        assert post('evil.example:8787')[0] == 403
        assert post(f'localhost:{port}', console='')[0] == 403
        status, body = post(f'localhost:{port}')
        assert status == 200 and b'hi' in body, (status, body)
    finally:
        server.LOCAL_HOSTS = hosts
        srv.shutdown()


def test_stream_cmd():
    saved, which = dict(server.cfg), server.shutil.which
    try:
        server.cfg.update(pc_host='pc.lan', moonlight_app='Desktop', resolution='1280x720', bitrate_kbps=8000)
        server.shutil.which = lambda name: f'/usr/bin/{name}' if name in ('moonlight-qt', 'steamlink') else None
        server.cfg['stream_app'] = 'moonlight'
        assert server.stream_cmd()[:4] == ['/usr/bin/moonlight-qt', 'stream', 'pc.lan', 'Desktop']
        server.cfg['stream_app'] = 'steamlink'
        assert server.stream_cmd() == ['/usr/bin/steamlink']
        server.shutil.which = lambda name: None
        for app in ('steamlink', 'moonlight'):
            server.cfg['stream_app'] = app
            try:
                server.stream_cmd()
                raise AssertionError(f'{app} missing, but got a command')
            except server.Oops:
                pass
    finally:
        server.cfg.clear(), server.cfg.update(saved)
        server.shutil.which = which


def test_install_steamlink():
    """Asks for the password when sudo wants one, installs once however often it's asked,
    and refreshes the package lists if the first try fails."""
    saved = server.sudo, server.notify, server.shutil.which
    calls, said, release = [], [], threading.Event()

    def fake_sudo(*cmd, password=None, timeout=20):
        if password != 'right':
            return 1, '', 'sudo: a password is required'
        if cmd == ('true',):
            return 0, '', ''
        calls.append(cmd[-1])
        release.wait(5)
        return (1, '', 'E: Unable to locate package steamlink') if len(calls) == 1 else (0, '', '')
    try:
        server.sudo, server.notify, server.shutil.which = fake_sudo, said.append, lambda name: None
        for password in (None, 'wrong'):
            try:
                server.ensure_steamlink(password)
                raise AssertionError('installed without a working password')
            except server.Oops as e:
                assert e.extra == {'needs_password': True}
        assert not calls
        assert not server.ensure_steamlink('right')
        assert not server.ensure_steamlink()  # still going: no second install, no password asked
        release.set()
        for _ in range(50):
            if not server.steamlink_installing.locked():
                break
            time.sleep(0.1)
        assert calls == ['steamlink', 'update', 'steamlink'], calls
        assert said[-1].startswith('Steam Link is installed'), said
        server.shutil.which = lambda name: '/usr/bin/steamlink'
        assert server.ensure_steamlink()
    finally:
        server.sudo, server.notify, server.shutil.which = saved


def test_sudo_password_on_stdin():
    """The password reaches sudo on stdin, never on the command line."""
    with tempfile.TemporaryDirectory() as d:
        fake = Path(d) / 'sudo'
        fake.write_text('#!/bin/sh\nread pw; echo "args=$* pw=$pw"\n')
        fake.chmod(0o755)
        path = os.environ['PATH']
        os.environ['PATH'] = f'{d}:{path}'
        try:
            assert server.sudo('true', password='s3cret')[1] == 'args=-S -p  true pw=s3cret\n'
            assert server.sudo('true')[1] == 'args=-n true pw=\n'
        finally:
            os.environ['PATH'] = path


def test_pc_awake():
    """Moonlight needs Sunshine; for Steam Link, Steam answering is enough."""
    saved, port_open = dict(server.cfg), server.port_open
    try:
        server.port_open = lambda host, port: port == server.STEAM_PORT
        server.cfg['stream_app'] = 'moonlight'
        assert not server.pc_awake('pc.lan')
        server.cfg['stream_app'] = 'steamlink'
        assert server.pc_awake('pc.lan')
    finally:
        server.cfg.clear(), server.cfg.update(saved)
        server.port_open = port_open


if __name__ == '__main__':
    test_nm_split()
    test_install_release()
    test_hdmi_audio_card()
    test_guide_hold()
    test_kms_device()
    test_close_app_leaves_nothing()
    test_run_fullscreen_leaves_nothing()
    test_parse_addresses()
    test_shell()
    test_quick_commands()
    test_last_app_output()
    test_api_wants_local_host()
    test_stream_cmd()
    test_pc_awake()
    test_install_steamlink()
    test_sudo_password_on_stdin()
    print('ok')

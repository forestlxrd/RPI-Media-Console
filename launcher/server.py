#!/usr/bin/env python3
"""RPi Media Console launcher.

Serves the launcher page on localhost, keeps exactly one full-screen thing on the
display (the kiosk browser, Moonlight or Kodi), and installs new GitHub releases.
Stdlib only, so a fresh Raspberry Pi OS needs nothing from pip.
"""
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from glob import glob
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent  # <release>/launcher
ROOT = HERE.parent                      # <release>
INSTALL = Path(os.environ.get('RPI_CONSOLE_HOME', '/opt/rpi-console'))
CONFIG = Path(os.environ.get('RPI_CONSOLE_CONFIG', Path.home() / '.config/rpi-console/config.json'))
PORT = 8787
HOME_URL = f'http://localhost:{PORT}/'
SUNSHINE_PORT = 47989

DEFAULTS = {
    'ha_url': 'http://homeassistant.lan:8123',
    'ha_token': '',
    'gaming_on_script': 'script.gaming_on',
    'gaming_off_script': 'script.gaming_off',
    'pc_host': '',
    'pc_name': 'Forest PC',
    'moonlight_app': 'Steam Big Picture',
    'moonlight_apps': ['Steam Big Picture', 'Desktop'],
    'resolution': '1920x1080',
    'bitrate_kbps': 15000,
    'kodi_cmd': ['kodi', '--standalone'],
    'vpn_interface': 'tun0',
    'github_repo': 'forestlxrd/RPI-Media-Console',
    'github_token': '',
    'auto_update': True,
}
EDITABLE = {'moonlight_app': str, 'resolution': str, 'bitrate_kbps': int, 'auto_update': bool}
PUBLIC = [*EDITABLE, 'moonlight_apps', 'pc_name']


def load_config():
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except FileNotFoundError:
        return dict(DEFAULTS)


cfg = load_config()
lock = threading.Lock()
state = {'activity': None, 'pairing': False, 'net': {}, 'pads': 0, 'message': None,
         'update': {'latest': None, 'checked': None, 'error': None, 'busy': False}}


class Oops(Exception):
    """An error meant for the screen. Extra keyword args go to the UI as-is."""
    def __init__(self, msg, **extra):
        super().__init__(msg)
        self.extra = extra


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def notify(text):
    log('notify:', text)
    with lock:
        state['message'] = {'id': time.monotonic_ns(), 'at': time.time(), 'text': text}


def set_activity(name, phase=None):
    with lock:
        state['activity'] = name and {'name': name, 'phase': phase, 'since': time.time()}


def run(*cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def port_open(host, port, timeout=1.5):
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- updates

def read_version(release_dir):
    try:
        return (Path(release_dir) / '.version').read_text().strip()
    except OSError:
        return 'dev'


VERSION = read_version(ROOT)
INSTALLED = INSTALL in ROOT.parents  # False when run straight from a git checkout


def gh_open(url):
    headers = {'User-Agent': 'rpi-media-console', 'Accept': 'application/vnd.github+json'}
    if cfg['github_token']:  # only needed if the repo goes private
        headers['Authorization'] = f"Bearer {cfg['github_token']}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)


def latest_release():
    with gh_open(f"https://api.github.com/repos/{cfg['github_repo']}/releases/latest") as r:
        data = json.load(r)
    return data['tag_name'], data['tarball_url']


def extract_tarball(src, dest):
    """Extract a GitHub source tarball, dropping its <owner>-<repo>-<sha>/ top folder."""
    with tarfile.open(src) as tar:
        members = []
        for m in tar.getmembers():
            m.name = m.name.partition('/')[2]
            if m.name:
                members.append(m)
        tar.extractall(dest, members=members, filter='data')  # 'data' refuses ../ and absolute paths


def install_release(tag, tarball_url, install=INSTALL):
    """Unpack a release into <install>/releases/<tag> and point <install>/current at it."""
    if not re.fullmatch(r'[\w.+-]+', tag):
        raise Oops(f'Odd release tag {tag!r}, skipping it')
    releases = install / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    dest = releases / tag
    with tempfile.TemporaryDirectory(dir=install) as tmp:
        tmp = Path(tmp)
        with gh_open(tarball_url) as r, open(tmp / 'src.tar.gz', 'wb') as f:
            shutil.copyfileobj(r, f)
        extract_tarball(tmp / 'src.tar.gz', tmp / 'x')
        if not (tmp / 'x/launcher/server.py').is_file():
            raise Oops(f'Release {tag} has no launcher/server.py, skipping it')
        (tmp / 'x/.version').write_text(tag + '\n')
        if dest.resolve() != ROOT:
            shutil.rmtree(dest, ignore_errors=True)
        (tmp / 'x').rename(dest)
    link = install / 'current.new'
    link.unlink(missing_ok=True)
    link.symlink_to(Path('releases') / tag)
    link.replace(install / 'current')  # atomic swap
    # Keep the two newest releases (for rolling back by hand) plus the one running now.
    for old in sorted(releases.iterdir(), key=lambda p: p.stat().st_mtime)[:-2]:
        if old.resolve() != ROOT:
            shutil.rmtree(old, ignore_errors=True)


def pending_version():
    """Version that `current` points at, if it differs from the code running now."""
    if not INSTALLED:
        return None
    installed = read_version(INSTALL / 'current')
    return installed if installed != VERSION else None


def check_for_update(install=True):
    with lock:
        state['update'].update(busy=True, error=None)
    try:
        tag, url = latest_release()
        with lock:
            state['update'].update(latest=tag, checked=time.time())
        if install and INSTALLED and tag not in (VERSION, read_version(INSTALL / 'current')):
            log('installing release', tag)
            install_release(tag, url)
            return tag
    except urllib.error.HTTPError as e:
        msg = 'No releases published yet' if e.code == 404 else f'GitHub answered {e.code}'
        with lock:
            state['update']['error'] = msg
        raise Oops(msg)
    except urllib.error.URLError as e:
        with lock:
            state['update']['error'] = "Couldn't reach GitHub"
        raise Oops("Couldn't reach GitHub") from e
    except (OSError, ValueError, KeyError, tarfile.TarError) as e:
        with lock:
            state['update']['error'] = f'Update failed: {e}'
        raise Oops(f'Update failed: {e}') from e
    finally:
        with lock:
            state['update']['busy'] = False


def update_loop():
    time.sleep(30)  # let Wi-Fi and the VPN come up first
    while True:
        delay = 6 * 3600
        if cfg['auto_update']:
            try:
                check_for_update()
            except Exception as e:
                log('update check failed:', e)
                delay = 15 * 60
        time.sleep(delay)


def update_now():
    tag = check_for_update()
    if not INSTALLED:
        return {'text': f'Newest release is {state["update"]["latest"]}. This copy runs from a git checkout, so it does not update itself.'}
    if tag or pending_version():
        return {'text': f'{tag or pending_version()} is downloaded. Restart the launcher to start using it.'}
    return {'text': "You're on the newest version."}


def restart_self():
    display.stop_kiosk()
    log('restarting into', pending_version() or VERSION)
    target = INSTALL / 'current/launcher/server.py' if INSTALLED else Path(sys.argv[0]).resolve()
    os.execv(sys.executable, [sys.executable, str(target)])


# ---------------------------------------------------------------- display

class Display:
    """Owns the screen: the kiosk browser normally, or one full-screen app at a time."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.kiosk = None

    def kiosk_cmd(self):
        browser = shutil.which('chromium') or shutil.which('chromium-browser')
        return [
            'cage', '-d', '--', browser, HOME_URL,
            '--kiosk', '--no-first-run', '--noerrdialogs', '--disable-infobars',
            '--disable-session-crashed-bubble', '--password-store=basic', '--ozone-platform=wayland',
            '--check-for-update-interval=31536000',
            f'--user-data-dir={Path.home() / ".local/share/rpi-console/chromium"}',
            '--disable-features=Translate',
        ]

    def run(self):
        if not shutil.which('cage'):  # dev machine: open http://localhost:8787 yourself
            log('cage not found, not starting the kiosk; open', HOME_URL)
            while True:
                self.jobs.get()()
        while True:
            self.kiosk = subprocess.Popen(self.kiosk_cmd())
            job = None
            while job is None and self.kiosk.poll() is None:
                try:
                    job = self.jobs.get(timeout=1)
                except queue.Empty:
                    pass
            self.stop_kiosk()
            if job:
                job()
            else:
                log('kiosk exited with', self.kiosk.returncode)
                time.sleep(2)
            if pending_version():  # a natural moment to switch to a new release
                restart_self()

    def stop_kiosk(self):
        if self.kiosk and self.kiosk.poll() is None:
            self.kiosk.terminate()
            try:
                self.kiosk.wait(10)
            except subprocess.TimeoutExpired:
                self.kiosk.kill()
                self.kiosk.wait()


display = Display()


def run_app(name, cmd, helper=None):
    set_activity(name, 'running')
    if helper:
        threading.Thread(target=helper, daemon=True).start()
    try:
        subprocess.run(cmd)
    except OSError as e:
        notify(f"Couldn't start {cmd[0]}: {e.strerror}")
    finally:
        set_activity(None)


# ---------------------------------------------------------------- games

cancel_games = threading.Event()


def ha_script(entity):
    if not cfg['ha_token']:
        return
    req = urllib.request.Request(
        f"{cfg['ha_url'].rstrip('/')}/api/services/script/turn_on",
        data=json.dumps({'entity_id': entity}).encode(),
        headers={'Authorization': f"Bearer {cfg['ha_token']}", 'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as e:
        raise Oops(f"Couldn't reach Home Assistant ({e})") from e


def games_wake():
    host, pc = cfg['pc_host'], cfg['pc_name']
    try:
        if not host:
            raise Oops('Set pc_host in the config file first')
        ha_script(cfg['gaming_on_script'])  # HA wakes the PC and starts Sunshine
        deadline = time.time() + 180
        while not port_open(host, SUNSHINE_PORT):
            if cancel_games.wait(2):
                ha_script(cfg['gaming_off_script'])
                return set_activity(None)
            if time.time() > deadline:
                ha_script(cfg['gaming_off_script'])
                raise Oops(f"{pc} didn't answer within 3 minutes")
        set_activity('games', 'connecting')
        display.jobs.put(stream)
    except Oops as e:
        set_activity(None)
        notify(str(e))


def stream():
    set_activity('games', 'streaming')
    try:
        subprocess.run(['moonlight', 'stream', cfg['pc_host'], cfg['moonlight_app'],
                        '--resolution', cfg['resolution'], '--fps', '60',
                        '--bitrate', str(cfg['bitrate_kbps']), '--display-mode', 'fullscreen'])
    except OSError:
        notify('Moonlight is not installed')
    finally:
        try:
            ha_script(cfg['gaming_off_script'])  # HA only puts the PC to sleep if it woke it
        except Oops as e:
            notify(str(e))
        set_activity(None)


def launch(app):
    with lock:
        if state['activity']:
            raise Oops('Something is already starting')
    if app == 'games':
        cancel_games.clear()
        set_activity('games', 'waking')
        threading.Thread(target=games_wake, daemon=True).start()
    elif app == 'movies':
        display.jobs.put(lambda: run_app('movies', cfg['kodi_cmd'], setup_jellyfin))
    else:
        raise Oops(f'Unknown app {app!r}')


# ---------------------------------------------------------------- Kodi + Jellyfin

KODI_ADDONS = Path.home() / '.kodi/addons'
JELLYFIN_REPO = 'repository.jellyfin.kodi'  # install.sh unpacks it; Kodi leaves copied add-ons disabled
JELLYFIN = 'plugin.video.jellyfin'


def kodi_rpc(method, **params):
    """Call Kodi's JSON-RPC on localhost:9090. Returns the result, or None on an error reply."""
    deadline = time.monotonic() + 10
    with socket.create_connection(('127.0.0.1', 9090), 5) as s:
        s.sendall(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode())
        buf, decoder = '', json.JSONDecoder()
        while time.monotonic() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                raise OSError('Kodi closed the connection')
            buf += chunk.decode(errors='replace')
            while buf.strip():
                try:
                    msg, end = decoder.raw_decode(buf.lstrip())
                except ValueError:
                    break  # half a message so far
                buf = buf.lstrip()[end:]
                if msg.get('id') == 1:  # skip Kodi's notifications
                    return msg.get('result')
                log('kodi said', str(msg)[:200])
        raise OSError(f'Kodi did not answer {method}')


def kodi_builtin(command):
    """Run a Kodi built-in such as InstallAddon(...). JSON-RPC can't; the EventServer on UDP 9777 can."""
    uid = int(time.time()) & 0xFFFFFFFF

    def packet(kind, payload):
        return b'XBMC\x02\x00' + struct.pack('!HIIHI', kind, 1, 1, len(payload), uid) + bytes(10) + payload

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(packet(0x01, b'rpi-console\0' + bytes(11)), ('127.0.0.1', 9777))      # hello
        s.sendto(packet(0x0A, b'\x01' + command.encode() + b'\0'), ('127.0.0.1', 9777))  # run built-in
        s.sendto(packet(0x02, b''), ('127.0.0.1', 9777))                                # bye


def wait_for(check, secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        try:
            if check():
                return True
        except OSError:
            pass  # Kodi still starting
        time.sleep(1)
    return False


def kodi_asks_about_jellyfin():
    """True while Kodi shows a yes/no dialog about the Jellyfin repo or add-on."""
    window = (kodi_rpc('GUI.GetProperties', properties=['currentwindow']) or {}).get('currentwindow', {})
    if window.get('id') != 10100:
        return False
    text = (kodi_rpc('XBMC.GetInfoLabels', labels=['Control.GetLabel(9)']) or {}).get('Control.GetLabel(9)', '')
    return 'Jellyfin' in text


def kodi_say_yes():
    time.sleep(1.5)  # the dialog ignores input while it animates open
    focused = lambda: (kodi_rpc('GUI.GetProperties', properties=['currentcontrol']) or {}).get('currentcontrol', {}).get('label')
    no = focused()  # it opens on "No"; "Yes" sits to its left
    for _ in range(5):
        kodi_rpc('Input.Left')
        time.sleep(0.5)
        if focused() != no:
            break
    kodi_rpc('Input.Select')


def setup_jellyfin():
    """First Movies launch: enable the Jellyfin repo install.sh unpacked and install the Jellyfin
    add-on with its dependencies, answering Kodi's two prompts ("enable this add-on?" at startup
    and "download this add-on?"). The add-on then asks for the server and login itself.
    Runs again on the next launch until the add-on is there."""
    if (KODI_ADDONS / JELLYFIN).is_dir() or not (KODI_ADDONS / JELLYFIN_REPO).is_dir():
        return
    try:
        if not wait_for(lambda: kodi_rpc('JSONRPC.Ping') == 'pong', 90):
            return log('Kodi never answered on JSON-RPC; skipping Jellyfin setup')
        kodi_rpc('Addons.SetAddonEnabled', addonid=JELLYFIN_REPO, enabled=True)
        asked_at = 0
        end = time.monotonic() + 240
        while time.monotonic() < end:
            if kodi_rpc('Addons.GetAddonDetails', addonid=JELLYFIN):
                return log('Jellyfin add-on installed')
            if kodi_asks_about_jellyfin():
                kodi_say_yes()
            elif time.monotonic() - asked_at > 60:  # Kodi may still be reading the repo; ask again later
                kodi_builtin(f'InstallAddon({JELLYFIN})')
                asked_at = time.monotonic()
            time.sleep(2)
        log('Jellyfin add-on did not install; trying again next time')
    except OSError as e:
        log('Jellyfin setup stopped:', e)  # usually Kodi was closed


# ---------------------------------------------------------------- Wi-Fi (NetworkManager)

def nm_split(line):
    """Split one line of `nmcli -t` output, which escapes ':' and '\\' with a backslash."""
    fields, cur, esc = [], '', False
    for ch in line:
        if esc:
            cur, esc = cur + ch, False
        elif ch == '\\':
            esc = True
        elif ch == ':':
            fields.append(cur)
            cur = ''
        else:
            cur += ch
    return fields + [cur]


def nm_error(err):
    if 'Secrets were required' in err or 'password' in err.lower():
        return Oops('Wrong password', needs_password=True)
    if 'No network with SSID' in err:
        return Oops("Can't find that network. Move closer or scan again.")
    lines = [ln for ln in err.strip().splitlines() if ln]
    return Oops(lines[-1].removeprefix('Error: ') if lines else 'Wi-Fi command failed')


def wifi_current():
    _, out, _ = run('nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL', 'dev', 'wifi', 'list', '--rescan', 'no')
    for line in out.splitlines():
        active, ssid, signal = (nm_split(line) + ['', '', ''])[:3]
        if active == 'yes':
            return {'ssid': ssid, 'signal': int(signal or 0)}
    return None


def wifi_saved():
    _, out, _ = run('nmcli', '-t', '-f', 'NAME,UUID,TYPE', 'con', 'show')
    saved = []
    for line in out.splitlines():
        name, uuid, kind = (nm_split(line) + ['', '', ''])[:3]
        if kind == '802-11-wireless':
            _, ssid, _ = run('nmcli', '-g', '802-11-wireless.ssid', 'con', 'show', uuid)
            saved.append({'name': name, 'uuid': uuid, 'ssid': ':'.join(nm_split(ssid.strip())) or name})
    return saved


def wifi_scan():
    rc, out, err = run('nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY,FREQ',
                       'dev', 'wifi', 'list', '--rescan', 'yes', timeout=30)
    if rc:  # NetworkManager refuses a rescan right after another one; use what it has
        rc, out, err = run('nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY,FREQ',
                           'dev', 'wifi', 'list', '--rescan', 'no')
    if rc:
        raise nm_error(err)
    saved = wifi_saved()
    known = {s['ssid'] for s in saved}
    nets = {}
    for line in out.splitlines():
        in_use, ssid, signal, security, freq = (nm_split(line) + [''] * 5)[:5]
        if not ssid:
            continue  # hidden networks have no name to show
        n = nets.setdefault(ssid, {'ssid': ssid, 'signal': 0, 'secure': False, 'bands': [],
                                   'active': False, 'saved': ssid in known})
        n['signal'] = max(n['signal'], int(signal or 0))
        n['secure'] |= security not in ('', '--')
        n['active'] |= in_use == '*'
        mhz = int((freq.split() or ['0'])[0] or 0)
        band = '5 GHz' if mhz >= 4900 else '2.4 GHz'
        if band not in n['bands']:
            n['bands'].append(band)
    networks = sorted(nets.values(), key=lambda n: (not n['active'], -n['signal']))
    return {'networks': networks, 'saved': saved}


def wifi_connect(ssid, password='', hidden=False):
    if not 0 < len(ssid.encode()) <= 32:
        raise Oops('Network names are 1 to 32 characters')
    saved = next((s for s in wifi_saved() if s['ssid'] == ssid), None)
    if saved and not password:
        rc, _, err = run('nmcli', 'con', 'up', 'uuid', saved['uuid'], timeout=60)
    else:
        cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]
        if password:
            cmd += ['password', password]
        if hidden:
            cmd += ['hidden', 'yes']
        rc, _, err = run(*cmd, timeout=60)
        if rc and not saved:  # don't leave a half-made profile behind
            run('nmcli', 'con', 'delete', 'id', ssid)
    if rc:
        raise nm_error(err)


def wifi_forget(uuid):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', uuid or ''):
        raise Oops('Unknown network')
    rc, _, err = run('nmcli', 'con', 'delete', 'uuid', uuid)
    if rc:
        raise nm_error(err)


# ---------------------------------------------------------------- controllers (BlueZ)

MAC = r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}'
PAD_NAME = re.compile(r'controller|gamepad|xbox|8bitdo|dualsense|joy-?con', re.I)
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02\r]')
pair_stop = threading.Event()


def bt_devices(kind):
    _, out, _ = run('bluetoothctl', 'devices', kind)
    return dict(re.findall(rf'Device ({MAC}) (.+)', out))


def controllers():
    connected = bt_devices('Connected')
    result = []
    for mac, name in bt_devices('Paired').items():
        _, info, _ = run('bluetoothctl', 'info', mac)
        battery = re.search(r'Battery Percentage: \S+ \((\d+)\)', info)
        result.append({'mac': mac, 'name': name, 'connected': mac in connected,
                       'battery': int(battery[1]) if battery else None})
    return result


def pads_present():
    return len(glob('/dev/input/js*'))


def pair_controller(timeout, stop):
    """Scan, then pair, trust and connect the first new controller found. Returns its name."""
    known = set(bt_devices('Paired'))
    p = subprocess.Popen(['bluetoothctl'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(ANSI.sub('', ln)) for ln in p.stdout], daemon=True).start()

    def send(cmd):
        p.stdin.write(cmd + '\n')
        p.stdin.flush()

    def expect(pattern, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end and not stop():
            try:
                m = re.search(pattern, lines.get(timeout=1))
            except queue.Empty:
                continue
            if m:
                return m
        return None

    try:
        for cmd in ('agent NoInputNoOutput', 'default-agent', 'scan on'):
            send(cmd)
        end = time.monotonic() + timeout
        while time.monotonic() < end and not stop():
            m = expect(rf'\[(?:NEW|CHG)\] Device ({MAC}) (?:Name: |Alias: )?(.*)', end - time.monotonic())
            if not m:
                break
            mac, name = m[1], m[2].strip()
            if mac in known or not PAD_NAME.search(name):
                continue
            known.add(mac)
            log('pairing', mac, name)
            send('scan off')
            send(f'pair {mac}')
            result = expect(r'Pairing successful|AlreadyExists|Failed to pair', 30)
            if not result or result[0] == 'Failed to pair':
                send('scan on')
                continue
            send(f'trust {mac}')
            send(f'connect {mac}')
            expect(r'Connection successful|Failed to connect', 15)
            return name
        return None
    finally:
        p.kill()


def start_pairing(timeout=60, until_pad=False):
    with lock:
        if state['pairing']:
            return
        state['pairing'] = True
    pair_stop.clear()

    def go():
        try:
            name = pair_controller(timeout, lambda: pair_stop.is_set() or (until_pad and pads_present()))
            if name:
                notify(f'{name} is paired')
            elif not pair_stop.is_set() and not (until_pad and pads_present()):
                notify('No controller showed up. Hold its pair button until the Xbox logo flashes fast, then try again.')
        except OSError as e:
            notify(f"Couldn't start Bluetooth pairing: {e.strerror}")
        finally:
            with lock:
                state['pairing'] = False

    threading.Thread(target=go, daemon=True).start()


def bt_forget(mac):
    if not re.fullmatch(MAC, mac or ''):
        raise Oops('Unknown controller')
    rc, _, err = run('bluetoothctl', 'remove', mac)
    if rc:
        raise Oops(err.strip() or "Couldn't remove it")


def autopair():
    time.sleep(15)  # give already-paired controllers a chance to reconnect
    if shutil.which('bluetoothctl') and not pads_present():
        start_pairing(timeout=120, until_pad=True)


# ---------------------------------------------------------------- status, settings, power

def status_loop():
    ha = urlsplit(cfg['ha_url'])
    while True:
        _, conn, _ = run('nmcli', 'networking', 'connectivity')
        net = {
            'wifi': wifi_current(),
            'connectivity': conn.strip() or None,  # full / limited / portal / none
            'vpn': Path('/sys/class/net', cfg['vpn_interface']).exists(),
            'home': bool(ha.hostname) and port_open(ha.hostname, ha.port or (443 if ha.scheme == 'https' else 80)),
        }
        with lock:
            state['net'], state['pads'] = net, pads_present()
        time.sleep(5)


def snapshot():
    with lock:
        s = json.loads(json.dumps(state))
    s['version'] = VERSION
    s['update']['pending'] = pending_version()
    s['settings'] = {k: cfg[k] for k in PUBLIC}
    return s


def update_settings(changes):
    for key, value in changes.items():
        if key not in EDITABLE:
            raise Oops(f'{key} cannot be changed here')
        try:
            changes[key] = EDITABLE[key](value)
        except (TypeError, ValueError):
            raise Oops(f'Bad value for {key}') from None
    cfg.update(changes)
    stored = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix('.tmp')
    tmp.write_text(json.dumps({**stored, **changes}, indent=2) + '\n')
    tmp.replace(CONFIG)


def power(action):
    if action not in ('poweroff', 'reboot'):
        raise Oops('Unknown power action')
    rc, _, err = run('systemctl', action)
    if rc:
        raise Oops(err.strip() or f"Couldn't {action}")


ROUTES = {
    ('GET', '/api/state'): lambda b: snapshot(),
    ('POST', '/api/launch'): lambda b: launch(b.get('app')),
    ('POST', '/api/cancel'): lambda b: cancel_games.set(),
    ('GET', '/api/wifi'): lambda b: wifi_scan(),
    ('POST', '/api/wifi/connect'): lambda b: wifi_connect(str(b.get('ssid', '')), str(b.get('password', '')), bool(b.get('hidden'))),
    ('POST', '/api/wifi/forget'): lambda b: wifi_forget(b.get('uuid')),
    ('GET', '/api/controllers'): lambda b: {'controllers': controllers()},
    ('POST', '/api/controllers/pair'): lambda b: start_pairing(),
    ('POST', '/api/controllers/cancel'): lambda b: pair_stop.set(),
    ('POST', '/api/controllers/forget'): lambda b: bt_forget(b.get('mac')),
    ('POST', '/api/settings'): lambda b: update_settings(b),
    ('POST', '/api/update'): lambda b: update_now(),
    ('POST', '/api/restart'): lambda b: display.jobs.put(restart_self),
    ('POST', '/api/power'): lambda b: power(b.get('action')),
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE / 'web'), **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')  # a new release must never meet a cached old page
        super().end_headers()

    def do_GET(self):
        if self.path.startswith('/api/'):
            return self.api('GET')
        super().do_GET()

    def do_POST(self):
        # Other origins can't send this header without a CORS preflight, which this server
        # never approves, so a stray web page can't power the Pi off.
        if self.headers.get('X-Console') != '1':
            return self.send_error(403)
        self.api('POST')

    def api(self, method):
        route = ROUTES.get((method, self.path.split('?')[0]))
        if not route:
            return self.send_error(404)
        code = 200
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}') if method == 'POST' else {}
            result = route(body if isinstance(body, dict) else {})
            result = result if isinstance(result, dict) else {'ok': True}
        except Oops as e:
            code, result = 400, {'error': str(e), **e.extra}
        except Exception as e:
            traceback.print_exc()
            code, result = 500, {'error': f'Something broke: {e}'}
        data = json.dumps(result).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    log('RPi Media Console', VERSION)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    for target in (server.serve_forever, status_loop, update_loop, autopair):
        threading.Thread(target=target, daemon=True).start()
    display.run()


if __name__ == '__main__':
    main()

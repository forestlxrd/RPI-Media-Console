#!/usr/bin/env python3
"""RPi Media Console launcher.

Serves the launcher page on localhost, keeps exactly one full-screen thing on the
display (the kiosk browser, Moonlight, Steam Link or Kodi), and installs new GitHub releases.
Stdlib only, so a fresh Raspberry Pi OS needs nothing from pip.
"""
import contextlib
import json
import os
import queue
import re
import select
import shlex
import shutil
import signal
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
# What the last app printed. The journal files it under the login session rather than under
# rpi-console (PAMName= moves the launcher there), so a file of its own is easier to find.
APP_LOG = Path(os.environ.get('RPI_CONSOLE_APP_LOG', Path.home() / '.local/state/rpi-console/last-app.log'))
PORT = 8787
HOME_URL = f'http://localhost:{PORT}/'
SUNSHINE_PORT = 47989
STEAM_PORT = 27036  # Steam listens here for Remote Play (Steam Link) while it runs

DEFAULTS = {
    'ha_url': 'http://homeassistant.lan:8123',
    'ha_token': '',
    'gaming_on_script': 'script.gaming_on',
    'gaming_off_script': 'script.gaming_off',
    'pc_host': '',
    'pc_name': 'Forest PC',
    'stream_app': 'moonlight',  # 'moonlight' (Sunshine) or 'steamlink' (Steam Remote Play)
    'moonlight_app': 'Steam Big Picture',
    'moonlight_apps': ['Steam Big Picture', 'Desktop'],
    'resolution': '1920x1080',
    'bitrate_kbps': 15000,
    'movies_app': 'jellyfin',  # 'jellyfin' (its own TV app, in the browser) or 'kodi'
    'jellyfin_url': 'http://jelly.lan',
    'kodi_cmd': ['kodi', '--standalone'],
    'vpn_interface': 'tun0',
    'vpn_connection': 'home-vpn',  # NetworkManager profile that vpn.sh creates
    'github_repo': 'forestlxrd/RPI-Media-Console',
    'github_token': '',
    'auto_update': True,
    # One-press commands in Settings → Terminal, as [label, command].
    'quick_commands': [
        # The launcher logs as python3 inside the login session, not as the rpi-console unit.
        ['Launcher log', 'journalctl -b -t python3 _UID=$(id -u) -n 80 --no-pager -o cat'],
        ['Last app output', 'tail -n 300 ~/.local/state/rpi-console/last-app.log'],
        ['Steam Link logs', "f=$(find ~/.local/share ~/.config /tmp -ipath '*steam*' -iname '*log*' -type f -mmin -240 2>/dev/null | sort -u); "
                            "[ -n \"$f\" ] || echo 'No Steam Link logs from the last 4 hours'; "
                            "echo \"$f\" | while read -r l; do [ -n \"$l\" ] && { echo \"== $l\"; tail -n 40 \"$l\"; }; done"],
        ['Steam Link files', 'dpkg -l steamlink | tail -n 1; ls -la ~/.local/share/SteamLink 2>&1 | head -n 30'],
        ['Controllers', "ls -l /dev/hidraw* /dev/input/by-id/ 2>&1; grep -E '^(N|H):' /proc/bus/input/devices"],
        ['Moonlight controllers', "grep -i gamepad ~/.local/state/rpi-console/last-app.log | tail -n 40; "
                                  "grep -i multi ~/.config/Moonlight*/*.conf"],
        ['Sound', 'cat /proc/asound/cards; grep -H . /proc/asound/card*/pcm*p/sub*/status 2>/dev/null'],
        ['Network', 'ip -br addr; nmcli -t -f NAME,TYPE,DEVICE connection show --active'],
        # Router ping = the Wi-Fi alone; PC ping = Wi-Fi + internet + VPN. Compare the two.
        # Router ping = the Wi-Fi alone; Home Assistant ping = Wi-Fi + internet + VPN. Windows drops
        # pings by default, so the gaming PC itself can't be measured this way.
        ['Connection', "nmcli -f IN-USE,SSID,CHAN,RATE,SIGNAL dev wifi list --rescan no | grep -E '^(IN-USE|\\*)'; "
                       "echo; echo 'VPN:'; nmcli -g vpn.data connection show home-vpn 2>/dev/null | tr ',' '\\n' | "
                       "grep -iE 'proto|comp' || echo 'proto-tcp not set, so UDP'; "
                       "echo; echo 'Router (Wi-Fi only):'; ping -c 20 -i 0.2 -q $(ip route | awk '/^default via/ && !/tun/ {print $3; exit}') | tail -n 2; "
                       "echo; echo 'Home Assistant (Wi-Fi + internet + VPN):'; "
                       "ping -c 20 -i 0.2 -q $(python3 -c 'import json, os, urllib.parse; "
                       "c = json.load(open(os.path.expanduser(\"~/.config/rpi-console/config.json\"))); "
                       "print(urllib.parse.urlsplit(c.get(\"ha_url\", \"http://homeassistant.lan:8123\")).hostname)') | tail -n 2"],
        ['Disk and memory', 'df -h /; free -h; uptime'],
    ],
}
EDITABLE = {'movies_app': str, 'stream_app': str, 'moonlight_app': str, 'resolution': str, 'bitrate_kbps': int, 'auto_update': bool}
PUBLIC = [*EDITABLE, 'moonlight_apps', 'pc_name', 'quick_commands']


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

    def run(self):
        if not shutil.which('cage'):  # dev machine: open http://localhost:8787 yourself
            log('cage not found, not starting the kiosk; open', HOME_URL)
            while True:
                self.jobs.get()()
        while True:
            self.kiosk = subprocess.Popen(browser_cmd(HOME_URL, 'chromium'), env=app_env())
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
            end_processes(process_tree(self.kiosk.pid))  # cage and every Chromium process under it
            self.kiosk.wait()


def no_password_bubble(profile_dir):
    """Turn off Chromium's "Save password?" bubble, which a controller can't dismiss. It showed up
    after the Pi password dialog. Chromium reads Preferences at start, so this runs before each launch."""
    prefs = Path(profile_dir) / 'Default/Preferences'
    try:
        data = json.loads(prefs.read_text())
    except (OSError, ValueError):
        data = {}
    if data.get('credentials_enable_service') is False and data.get('profile', {}).get('password_manager_enabled') is False:
        return
    data['credentials_enable_service'] = False
    data.setdefault('profile', {})['password_manager_enabled'] = False
    try:
        prefs.parent.mkdir(parents=True, exist_ok=True)
        prefs.write_text(json.dumps(data))
    except OSError as e:
        log("couldn't turn off Chromium's password bubble:", e)


def browser_cmd(url, profile, *extra):
    """Chromium full screen in cage, the launcher's way of showing one web page on the TV."""
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    no_password_bubble(Path.home() / '.local/share/rpi-console' / profile)
    return [
        'cage', '-d', '--', browser, url,
        '--kiosk', '--no-first-run', '--noerrdialogs', '--disable-infobars',
        '--disable-session-crashed-bubble', '--password-store=basic', '--ozone-platform=wayland',
        '--check-for-update-interval=31536000',
        f'--user-data-dir={Path.home() / ".local/share/rpi-console" / profile}',
        '--disable-features=Translate', *extra,
    ]


def jellyfin_cmd():
    site = urlsplit(cfg['jellyfin_url'])
    return browser_cmd(cfg['jellyfin_url'], 'jellyfin',
                       # The Gamepad API only works on secure pages, and Jellyfin is plain http on the LAN.
                       f'--unsafely-treat-insecure-origin-as-secure={site.scheme}://{site.netloc}',
                       '--autoplay-policy=no-user-gesture-required')


display = Display()


GUIDE_HOLD = 5          # seconds the Xbox button is held to close Kodi, Moonlight or Steam Link
EV_KEY, BTN_MODE = 1, 0x13C  # BTN_MODE is the Xbox button
INPUT_EVENT = struct.Struct('llHHi')  # struct input_event: timeval, type, code, value


def hdmi_audio_card(drm='/sys/class/drm'):
    """ALSA card of the HDMI port with a screen on it (vc4hdmi0 is the port next to USB-C)."""
    for connector, card in (('HDMI-A-1', 'vc4hdmi0'), ('HDMI-A-2', 'vc4hdmi1')):
        for status in glob(f'{drm}/card*-{connector}/status'):
            try:
                if Path(status).read_text().strip() == 'connected':
                    return card
            except OSError:
                pass
    return None


def kms_device(drm='/sys/class/drm'):
    """/dev/dri/cardN that drives the HDMI ports. On a Pi 4 the other card is the 3D-only GPU,
    and cage started on that one runs happily with no screen at all."""
    for path in sorted(glob(f'{drm}/card*-HDMI-A-*')):
        return '/dev/dri/' + Path(path).name.split('-', 1)[0]
    return None


def app_env():
    env = dict(os.environ)
    card, gpu = hdmi_audio_card(), kms_device()
    if card:
        env['ALSA_CARD'] = card  # the default ALSA device follows the TV
    if gpu:
        env['WLR_DRM_DEVICES'] = gpu  # cage fails, and gets retried, rather than run screenless
    return env


def watch_guide_hold(stop, on_hold, devices='/dev/input/event*', hold=GUIDE_HOLD):
    """Call on_hold() once any controller's Xbox button has been held for `hold` seconds.
    Needs xpadneo's shift mode off (install.sh does that), or the press only arrives on release."""
    fds, pressed = {}, {}
    try:
        while not stop.is_set():
            for path in glob(devices):
                if path not in fds:
                    try:
                        fds[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    except OSError:
                        fds[path] = None  # unreadable (a keyboard we have no rights to, say)
            ready, _, _ = select.select([fd for fd in fds.values() if fd is not None], [], [], 0.25)
            for path, fd in list(fds.items()):
                if fd not in ready:
                    continue
                try:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError:  # controller switched off or out of range
                    os.close(fd)
                    del fds[path]
                    pressed.pop(path, None)
                    continue
                for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                    _, _, kind, code, value = INPUT_EVENT.unpack_from(data, off)
                    if kind == EV_KEY and code == BTN_MODE:
                        if value:
                            pressed.setdefault(path, time.monotonic())
                        else:
                            pressed.pop(path, None)
            if any(time.monotonic() - t >= hold for t in pressed.values()):
                return on_hold()
    finally:
        for fd in fds.values():
            if fd is not None:
                os.close(fd)


def proc_stat(pid):
    """(state, parent pid, start time) of a process, or None once it is gone."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[0], int(fields[1]), int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def process_tree(root):
    """root and every process under it, as {pid: start time}. The start time tells a
    process apart from a later one that reuses its pid."""
    stats, children = {}, {}
    for path in glob('/proc/[0-9]*/stat'):
        pid = int(path.split('/')[2])
        stat = proc_stat(pid)
        if stat:
            stats[pid] = stat
            children.setdefault(stat[1], []).append(pid)
    tree, todo = {}, [root]
    while todo:
        pid = todo.pop()
        if pid in stats and pid not in tree:
            tree[pid] = stats[pid][2]
            todo += children.get(pid, [])
    return tree


def end_processes(family, grace=5):
    """SIGTERM the processes in {pid: start time} that still run, so they can save their state,
    SIGKILL whatever is left after `grace` seconds, and return once all are gone."""
    def running():
        out = []
        for pid, started in family.items():
            stat = proc_stat(pid)
            if stat and stat[2] == started and stat[0] != 'Z':
                out.append(pid)
        return out

    if not running():
        return
    log('closing', running())
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 3)):
        for pid in running():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        end = time.monotonic() + wait
        while running() and time.monotonic() < end:
            time.sleep(0.2)
        if not running():
            return


def close_app(proc, grace=5):
    """End an app and everything it started (Kodi is a wrapper script plus kodi.bin)."""
    end_processes(process_tree(proc.pid), grace)


on_screen = {}  # processes of the app on the TV now, so a shutdown can take them along
on_screen_lock = threading.Lock()


def seen(pid=None):
    """Add pid's process tree to on_screen, and return a copy of it."""
    with on_screen_lock:
        if pid:
            on_screen.update(process_tree(pid))
        return dict(on_screen)


def run_fullscreen(cmd):
    """Run Kodi, Moonlight or Jellyfin until it exits or the Xbox button is held for GUIDE_HOLD
    seconds. Returns only after every process it started has ended, because anything left
    holds on to the screen and the launcher can't come back."""
    log('starting', shlex.join(cmd))
    APP_LOG.parent.mkdir(parents=True, exist_ok=True)
    out = open(APP_LOG, 'w', buffering=1)
    out.write(f"{time.strftime('%H:%M:%S')} starting {shlex.join(cmd)}\n")
    try:
        proc = subprocess.Popen(cmd, env=app_env(), stdout=out, stderr=subprocess.STDOUT)
    except OSError:
        out.close()
        raise
    stop = threading.Event()
    with on_screen_lock:
        on_screen.clear()
    seen(proc.pid)

    def remember():  # keep track of children too, since they outlive a wrapper that dies first
        while not stop.wait(1):
            seen(proc.pid)

    rememberer = threading.Thread(target=remember, daemon=True)
    rememberer.start()
    watcher = threading.Thread(target=watch_guide_hold, args=(stop, lambda: end_processes(seen(proc.pid))), daemon=True)
    watcher.start()
    try:
        code = proc.wait()
        log(Path(cmd[0]).name, 'exited with', code)
        out.write(f"{time.strftime('%H:%M:%S')} {Path(cmd[0]).name} exited with {code}\n")
        return code
    finally:
        stop.set()
        watcher.join()  # a hold may still be closing things
        rememberer.join()
        end_processes(seen())  # whatever it left behind
        with on_screen_lock:
            on_screen.clear()
        out.close()


def run_app(name, cmd, helper=None):
    set_activity(name, 'running')
    if helper:
        threading.Thread(target=helper, daemon=True).start()
    try:
        run_fullscreen(cmd)
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
        while not pc_awake(host):
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


def pc_awake(host):
    """Sunshine answers once the PC is up (Home Assistant starts it); Steam, for Steam Link, once Steam runs."""
    ports = (SUNSHINE_PORT, STEAM_PORT) if cfg['stream_app'] == 'steamlink' else (SUNSHINE_PORT,)
    return any(port_open(host, port) for port in ports)


# Runs inside cage. Steam Link's own Qt 5.14 has no Wayland plugin, so it runs on X11 through
# cage's Xwayland, as on the Pi OS desktop. The DISPLAY line in last-app.log shows cage started it.
# It found the controller but ignored every press: SDL drops controller events while it thinks
# its window lacks focus, and read the pad through its own HIDAPI driver rather than the evdev
# device Moonlight and the launcher use.
STEAMLINK_IN_CAGE = '''echo "Xwayland display: ${DISPLAY:-none}"
QT_QPA_PLATFORM=xcb SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1 SDL_JOYSTICK_HIDAPI=0 exec "$1"'''
STEAMLINK_PACKAGES = ['steamlink', 'xwayland']

steamlink_installing = threading.Lock()


def sudo(*cmd, password=None, timeout=20):
    """sudo with no terminal: the password, when there is one, goes in on stdin and nowhere else.
    Without one it only works if sudoers lets this user in without a password."""
    if password is None:
        args, extra = ['sudo', '-n', *cmd], {'stdin': subprocess.DEVNULL}
    else:
        args, extra = ['sudo', '-S', '-p', '', *cmd], {'input': password + '\n'}
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **extra)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def ensure_steamlink(password=None):
    """True once Steam Link is installed. Otherwise starts installing it, once, and returns False.
    Raises Oops(needs_password=True) when sudo wants the user's password first."""
    if shutil.which('steamlink') and shutil.which('Xwayland'):
        return True
    if steamlink_installing.locked():
        return False
    if sudo('true', password=password)[0]:
        raise Oops('That password did not work' if password else 'Installing Steam Link needs the Pi password',
                   needs_password=True)
    if steamlink_installing.acquire(blocking=False):
        threading.Thread(target=install_steamlink, args=(password,), daemon=True).start()
    return False


def install_steamlink(password=None):
    try:
        notify('Installing Steam Link. This takes a few minutes.')
        apt = ['env', 'DEBIAN_FRONTEND=noninteractive',
               'apt-get', '-y', '-o', 'DPkg::Lock::Timeout=300']  # wait out a running automatic update
        code, _, err = sudo(*apt, 'install', *STEAMLINK_PACKAGES, password=password, timeout=1800)
        if code:  # the package lists may be too old to find it
            sudo(*apt, 'update', password=password, timeout=600)
            code, _, err = sudo(*apt, 'install', *STEAMLINK_PACKAGES, password=password, timeout=1800)
        if code:
            log('installing steamlink failed:', err.strip()[-2000:])
            notify(f"Couldn't install Steam Link: {(err.strip().splitlines() or ['no reason given'])[-1]}")
        else:
            notify('Steam Link is installed. Press Games to start it.')
    finally:
        steamlink_installing.release()


def stream_cmd():
    """How Games streams: Moonlight straight into the chosen Sunshine app, or Steam Link, whose own
    menu picks the PC (it remembers the last one) and has its own picture settings."""
    if cfg['stream_app'] == 'steamlink':
        steamlink = shutil.which('steamlink')
        if not steamlink:
            raise Oops('Steam Link is not installed yet')
        # Its menus are Qt. Straight on the screen they either fall back to a backend without
        # OpenGL that never redraws, or (eglfs) fail every page flip. In cage it has a Wayland
        # display, as on the Pi OS desktop, but has to be told to use it.
        if not shutil.which('cage'):
            return [steamlink]
        return ['cage', '-d', '--', 'sh', '-c', STEAMLINK_IN_CAGE, 'sh', steamlink]
    moonlight = shutil.which('moonlight-qt') or shutil.which('moonlight')  # Pi OS names it moonlight-qt
    if not moonlight:
        raise Oops('Moonlight is not installed')
    return [moonlight, 'stream', cfg['pc_host'], cfg['moonlight_app'],
            '--resolution', cfg['resolution'], '--fps', '60',
            '--bitrate', str(cfg['bitrate_kbps']), '--display-mode', 'fullscreen']


def stream():
    set_activity('games', 'streaming')
    try:
        run_fullscreen(stream_cmd())
    except Oops as e:
        notify(str(e))
    except OSError as e:
        notify(f"Couldn't start the stream: {e.strerror}")
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
        if cfg['stream_app'] == 'steamlink' and not ensure_steamlink():
            raise Oops("Steam Link is still installing. Press Games again once it's ready.")
        cancel_games.clear()
        set_activity('games', 'waking')
        threading.Thread(target=games_wake, daemon=True).start()
    elif app == 'movies' and cfg['movies_app'] == 'kodi':
        display.jobs.put(lambda: run_app('movies', cfg['kodi_cmd'], setup_jellyfin))
    elif app == 'movies':
        display.jobs.put(lambda: run_app('movies', jellyfin_cmd()))
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
        for cmd in ('power on', 'agent NoInputNoOutput', 'default-agent', 'scan on'):
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


# ---------------------------------------------------------------- terminal

SHELL_TIMEOUT = 30       # seconds a command may run; then it's stopped and what it printed so far is shown
SHELL_KEEP = 100_000     # characters of output kept, from the end, where the answer usually is
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def shell(cmd, timeout=SHELL_TIMEOUT):
    """Run one command line from Settings → Terminal, like a non-interactive SSH session.
    Nothing can be typed into it, so a command that asks a question gets no answer and stops."""
    cmd = (cmd or '').strip()
    if not cmd:
        raise Oops('Type a command first')
    env = {**app_env(), 'TERM': 'dumb', 'PAGER': 'cat', 'SYSTEMD_PAGER': '', 'SYSTEMD_COLORS': '0'}
    proc = subprocess.Popen(['bash', '-lc', cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, cwd=Path.home(), env=env, start_new_session=True)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)  # the whole command, pipes and all
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # something left its group and holds the pipe
            proc.kill()
            out = b''
    text = ANSI.sub('', out.decode('utf-8', 'replace'))
    return {'output': text[-SHELL_KEEP:], 'cut': len(text) > SHELL_KEEP,
            'code': proc.returncode, 'timed_out': timed_out}


def parse_addresses(ip_out, vpn='tun0'):
    """`ip -o -4 addr show` → [{'kind': 'Wi-Fi', 'iface': 'wlan0', 'addr': '…'}], loopback left out."""
    found = []
    for line in ip_out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != 'inet' or parts[1] == 'lo':
            continue
        iface = parts[1]
        kind = ('VPN' if iface == vpn or iface.startswith(('tun', 'wg')) else 'Wi-Fi' if iface.startswith('wl')
                else 'Cable' if iface.startswith(('eth', 'en')) else iface)
        found.append({'kind': kind, 'iface': iface, 'addr': parts[3].split('/')[0]})
    return found


def addresses():
    _, out, _ = run('ip', '-o', '-4', 'addr', 'show')
    return {'host': socket.gethostname(), 'addresses': parse_addresses(out, cfg['vpn_interface'])}


# ---------------------------------------------------------------- status, settings, power

def status_loop():
    ha = urlsplit(cfg['ha_url'])
    vpn_tried = 0
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
        # Away from home, keep the VPN up. NetworkManager stops after a failed attempt (server
        # unreachable, new Wi-Fi), so try again every minute. At home the LAN works directly.
        online = net['connectivity'] not in (None, 'none')
        if online and not net['vpn'] and not net['home'] and time.monotonic() - vpn_tried > 60:
            vpn_tried = time.monotonic()
            threading.Thread(target=run, args=('nmcli', 'connection', 'up', 'id', cfg['vpn_connection']),
                             kwargs={'timeout': 60}, daemon=True).start()
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
    ('GET', '/api/addresses'): lambda b: addresses(),
    ('POST', '/api/shell'): lambda b: shell(b.get('cmd')),
    ('POST', '/api/steamlink/install'): lambda b: {'ready': ensure_steamlink(b.get('password'))},
}
LOCAL_HOSTS = (f'localhost:{PORT}', f'127.0.0.1:{PORT}')


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
        # A page whose own name points at 127.0.0.1 (DNS rebinding) counts as same-origin and
        # could send X-Console; its Host header still gives it away.
        if self.headers.get('Host') not in LOCAL_HOSTS:
            return self.send_error(403)
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


def end_leftovers():
    """End a kiosk or app that outlived an earlier launcher. They run in the login session,
    which a service restart doesn't touch, and one of them would keep the screen."""
    family = {}
    for path in glob('/proc/[0-9]*/cmdline'):
        try:
            if os.stat(Path(path).parent).st_uid != os.getuid():
                continue
            exe = os.path.basename(Path(path).read_bytes().split(b'\0')[0])
        except OSError:
            continue
        if exe in (b'cage', b'kodi.bin', b'moonlight-qt', b'moonlight', b'steamlink'):
            family.update(process_tree(int(path.split('/')[2])))
    end_processes(family)


def shutdown(*_):
    display.stop_kiosk()
    end_processes(seen())
    os._exit(0)


def main():
    log('RPi Media Console', VERSION)
    end_leftovers()
    signal.signal(signal.SIGTERM, shutdown)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    for target in (server.serve_forever, status_loop, update_loop, autopair):
        threading.Thread(target=target, daemon=True).start()
    display.run()


if __name__ == '__main__':
    main()

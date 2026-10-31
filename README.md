# RPi Media Console

A controller-driven launcher for a Raspberry Pi 4 on a TV. Pick **Games** to wake the gaming PC at home and stream it with Moonlight, or **Movies** to open Kodi with your Jellyfin library. Settings cover Wi-Fi, controller pairing, stream quality and updates. The launcher installs new GitHub releases by itself.

## How it fits together

- `launcher/server.py` runs as a systemd service on tty1. It serves the launcher page on `localhost:8787` and keeps one thing on screen at a time: Chromium in a `cage` kiosk, or Moonlight, or Kodi. When Moonlight or Kodi exits, the launcher comes back.
- `launcher/web/` holds the launcher page. The controller drives it through the Gamepad API, and text fields (Wi-Fi passwords) get an Xbox-style on-screen keyboard.
- **Games** asks Home Assistant to run `script.gaming_on`, waits for Sunshine to answer on port 47989, then starts Moonlight. When the stream ends it runs `script.gaming_off`, and Home Assistant decides whether the PC goes back to sleep.
  - Settings → Playback → **Stream games with** switches to Valve's Steam Link instead, which streams from Steam itself (Steam Remote Play) rather than Sunshine. The launcher still wakes the PC, then waits for Sunshine or Steam to answer. Steam Link runs inside `cage` on Xwayland, since its own Qt has no Wayland support. If Valve's `steamlink` package or `xwayland` isn't installed yet, choosing Steam Link installs them (a few minutes; a message says when it's ready). If sudo needs your Pi password, the launcher asks for it on screen and hands it straight to sudo without keeping it. Steam Link has its own menu for picking the PC and the picture quality, so Moonlight's settings hide while it's chosen. The first time, pair it from that menu: it shows a PIN to enter in Steam on the PC. If it doesn't list the PC over the VPN, choose **Other Computer** there.
- **Movies** opens Jellyfin's own TV app full screen in a separate browser window. Settings → Playback switches it to Kodi instead. Kodi plays more video formats on the Pi's hardware; the browser leaves x265/HEVC files for your server to convert.
  - The first time, use a mouse to set Jellyfin's Settings → Display → Display mode to **TV** and turn on Settings → Controls → **Enable gamepad**. Jellyfin keeps both in that browser only.
  - If videos play without sound, set the audio to **FLAC** in Jellyfin's settings in that same window. The browser can't decode AC3/EAC3 or DTS, and FLAC makes Jellyfin convert those tracks. Like the settings above, it lives in that browser only, so set it again if the profile in `~/.local/share/rpi-console/jellyfin` is ever reset.
  - With Kodi, the first launch installs the Jellyfin add-on for you; the add-on then asks once for your server address and login, and your library shows up in Kodi's Movies and TV shows menus.
- **Getting back:** hold the Xbox button for 5 seconds in Jellyfin, Kodi, Moonlight or Steam Link. The launcher closes that app, along with anything it started, and comes back. Moonlight's own combo (Menu + View + LB + RB) ends a stream too.
- **Sound** goes to whichever HDMI port has the TV on it. The launcher checks before starting Kodi or Moonlight, so leave Kodi's audio output on Default.

The server uses only the Python standard library.

## Install

You need Raspberry Pi OS Lite (64-bit, the Trixie-based release or newer) with SSH, plus these pieces, which this repo does not install:

- [moonlight-qt](https://github.com/moonlight-stream/moonlight-docs/wiki/Installing-Moonlight-Qt-on-Raspberry-Pi-4), paired once with Sunshine
- [xpadneo](https://github.com/atar-axis/xpadneo) for Xbox controllers over Bluetooth

Copy your OpenVPN client file to the Pi (for example `scp home.ovpn pi@raspberrypi.local:~`), then on the Pi:

```sh
git clone https://github.com/forestlxrd/RPI-Media-Console
cd RPI-Media-Console
bash install.sh ~/home.ovpn
nano ~/.config/rpi-console/config.json
sudo reboot
```

`install.sh` installs `cage`, Chromium, Kodi and Kodi's controller support, copies the launcher to `/opt/rpi-console`, drops the Jellyfin repository into Kodi, adds your user to the groups it needs, and enables the service. The launcher's log is `journalctl -b -t python3 _UID=$(id -u) -f` (the `PAMName=` login session it needs for the screen moves it out of `journalctl -u rpi-console`), and whatever the last app printed is in `~/.local/state/rpi-console/last-app.log`.

## VPN

`vpn.sh` imports the `.ovpn` file into NetworkManager as `home-vpn`, and asks for a username, password or key passphrase if the file needs one. `install.sh` runs it for you when you pass the file; run `bash vpn.sh other.ovpn` later to swap profiles.

The launcher keeps the VPN up whenever home isn't reachable directly: within a minute of the Pi getting online somewhere else, and again after drops. At home it leaves the VPN off. NetworkManager uses the DNS server your VPN pushes, so names like `homeassistant.lan` resolve through the tunnel if the server pushes Pi-hole. If it doesn't, use IP addresses in the config.

## Config

`~/.config/rpi-console/config.json`. Updates never touch this file.

| Key | Default | What it does |
| --- | --- | --- |
| `pc_host` | | IP of the gaming PC. Sunshine and Moonlight use it. |
| `pc_name` | `Forest PC` | Name shown while the PC wakes up. |
| `ha_url` | `http://homeassistant.lan:8123` | Home Assistant. The launcher also uses it to tell whether home is reachable. |
| `ha_token` | | Long-lived token from a non-admin Home Assistant user. Leave it empty to skip waking the PC. |
| `gaming_on_script`, `gaming_off_script` | `script.gaming_on`, `script.gaming_off` | Scripts Games runs before and after a session. |
| `moonlight_apps` | `["Steam Big Picture", "Desktop"]` | Sunshine apps you can pick in Settings → Streaming. |
| `movies_app` | `jellyfin` | What the Movies tile opens: `jellyfin` or `kodi`. Also in Settings → Playback. |
| `jellyfin_url` | `http://jelly.lan` | Jellyfin's address. |
| `kodi_cmd` | `["kodi", "--standalone"]` | How Movies starts Kodi. |
| `vpn_interface` | `tun0` | Network interface the VPN status reads. |
| `vpn_connection` | `home-vpn` | NetworkManager profile the launcher brings up away from home. |
| `github_repo` | `forestlxrd/RPI-Media-Console` | Where updates come from. |
| `github_token` | | Only needed if the repo goes private. |
| `quick_commands` | launcher log, Steam Link logs, controllers, … | One-press buttons in Settings → Terminal, as `[label, command]` pairs. Setting it replaces the whole list. |

Settings → Terminal shows the Pi's addresses for SSH (on its Wi-Fi, cable and VPN) and runs shell commands from the controller: press A on the command field for the on-screen keyboard, and Menu runs it. Each command gets 30 seconds and can't be typed into, so use it for quick checks rather than editors or prompts. That includes `sudo` when it asks for a password; `journalctl` works without it for users in the `adm` group, which Raspberry Pi OS's first user is. Up and down scroll the output. The buttons under the command field run common checks in one press: the launcher's log, everything the last app (Kodi, Moonlight, Steam Link, Jellyfin) printed, Steam Link's logs, controllers, sound, network, disk.

Settings → Playback also has **Controller vibration** (On / Gentle / Off). It sets xpadneo's `rumble_attenuation` (0, 60 or 100 percent) right away and in `/etc/modprobe.d/rpi-console-xpadneo.conf`, so it asks for the Pi password the first time. Turn it down if the controllers buzz on their own during streams.

Settings → Playback changes `movies_app`, `stream_app` (`moonlight` or `steamlink`), `moonlight_app`, `resolution` and `bitrate_kbps` from the couch.

## Updates

Every 6 hours, and 30 seconds after boot, the launcher asks GitHub for the [latest release](https://github.com/forestlxrd/RPI-Media-Console/releases/latest). If the tag differs from the running version, it downloads the source tarball into `/opt/rpi-console/releases/<tag>` and points `/opt/rpi-console/current` at it. The new version takes over the next time Moonlight or Kodi exits, at the next boot, or when you press **Restart and update** in Settings → Updates.

To ship a change, push it and publish a release:

```sh
gh release create v0.2.0 --generate-notes
```

Drafts and pre-releases don't count. A release without `launcher/server.py` gets skipped. The Pi keeps the two newest releases, so you can roll back over SSH:

```sh
ln -sfn releases/v0.1.0 /opt/rpi-console/current && sudo systemctl restart rpi-console
```

You can turn automatic updates off in Settings → Updates.

## Controls

| Button | Launcher | On-screen keyboard |
| --- | --- | --- |
| Left stick / D-pad | Move | Move |
| A | Select | Type the key |
| B | Back | Close the keyboard |
| X / Y | | Delete / Space |
| LT / RT | | Caps / Symbols |
| LB / RB | | Move the caret |
| Menu | | Done (Enter) |
| View | | Show the password |
| Xbox button, or View + Menu | Home | |
| Hold the Xbox button 5 s | Closes Jellyfin, Kodi, Moonlight or Steam Link and returns here | |

Kodi uses its own controller mapping. If it ignores the controller, set it up once under Settings → System → Input → Configure attached controllers (a mouse helps the first time).

If the Pi boots with no controller connected, it looks for one to pair for two minutes. A controller on a USB cable works without pairing.

If a controller reports a button wrong in the launcher, fix the number in `BUTTONS` at the top of `launcher/web/pad.js`.

## Working on it

On any machine with Python 3.12 or newer:

```sh
python3 launcher/server.py        # then open http://localhost:8787 (arrow keys, Enter, Esc)
python3 launcher/test_server.py   # checks the nmcli parser and the release installer
```

Without `cage` the server skips the kiosk. Wi-Fi and Bluetooth screens show errors unless `nmcli` and `bluetoothctl` exist.

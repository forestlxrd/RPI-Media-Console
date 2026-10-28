# RPi Media Console

A controller-driven launcher for a Raspberry Pi 4 on a TV. Pick **Games** to wake the gaming PC at home and stream it with Moonlight, or **Movies** to open Kodi with your Jellyfin library. Settings cover Wi-Fi, controller pairing, stream quality and updates. The launcher installs new GitHub releases by itself.

## How it fits together

- `launcher/server.py` runs as a systemd service on tty1. It serves the launcher page on `localhost:8787` and keeps one thing on screen at a time: Chromium in a `cage` kiosk, or Moonlight, or Kodi. When Moonlight or Kodi exits, the launcher comes back.
- `launcher/web/` holds the launcher page. The controller drives it through the Gamepad API, and text fields (Wi-Fi passwords) get an Xbox-style on-screen keyboard.
- **Games** asks Home Assistant to run `script.gaming_on`, waits for Sunshine to answer on port 47989, then starts Moonlight. When the stream ends it runs `script.gaming_off`, and Home Assistant decides whether the PC goes back to sleep.
- **Movies** opens Kodi. The first time, the launcher installs the Jellyfin add-on in Kodi for you; the add-on then asks once for your server address and login. After that, your Jellyfin movies and shows sit in Kodi's own Movies and TV shows menus.
- **Getting back:** hold the Xbox button for 5 seconds in Kodi or Moonlight. The launcher closes that app, along with anything it started, and comes back. Moonlight's own combo (Menu + View + LB + RB) ends a stream too.
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

`install.sh` installs `cage`, Chromium, Kodi and Kodi's controller support, copies the launcher to `/opt/rpi-console`, drops the Jellyfin repository into Kodi, adds your user to the groups it needs, and enables the service. Logs go to `journalctl -u rpi-console -f`.

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
| `kodi_cmd` | `["kodi", "--standalone"]` | Command for the Movies tile. |
| `vpn_interface` | `tun0` | Network interface the VPN status reads. |
| `vpn_connection` | `home-vpn` | NetworkManager profile the launcher brings up away from home. |
| `github_repo` | `forestlxrd/RPI-Media-Console` | Where updates come from. |
| `github_token` | | Only needed if the repo goes private. |

Settings → Streaming changes `moonlight_app`, `resolution` and `bitrate_kbps` from the couch.

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
| Hold the Xbox button 5 s | Closes Kodi or Moonlight and returns here | |

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

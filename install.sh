#!/usr/bin/env bash
# Installs the launcher on Raspberry Pi OS Lite (64-bit). Run it from a clone of this repo,
# as your normal user:
#   git clone https://github.com/forestlxrd/RPI-Media-Console && cd RPI-Media-Console && bash install.sh
# Pass your OpenVPN client file to set up the home VPN too:  bash install.sh ~/home.ovpn
# After this, the launcher keeps itself up to date from GitHub releases.
set -euo pipefail
ovpn=${1:+$(realpath "$1")}
cd "$(dirname "$0")"

user=$(id -un)
if [ "$user" = root ]; then echo "Run this as your normal user (it uses sudo where it needs to)."; exit 1; fi
home=/opt/rpi-console
version=$(git describe --tags --always 2>/dev/null || echo dev)

echo "==> Packages"
sudo apt-get update
sudo apt-get install -y cage kodi python3 network-manager bluez
sudo apt-get install -y kodi-peripheral-joystick || echo "    kodi-peripheral-joystick not found; Kodi won't see controllers until you add it."
sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser
sudo apt-get install -y steamlink xwayland || echo "    steamlink not found; Settings → Playback can only stream with Moonlight."

echo "==> Launcher $version into $home"
sudo install -d -o "$user" -g "$user" "$home" "$home/releases"
rm -rf "$home/releases/$version"
mkdir "$home/releases/$version"
tar --exclude=.git -cf - . | tar -xf - -C "$home/releases/$version"
echo "$version" > "$home/releases/$version/.version"
ln -sfn "releases/$version" "$home/current"

mkdir -p ~/.config/rpi-console
if [ ! -f ~/.config/rpi-console/config.json ]; then
  cp config.example.json ~/.config/rpi-console/config.json
  echo "    Wrote ~/.config/rpi-console/config.json. Fill in pc_host and ha_token."
fi

echo "==> Jellyfin repository for Kodi (the first Movies launch installs the add-on from it)"
if [ ! -d ~/.kodi/addons/repository.jellyfin.kodi ]; then
  mkdir -p ~/.kodi/addons
  curl -fsSL -o /tmp/repository.jellyfin.kodi.zip https://kodi.jellyfin.org/repository.jellyfin.kodi.zip
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' /tmp/repository.jellyfin.kodi.zip ~/.kodi/addons
  rm /tmp/repository.jellyfin.kodi.zip
  [ -d ~/.kodi/addons/repository.jellyfin.kodi ] || echo "    The zip didn't contain repository.jellyfin.kodi; install the Jellyfin add-on in Kodi by hand."
fi

echo "==> Permissions for controllers, the GPU, Wi-Fi and Bluetooth"
for g in input video render netdev bluetooth; do
  getent group "$g" >/dev/null && sudo usermod -aG "$g" "$user"
done

echo "==> Bluetooth on (Raspberry Pi OS can start with it switched off)"
sudo rfkill unblock bluetooth || true

echo "==> Xbox button reports presses right away, so holding it closes Kodi or Moonlight"
# xpadneo normally holds the press back until release (for its profile switching).
echo "options hid_xpadneo disable_shift_mode=1" | sudo tee /etc/modprobe.d/rpi-console-xpadneo.conf >/dev/null
echo 1 | sudo tee /sys/module/hid_xpadneo/parameters/disable_shift_mode >/dev/null 2>&1 || true

echo "==> Captive portal detection (hotel and cafe Wi-Fi sign-in pages)"
sudo tee /etc/NetworkManager/conf.d/20-connectivity.conf >/dev/null <<'EOF'
[connectivity]
uri=http://nmcheck.gnome.org/check_network_status.txt
interval=300
EOF
sudo systemctl reload NetworkManager || true

if [ -n "$ovpn" ]; then
  bash vpn.sh "$ovpn"
fi

echo "==> Start on boot"
sed "s/@USER@/$user/" systemd/rpi-console.service | sudo tee /etc/systemd/system/rpi-console.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl set-default graphical.target
sudo systemctl enable rpi-console.service

echo
echo "Done. Reboot to start the launcher: sudo reboot"
echo "Logs: journalctl -u rpi-console -f"

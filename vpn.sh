#!/usr/bin/env bash
# Sets up the home VPN from an OpenVPN client file (.ovpn):
#   bash vpn.sh ~/home.ovpn
# Run it again with another file to replace the profile. install.sh calls this when you
# pass it a .ovpn file. The launcher brings the VPN up whenever home isn't reachable directly.
set -euo pipefail

ovpn=${1:?Usage: bash vpn.sh path/to/client.ovpn}
[ -f "$ovpn" ] || { echo "Can't find $ovpn"; exit 1; }
name=home-vpn  # the launcher's vpn_connection setting points here

echo "==> OpenVPN support for NetworkManager"
sudo apt-get install -y network-manager-openvpn

echo "==> Importing $ovpn as $name"
sudo nmcli connection delete id "$name" >/dev/null 2>&1 || true
out=$(sudo nmcli connection import type openvpn file "$ovpn")
uuid=$(printf '%s\n' "$out" | sed -n 's/.*(\([0-9a-f-]\{36\}\)).*/\1/p')
[ -n "$uuid" ] || { echo "Import failed: $out"; exit 1; }
# System-wide (the launcher has no keyring to ask), and reconnect on its own after drops.
sudo nmcli connection modify "$uuid" connection.id "$name" connection.permissions '' \
  connection.autoconnect no vpn.persistent yes

# Store any secrets the profile needs, since nobody is around to type them in at boot.
esc() { printf '%s' "${1//,/\\,}"; }  # nmcli splits these lists on commas
if grep -q '^auth-user-pass' "$ovpn"; then
  read -rp "VPN username: " vpn_user
  read -rsp "VPN password: " vpn_pass; echo
  sudo nmcli connection modify "$name" +vpn.data "username=$(esc "$vpn_user")" +vpn.data password-flags=0 \
    +vpn.secrets "password=$(esc "$vpn_pass")"
fi
if grep -q 'ENCRYPTED' "$ovpn"; then
  read -rsp "Passphrase for the VPN key: " key_pass; echo
  sudo nmcli connection modify "$name" +vpn.data cert-pass-flags=0 +vpn.secrets "cert-pass=$(esc "$key_pass")"
fi

echo
echo "Done. Away from home the launcher connects $name within a minute of the Pi getting online."
echo "To try it now: sudo nmcli connection up id $name   (from inside your home network this may fail; that's fine)"

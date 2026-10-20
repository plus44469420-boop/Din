#!/bin/sh
# Configure a wired interface on Linux or macOS for a direct PC <-> PS5 cable.
#   sudo ./setup-direct-link-unix.sh <interface> [pc-ip]
# Examples:
#   sudo ./setup-direct-link-unix.sh enp3s0          # Linux
#   sudo ./setup-direct-link-unix.sh en7             # macOS (USB-C Ethernet adapter)
# Find the interface name with `ip link` (Linux) or `networksetup -listallhardwareports` (macOS).
set -eu

if [ $# -lt 1 ]; then
  sed -n '2,8p' "$0"
  exit 1
fi

IFACE=$1
IP=${2:-192.168.88.1}
PS5_IP=${IP%.*}.2

case "$(uname -s)" in
  Linux)
    if command -v nmcli >/dev/null 2>&1 && nmcli -t -f DEVICE device | grep -qx "$IFACE"; then
      nmcli connection delete ps5cable >/dev/null 2>&1 || true
      nmcli connection add type ethernet ifname "$IFACE" con-name ps5cable \
        ipv4.method manual ipv4.addresses "$IP/24" ipv4.never-default yes ipv6.method ignore >/dev/null
      nmcli connection up ps5cable >/dev/null
    else
      ip link set "$IFACE" up
      ip addr flush dev "$IFACE"
      ip addr add "$IP/24" dev "$IFACE"
    fi
    ;;
  Darwin)
    SERVICE=$(networksetup -listnetworkserviceorder | awk -v dev="$IFACE" '
      /^\([0-9]+\)/ { sub(/^\([0-9]+\) /, ""); name = $0 }
      index($0, "Device: " dev ")") { print name; exit }')
    if [ -z "$SERVICE" ]; then
      echo "No network service uses $IFACE"; exit 1
    fi
    # Empty router keeps Wi-Fi as the default route.
    networksetup -setmanual "$SERVICE" "$IP" 255.255.255.0
    ;;
  *)
    echo "Unsupported OS: $(uname -s)"; exit 1
    ;;
esac

cat <<EOF
PC side done: $IP/24 on $IFACE.

On the PS5: Settings > Network > Settings > Set Up Internet Connection >
  Set Up Manually > Use a LAN Cable > Manual:
    IP address:      $PS5_IP
    Subnet mask:     255.255.255.0
    Default gateway: $IP
    Primary DNS:     $IP

Then test with:  ping $PS5_IP
EOF

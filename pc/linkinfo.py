"""Identify the PC-side adapter that carries the cable link to the PS5 and
work out what limits its speed.

The PS5 end is always its built-in gigabit Ethernet port, so the variables
are on the PC: which adapter (often a USB-C to Ethernet dongle), the Ethernet
speed it negotiated, and the USB generation it is running at. Each platform
reports a different subset; whatever is missing stays None and the speed test
fills the gap.
"""

import json
import os
import re
import socket
import subprocess
import sys

PS5_PORT_MBPS = 1000

# (vendor id, product id) -> (chip, max Ethernet Mbps, max USB Mbps)
KNOWN_USB_CHIPS = {
    ("0bda", "8152"): ("Realtek RTL8152 (Fast Ethernet)", 100, 480),
    ("0bda", "8153"): ("Realtek RTL8153 (Gigabit)", 1000, 5000),
    ("0bda", "8155"): ("Realtek RTL8155 (Gigabit)", 1000, 5000),
    ("0bda", "8156"): ("Realtek RTL8156 (2.5 Gigabit)", 2500, 5000),
    ("0bda", "8157"): ("Realtek RTL8157 (5 Gigabit)", 5000, 10000),
    ("0b95", "1790"): ("ASIX AX88179 (Gigabit)", 1000, 5000),
    ("0b95", "178a"): ("ASIX AX88179A (Gigabit)", 1000, 5000),
    ("0b95", "772b"): ("ASIX AX88772B (Fast Ethernet)", 100, 480),
    ("0b95", "7720"): ("ASIX AX88772 (Fast Ethernet)", 100, 480),
    ("0b95", "772a"): ("ASIX AX88772A (Fast Ethernet)", 100, 480),
    ("05ac", "1402"): ("Apple USB Ethernet Adapter (Fast Ethernet)", 100, 480),
    ("17ef", "720c"): ("Lenovo USB-C Ethernet (RTL8153, Gigabit)", 1000, 5000),
    ("17ef", "7205"): ("Lenovo USB 3.0 Ethernet (RTL8153, Gigabit)", 1000, 5000),
    ("045e", "07c6"): ("Microsoft Surface Ethernet (RTL8153, Gigabit)", 1000, 5000),
    ("0424", "7500"): ("Microchip LAN7500 (Gigabit, USB 2.0)", 1000, 480),
    ("0424", "7800"): ("Microchip LAN7800 (Gigabit)", 1000, 5000),
    ("2357", "0601"): ("TP-Link UE300 (RTL8153, Gigabit)", 1000, 5000),
}

# Adapter names as Windows/macOS show them -> (chip, max Ethernet Mbps,
# max USB Mbps, bus). First match wins.
DESCRIPTION_PATTERNS = [
    (r"usb.*\b5\s?g(b)?e\b|rtl8157", "Realtek 5GbE USB", 5000, 10000, "usb"),
    (r"usb.*2\.5\s?g(b)?e|rtl8156", "Realtek 2.5GbE USB", 2500, 5000, "usb"),
    (r"realtek usb gbe|rtl8153", "Realtek RTL8153 (Gigabit)", 1000, 5000, "usb"),
    (r"realtek usb fe|rtl8152", "Realtek RTL8152 (Fast Ethernet)", 100, 480, "usb"),
    (r"ax88179", "ASIX AX88179 (Gigabit)", 1000, 5000, "usb"),
    (r"ax8877\d", "ASIX AX88772 (Fast Ethernet)", 100, 480, "usb"),
    (r"usb 10/100/1000|usb.*gigabit|usb.*1000|usb.*gbe", "USB Gigabit Ethernet", 1000, None, "usb"),
    (r"usb 10/100\b|usb.*fast ethernet", "USB Fast Ethernet", 100, 480, "usb"),
    (r"thunderbolt", "Thunderbolt Ethernet", None, None, "thunderbolt"),
    (r"wi-?fi|wireless|802\.11|airport", "Wi-Fi", None, None, "wifi"),
    (r"pcie|pci-e|intel\(r\) ethernet|killer|gbe family controller", "Built-in Ethernet", None, None, "pci"),
]

USB_SPEED_LABEL = {
    1: "USB 1.0 Low-Speed (1.5 Mbps)",
    12: "USB 1.1 Full-Speed (12 Mbps)",
    480: "USB 2.0 Hi-Speed (480 Mbps)",
    5000: "USB 3.2 Gen 1 (5 Gbps)",
    10000: "USB 3.2 Gen 2 (10 Gbps)",
    20000: "USB 3.2 Gen 2x2 (20 Gbps)",
    40000: "USB4 / Thunderbolt (40 Gbps)",
}

# Rough real-world payload throughput in MB/s once protocol overhead is paid.
USB_PRACTICAL_MBPS = {1: 0.1, 12: 1.0, 480: 38.0, 5000: 400.0, 10000: 900.0,
                      20000: 1800.0, 40000: 3000.0}


def ethernet_practical(mbps):
    """TCP payload over Ethernet: about 94% of the line rate."""
    return mbps * 0.94 / 8


def human_rate(mb_s):
    return f"{mb_s:.0f} MB/s" if mb_s >= 10 else f"{mb_s:.1f} MB/s"


# --- platform probes ---------------------------------------------------------

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def linux_iface_for_ip(ip, sysroot="/sys"):
    import fcntl
    import struct

    for name in sorted(os.listdir(os.path.join(sysroot, "class/net"))):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(s.fileno(), 0x8915,  # SIOCGIFADDR
                                 struct.pack("256s", name.encode()[:15]))
            if socket.inet_ntoa(packed[20:24]) == ip:
                return name
        except OSError:
            continue
        finally:
            s.close()
    return None


def linux_detect(local_ip, sysroot="/sys", iface=None):
    sysroot = os.environ.get("PS5CABLE_SYSFS_ROOT", sysroot)
    iface = iface or os.environ.get("PS5CABLE_IFACE") or linux_iface_for_ip(local_ip, sysroot)
    info = {"interface": iface}
    if not iface:
        return info
    net = os.path.join(sysroot, "class/net", iface)
    speed = _read(os.path.join(net, "speed"))
    if speed and speed.lstrip("-").isdigit() and int(speed) > 0:
        info["link_mbps"] = int(speed)
    if _read(os.path.join(net, "operstate")) == "down":
        info["link_down"] = True
    if os.path.isdir(os.path.join(net, "wireless")) or os.path.isdir(os.path.join(net, "phy80211")):
        info["bus"] = "wifi"
        return info

    dev = os.path.join(net, "device")
    if not os.path.exists(dev):
        info["bus"] = "virtual"
        info["description"] = "Loopback / virtual interface" if iface.startswith("lo") else "Virtual interface"
        return info
    real = os.path.realpath(dev)
    driver = os.path.realpath(os.path.join(dev, "driver"))
    if os.path.exists(driver):
        info["driver"] = os.path.basename(driver)

    if "/usb" in real:
        info["bus"] = "usb"
        usb = real
        while usb and usb != "/" and not os.path.exists(os.path.join(usb, "idVendor")):
            usb = os.path.dirname(usb)
        if os.path.exists(os.path.join(usb, "idVendor")):
            info["vendor_id"] = _read(os.path.join(usb, "idVendor"))
            info["product_id"] = _read(os.path.join(usb, "idProduct"))
            sp = _read(os.path.join(usb, "speed"))
            if sp:
                try:
                    info["usb_mbps"] = int(float(sp))
                except ValueError:
                    pass
            product = _read(os.path.join(usb, "product"))
            maker = _read(os.path.join(usb, "manufacturer"))
            info["description"] = " ".join(x for x in (maker, product) if x) or None
    elif "/pci" in real:
        info["bus"] = "thunderbolt" if "thunderbolt" in real else "pci"
        info["vendor_id"] = (_read(os.path.join(real, "vendor")) or "").replace("0x", "") or None
        info["product_id"] = (_read(os.path.join(real, "device")) or "").replace("0x", "") or None
    return info


WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$a = Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq '%s' | Select-Object -First 1
if (-not $a) { '{}'; exit }
$n = Get-NetAdapter -InterfaceIndex $a.InterfaceIndex
[ordered]@{
  name = $n.Name; description = $n.InterfaceDescription; pnp = $n.PnPDeviceID
  link_bps = [int64]$n.ReceiveLinkSpeed; speed_bps = [int64]$n.Speed
  media = [string]$n.PhysicalMediaType; status = [string]$n.Status
} | ConvertTo-Json -Compress
"""


def windows_parse(raw):
    data = json.loads(raw or "{}")
    if not data:
        return {}
    info = {"interface": data.get("name"), "description": data.get("description")}
    bps = data.get("link_bps") or data.get("speed_bps") or 0
    if bps > 0:
        info["link_mbps"] = int(round(bps / 1_000_000))
    if str(data.get("status", "")).lower() not in ("", "up"):
        info["link_down"] = True
    pnp = data.get("pnp") or ""
    m = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", pnp, re.I)
    if m:
        info.update(bus="usb", vendor_id=m.group(1).lower(), product_id=m.group(2).lower())
    elif pnp.upper().startswith("PCI\\"):
        info["bus"] = "pci"
    if "802.11" in str(data.get("media", "")):
        info["bus"] = "wifi"
    return info


def windows_detect(local_ip):
    ip = re.sub(r"[^0-9.]", "", local_ip or "")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SCRIPT % ip],
        capture_output=True, text=True, timeout=15, creationflags=flags)
    info = windows_parse(out.stdout.strip())
    if info.get("bus") == "usb":
        info["usb_note"] = ("Windows does not report the USB link speed. Run the "
                            "speed test to see if the adapter is held back.")
    return info


def mac_parse_ifconfig(text, ip):
    iface, block = None, {}
    for line in text.splitlines():
        m = re.match(r"^(\w[\w.]*): flags=", line)
        if m:
            iface = m.group(1)
            block[iface] = []
        elif iface:
            block[iface].append(line.strip())
    for name, lines in block.items():
        if any(l.startswith(f"inet {ip} ") for l in lines):
            info = {"interface": name}
            for l in lines:
                m = re.match(r"media: .*?\((\d+)(G?)base", l)
                if m:
                    info["link_mbps"] = int(m.group(1)) * (1000 if m.group(2) else 1)
                if l.startswith("status:") and "inactive" in l:
                    info["link_down"] = True
            return info
    return {}


def mac_hardware_port(text, iface):
    port = None
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and line.split(":", 1)[1].strip() == iface:
            return port
    return None


MAC_SPEED = {"low_speed": 1, "full_speed": 12, "high_speed": 480,
             "super_speed": 5000, "super_speed_plus": 10000}


def mac_find_usb(tree, name=None):
    """Searches system_profiler JSON for a known Ethernet chip (or a device
    whose name matches the hardware port) and returns (vid, pid, usb_mbps)."""
    stack = list(tree.values()) if isinstance(tree, dict) else list(tree)
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
        vid = str(node.get("vendor_id") or node.get("USBDeviceKeyVendorID") or "")
        pid = str(node.get("product_id") or node.get("USBDeviceKeyProductID") or "")
        m_v, m_p = re.search(r"0x([0-9a-f]{4})", vid, re.I), re.search(r"0x([0-9a-f]{4})", pid, re.I)
        key = (m_v.group(1).lower(), m_p.group(1).lower()) if m_v and m_p else None
        if (key and key in KNOWN_USB_CHIPS) or (name and node.get("_name") == name):
            speed = node.get("device_speed")
            mbps = MAC_SPEED.get(speed)
            link = str(node.get("USBDeviceKeyLinkSpeed") or "")
            m = re.match(r"([\d.]+)\s*(G|M)b", link)
            if m:
                mbps = int(float(m.group(1)) * (1000 if m.group(2) == "G" else 1))
            return key, mbps
    return None, None


def mac_detect(local_ip):
    run = lambda *a: subprocess.run(list(a), capture_output=True, text=True, timeout=20).stdout
    info = mac_parse_ifconfig(run("ifconfig"), local_ip)
    if not info:
        return info
    port = mac_hardware_port(run("networksetup", "-listallhardwareports"), info["interface"])
    if port:
        info["description"] = port
    for dtype in ("SPUSBHostDataType", "SPUSBDataType"):
        try:
            tree = json.loads(run("system_profiler", "-json", dtype) or "{}")
        except ValueError:
            continue
        key, mbps = mac_find_usb(tree, port)
        if key or mbps:
            info["bus"] = "usb"
            if key:
                info["vendor_id"], info["product_id"] = key
            if mbps:
                info["usb_mbps"] = mbps
            break
    return info


# --- assessment ---------------------------------------------------------------

def identify(info):
    key = (info.get("vendor_id"), info.get("product_id"))
    if key in KNOWN_USB_CHIPS:
        chip, eth_max, usb_max = KNOWN_USB_CHIPS[key]
        info.update(chip=chip, adapter_max_mbps=eth_max, adapter_usb_mbps=usb_max, bus="usb")
        return info
    text = " ".join(str(info.get(k) or "") for k in ("description", "driver")).lower()
    for pattern, chip, eth_max, usb_max, bus in DESCRIPTION_PATTERNS:
        if re.search(pattern, text):
            info.setdefault("chip", chip)
            if eth_max:
                info.setdefault("adapter_max_mbps", eth_max)
            if usb_max:
                info.setdefault("adapter_usb_mbps", usb_max)
            if bus and not info.get("bus"):
                info["bus"] = bus
            break
    return info


def assess(info):
    """Adds expected_mbs, bottleneck, level ('ok'|'warn'|'bad'|'unknown') and
    plain-language notes to a probe result."""
    info = identify(dict(info))
    notes, limits = [], []
    bus = info.get("bus")
    link = info.get("link_mbps")
    usb = info.get("usb_mbps")
    adapter_max = info.get("adapter_max_mbps")

    if bus == "usb":
        info["connection"] = "USB to Ethernet adapter"
    elif bus == "thunderbolt":
        info["connection"] = "Thunderbolt Ethernet"
    elif bus == "pci":
        info["connection"] = "Built-in Ethernet port"
    elif bus == "wifi":
        info["connection"] = "Wi-Fi"
    elif bus == "virtual":
        info["connection"] = "Virtual / loopback (no cable)"
    else:
        info["connection"] = "Unknown"

    if usb:
        info["usb_label"] = USB_SPEED_LABEL.get(usb, f"{usb} Mbps")

    limits.append(("PS5 LAN port (1 Gbps)", ethernet_practical(PS5_PORT_MBPS)))
    if link:
        limits.append((f"Ethernet link ({link} Mbps)", ethernet_practical(link)))
    if usb:
        limits.append((f"USB link ({info['usb_label'].split(' (')[0]})", USB_PRACTICAL_MBPS.get(usb, usb / 8 * 0.6)))
    name, best = min(limits, key=lambda x: x[1])
    info["expected_mbs"] = round(best, 1)
    info["bottleneck"] = name
    info["limits"] = [{"name": n, "mbs": round(v, 1)} for n, v in limits]
    level = "ok"

    if bus == "virtual":
        level = "unknown"
        notes.append("This is not a cable link. Nothing to measure here.")
    elif bus == "wifi":
        level = "warn"
        notes.append("Traffic to the PS5 is going over Wi-Fi, not the cable. Check that "
                     "the cable adapter has the 192.168.88.1 address.")
    if info.get("link_down"):
        level = "bad"
        notes.append("The adapter reports no Ethernet link. Check that the cable is "
                     "plugged in at both ends and the PS5 is on.")
    if link and link < 1000:
        level = "bad" if link <= 100 else "warn"
        if adapter_max and adapter_max <= 100:
            notes.append(f"This adapter ({info.get('chip')}) only does {adapter_max} Mbps, "
                         f"about {human_rate(ethernet_practical(adapter_max))}. A gigabit USB 3 "
                         "adapter (RTL8153, AX88179, ...) is about 10x faster.")
        else:
            notes.append(f"The Ethernet link came up at only {link} Mbps. Use a Cat5e or "
                         "Cat6 cable (all 8 wires) and reseat both ends; the PS5 supports 1000 Mbps.")
    if usb and usb <= 480 and (adapter_max or 1000) > 100:
        level = "bad" if level == "bad" else "warn"
        notes.append("The adapter is running at USB 2.0 speed (480 Mbps), which caps it at "
                     "about 38 MB/s. Plug it straight into a USB 3 or USB-C port with data "
                     "(not a charge-only cable or a USB 2 hub).")
    if link and link > 1000:
        notes.append(f"The adapter negotiated {link} Mbps, but the PS5's LAN port tops out "
                     "at 1 Gbps.")
    if info.get("usb_note"):
        notes.append(info["usb_note"])
    if level == "ok" and bus in ("usb", "pci", "thunderbolt") and (link or 0) >= 1000:
        notes.insert(0, f"Full gigabit to the PS5: about {human_rate(best)}, "
                        f"roughly {best * 60 / 1024:.1f} GB per minute.")
    if not link and bus not in ("virtual",) and level == "ok":
        level = "unknown"

    info["level"] = level
    info["notes"] = notes
    info["summary"] = summarize(info)
    return info


def summarize(info):
    parts = [info.get("chip") or info.get("description") or info.get("connection")]
    if info.get("usb_label"):
        parts.append(info["usb_label"].split(" (")[0])
    if info.get("link_mbps"):
        parts.append(f"{info['link_mbps']} Mbps" if info["link_mbps"] < 1000
                     else f"{info['link_mbps'] / 1000:g} Gbps")
    return " · ".join(p for p in parts if p)


def interpret_speedtest(info, up_mbs, down_mbs):
    """Compares a measured speed with what the hardware should manage."""
    measured = min(x for x in (up_mbs, down_mbs) if x)
    expected = info.get("expected_mbs") or ethernet_practical(PS5_PORT_MBPS)
    notes = []
    if measured >= expected * 0.75:
        notes.append(f"Measured {human_rate(measured)}: the link is performing as expected.")
        verdict = "ok"
    else:
        verdict = "warn"
        notes.append(f"Measured {human_rate(measured)}, expected about {human_rate(expected)}.")
        if 25 <= measured <= 45 and (info.get("link_mbps") or 1000) >= 1000 and not info.get("usb_mbps"):
            notes.append("That matches a USB 2.0 connection. The adapter is probably on a "
                         "USB 2.0 port, a USB 2 hub, or a USB-C cable that only carries USB 2 data.")
        elif 8 <= measured <= 12.5:
            notes.append("That matches a 100 Mbps link: a Fast Ethernet adapter or a "
                         "cable/port that could not negotiate gigabit.")
        else:
            notes.append("Possible causes: a hub or dock in between, a long or damaged "
                         "cable, or power saving on the adapter.")
    return {"up_mbs": up_mbs, "down_mbs": down_mbs, "verdict": verdict, "notes": notes}


def detect(local_ip):
    """Best-effort adapter info for the interface that owns `local_ip`."""
    if not local_ip:
        return assess({"bus": None, "link_down": True})
    if local_ip.startswith("127.") and not os.environ.get("PS5CABLE_IFACE"):
        return assess({"interface": "lo", "bus": "virtual",
                       "description": "Loopback / virtual interface"})
    try:
        if sys.platform.startswith("linux"):
            raw = linux_detect(local_ip)
        elif sys.platform == "win32":
            raw = windows_detect(local_ip)
        elif sys.platform == "darwin":
            raw = mac_detect(local_ip)
        else:
            raw = {}
    except Exception as e:  # never let adapter probing break the app
        raw = {"error": str(e)}
    raw["local_ip"] = local_ip
    return assess(raw)

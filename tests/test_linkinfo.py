"""Adapter detection and assessment against fake sysfs trees and captured
Windows/macOS tool output."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

import linkinfo  # noqa: E402


def fake_usb_sysfs(root, iface="enx001", vid="0bda", pid="8153", usb_speed="5000",
                   link="1000", product="USB 10/100/1000 LAN", maker="Realtek"):
    """Builds /sys/class/net/<iface> -> .../usb2/2-1/2-1:1.0 like the kernel does."""
    usbdev = root / "devices/pci0000:00/0000:00:14.0/usb2/2-1"
    intf = usbdev / "2-1:1.0"
    intf.mkdir(parents=True)
    for name, val in (("idVendor", vid), ("idProduct", pid), ("speed", usb_speed),
                      ("product", product), ("manufacturer", maker)):
        (usbdev / name).write_text(val + "\n")
    drv = root / "bus/usb/drivers/r8152"
    drv.mkdir(parents=True)
    os.symlink(drv, intf / "driver")
    net = root / "class/net" / iface
    net.mkdir(parents=True)
    (net / "speed").write_text(link + "\n")
    (net / "operstate").write_text("up\n")
    os.symlink(intf, net / "device")
    return root


class LinuxTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def detect(self, **kw):
        fake_usb_sysfs(self.root, **kw)
        raw = linkinfo.linux_detect("192.168.88.1", sysroot=str(self.root), iface="enx001")
        return linkinfo.assess(raw)

    def test_gigabit_usb3_adapter_is_full_speed(self):
        info = self.detect()
        self.assertEqual(info["chip"], "Realtek RTL8153 (Gigabit)")
        self.assertEqual(info["usb_label"], "USB 3.2 Gen 1 (5 Gbps)")
        self.assertEqual(info["link_mbps"], 1000)
        self.assertEqual(info["driver"], "r8152")
        self.assertEqual(info["level"], "ok")
        self.assertAlmostEqual(info["expected_mbs"], 117.5, places=1)
        self.assertIn("Full gigabit", info["notes"][0])
        self.assertEqual(info["summary"], "Realtek RTL8153 (Gigabit) · USB 3.2 Gen 1 · 1 Gbps")

    def test_usb2_port_is_the_bottleneck(self):
        info = self.detect(usb_speed="480")
        self.assertEqual(info["level"], "warn")
        self.assertEqual(info["expected_mbs"], 38.0)
        self.assertIn("USB link", info["bottleneck"])
        self.assertTrue(any("USB 2.0 speed" in n for n in info["notes"]))

    def test_fast_ethernet_adapter(self):
        info = self.detect(pid="8152", usb_speed="480", link="100")
        self.assertEqual(info["level"], "bad")
        self.assertAlmostEqual(info["expected_mbs"], 11.8, places=1)
        self.assertTrue(any("only does 100 Mbps" in n for n in info["notes"]))
        self.assertFalse(any("USB 2.0 speed" in n for n in info["notes"]))

    def test_gigabit_adapter_on_bad_cable(self):
        info = self.detect(link="100")
        self.assertEqual(info["level"], "bad")
        self.assertTrue(any("Cat5e" in n for n in info["notes"]))

    def test_2_5g_adapter_is_capped_by_ps5(self):
        info = self.detect(pid="8156", link="2500")
        self.assertEqual(info["bottleneck"], "PS5 LAN port (1 Gbps)")
        self.assertTrue(any("tops out at 1 Gbps" in n for n in info["notes"]))

    def test_unknown_usb_chip_uses_description(self):
        info = self.detect(vid="1234", pid="5678", product="USB 10/100/1000 LAN", maker="Generic")
        self.assertEqual(info["chip"], "USB Gigabit Ethernet")
        self.assertEqual(info["connection"], "USB to Ethernet adapter")


class WindowsTest(unittest.TestCase):
    def test_parse_usb_adapter(self):
        raw = json.dumps({"name": "Ethernet 3", "description": "Realtek USB GbE Family Controller",
                          "pnp": "USB\\VID_0BDA&PID_8153\\000001000000", "link_bps": 1000000000,
                          "speed_bps": 1000000000, "media": "802.3", "status": "Up"})
        info = linkinfo.windows_parse(raw)
        self.assertEqual((info["vendor_id"], info["product_id"], info["bus"]), ("0bda", "8153", "usb"))
        out = linkinfo.assess(info)
        self.assertEqual(out["chip"], "Realtek RTL8153 (Gigabit)")
        self.assertEqual(out["link_mbps"], 1000)
        self.assertNotIn("usb_label", out)

    def test_parse_builtin_and_wifi(self):
        info = linkinfo.assess(linkinfo.windows_parse(json.dumps({
            "name": "Ethernet", "description": "Intel(R) Ethernet Connection I219-V",
            "pnp": "PCI\\VEN_8086&DEV_15BC", "link_bps": 1000000000, "status": "Up"})))
        self.assertEqual(info["connection"], "Built-in Ethernet port")
        self.assertEqual(info["level"], "ok")
        wifi = linkinfo.assess(linkinfo.windows_parse(json.dumps({
            "name": "Wi-Fi", "description": "Intel(R) Wi-Fi 6 AX201", "pnp": "PCI\\VEN_8086",
            "link_bps": 866000000, "media": "Native 802.11", "status": "Up"})))
        self.assertEqual(wifi["connection"], "Wi-Fi")
        self.assertEqual(wifi["level"], "warn")

    def test_empty(self):
        self.assertEqual(linkinfo.windows_parse("{}"), {})


IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
\tmedia: autoselect
\tstatus: active
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.88.1 netmask 0xffffff00 broadcast 192.168.88.255
\tmedia: autoselect (1000baseT <full-duplex>)
\tstatus: active
"""
PORTS = """Hardware Port: Wi-Fi
Device: en0
Ethernet Address: aa

Hardware Port: USB 10/100/1000 LAN
Device: en7
Ethernet Address: bb
"""


class MacTest(unittest.TestCase):
    def test_ifconfig_and_ports(self):
        info = linkinfo.mac_parse_ifconfig(IFCONFIG, "192.168.88.1")
        self.assertEqual(info, {"interface": "en7", "link_mbps": 1000})
        self.assertEqual(linkinfo.mac_hardware_port(PORTS, "en7"), "USB 10/100/1000 LAN")

    def test_system_profiler_usb_speed(self):
        tree = {"SPUSBDataType": [{"_name": "USB31Bus", "_items": [
            {"_name": "Hub", "_items": [
                {"_name": "USB 10/100/1000 LAN", "vendor_id": "0x0bda  (Realtek)",
                 "product_id": "0x8153", "device_speed": "high_speed"}]}]}]}
        self.assertEqual(linkinfo.mac_find_usb(tree, "USB 10/100/1000 LAN"), (("0bda", "8153"), 480))
        host_tree = {"SPUSBHostDataType": [{"_items": [
            {"_name": "AX88179A", "USBDeviceKeyVendorID": "0x0b95", "USBDeviceKeyProductID": "0x178a",
             "USBDeviceKeyLinkSpeed": "5 Gb/s"}]}]}
        self.assertEqual(linkinfo.mac_find_usb(host_tree), (("0b95", "178a"), 5000))


class SpeedTestVerdictTest(unittest.TestCase):
    def test_full_speed(self):
        info = linkinfo.assess({"bus": "usb", "vendor_id": "0bda", "product_id": "8153",
                                "link_mbps": 1000})
        v = linkinfo.interpret_speedtest(info, 112.0, 110.5)
        self.assertEqual(v["verdict"], "ok")

    def test_usb2_signature_on_windows(self):
        info = linkinfo.assess({"bus": "usb", "vendor_id": "0bda", "product_id": "8153",
                                "link_mbps": 1000})
        v = linkinfo.interpret_speedtest(info, 36.0, 39.0)
        self.assertEqual(v["verdict"], "warn")
        self.assertTrue(any("USB 2.0 connection" in n for n in v["notes"]))

    def test_100mbit_signature(self):
        info = linkinfo.assess({"bus": "pci", "link_mbps": 1000})
        v = linkinfo.interpret_speedtest(info, 11.2, 11.4)
        self.assertTrue(any("100 Mbps link" in n for n in v["notes"]))


if __name__ == "__main__":
    unittest.main()

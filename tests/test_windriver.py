"""The built-in WinUSB driver installer. Installing needs Windows and admin
rights, which CI checks on its Windows runner; here only the INF and the
non-Windows refusals are tested."""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

import windriver  # noqa: E402


def cable(hwid, service, problem=0, name="USB cable"):
    return {"name": name, "hwids": [hwid], "service": service, "problem": problem}


class CableTest(unittest.TestCase):
    def test_driver_on_the_whole_device_is_reported(self):
        msg = windriver.bridge_problem([cable("USB\\VID_067B&PID_27A1", "WINUSB")])
        self.assertIn("whole device", msg)
        self.assertIn("Install driver", msg)

    def test_a_stopped_driver_names_the_problem(self):
        msg = windriver.bridge_problem([cable("USB\\VID_067B&PID_27A1&MI_00", "WINUSB", 10)])
        self.assertIn("problem 10", msg)

    def test_a_healthy_cable_needs_nothing(self):
        devices = [cable("USB\\VID_067B&PID_27A1", "usbccgp"),
                   cable("USB\\VID_067B&PID_27A1&MI_00", "WINUSB")]
        self.assertIsNone(windriver.bridge_problem(devices))
        self.assertIsNone(windriver.bridge_problem([cable("USB\\VID_1234&PID_5678", "usbccgp")]))

    def test_the_inf_never_matches_the_whole_device(self):
        ids = windriver.inf_hwids([cable("USB\\VID_067B&PID_27A1", "WINUSB", 43)])
        self.assertEqual(ids, ["USB\\VID_067B&PID_27A1&MI_00"])
        self.assertTrue(all(i.endswith("&MI_00") for i in windriver.default_hwids()))

    def test_rescan_shows_a_cable_libusb_cannot_see(self):
        rows = windriver.supplement(
            [], [cable("USB\\VID_067B&PID_27A1", "WINUSB", 43, "Prolific")])
        self.assertEqual(rows[0]["id"], "067b:27a1")
        self.assertIn("stopped", rows[0]["kind"])
        self.assertEqual(windriver.supplement([{"id": "067b:27a1", "bridge": True}],
                                              [cable("USB\\VID_067B&PID_27A1", "WINUSB")]),
                         [{"id": "067b:27a1", "bridge": True}])


class InfTest(unittest.TestCase):
    def test_points_the_bridge_interface_at_winusb(self):
        inf = windriver.make_inf("10/21/2026")
        self.assertTrue(inf.isascii())
        self.assertIn("\r\n", inf)
        for hwid in ("USB\\VID_067B&PID_27A1&MI_00", "USB\\VID_067B&PID_25A1&MI_00",
                     "USB\\VID_067B&PID_27A7&MI_00"):
            self.assertIn(f"Install, {hwid}\r\n", inf)
        # The whole device must keep Windows' composite driver. Matching it here
        # is what makes Windows stop a USB-C cable.
        self.assertNotIn("Install, USB\\VID_067B&PID_27A1\r\n", inf)
        self.assertIn("CatalogFile = ps5cable.cat", inf)
        self.assertIn("DriverVer   = 10/21/2026,1.0.0.0", inf)
        self.assertIn("Include = winusb.inf", inf)
        self.assertIn("Needs   = WINUSB.NT.Services", inf)
        self.assertIn(windriver.INTERFACE_GUID, inf)
        self.assertIn('PL27A1 = "PS5 Cable: Prolific PL27A1 USB bridge"', inf)
        for arch in ("NTamd64", "NTx86", "NTarm64"):
            self.assertIn(f"[Cable.{arch}]", inf)

    @unittest.skipIf(os.name == "nt", "checks the refusal elsewhere")
    def test_only_windows_installs(self):
        self.assertFalse(windriver.supported())
        for fn in (windriver.install, windriver.install_elevated):
            with self.assertRaises(windriver.DriverError):
                fn()


if __name__ == "__main__":
    unittest.main()

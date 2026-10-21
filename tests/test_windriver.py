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


class InfTest(unittest.TestCase):
    def test_points_both_bridge_chips_at_winusb(self):
        inf = windriver.make_inf("10/21/2026")
        self.assertTrue(inf.isascii())
        self.assertIn("\r\n", inf)
        for hwid in ("USB\\VID_067B&PID_27A1", "USB\\VID_067B&PID_25A1",
                     "USB\\VID_067B&PID_27A1&MI_00"):
            self.assertIn(f"Install, {hwid}\r\n", inf)
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

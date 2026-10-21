"""The USB link (frames, tunnel states, reconnects, link assessment), plus
game-folder presets and the cross-drive rename guard."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
sys.path.insert(0, str(ROOT / "tests"))

import hub  # noqa: E402
import ps5cable  # noqa: E402
import usblink  # noqa: E402
from fakecable import HOST_SERVER, FakePS5, build_payload, free_port  # noqa: E402


def setUpModule():
    build_payload()


def wait_port(port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(port)


class FrameTest(unittest.TestCase):
    def test_roundtrip(self):
        f = usblink.encode(usblink.DATA, 7, b"hello")
        self.assertEqual(f[:2], b"PU")
        self.assertEqual(usblink.decode(f), (usblink.DATA, 7, b"hello"))

    def test_never_a_multiple_of_512(self):
        for n in (0, 500, 512 - 12, 1024 - 12, usblink.MAX_DATA):
            f = usblink.encode(usblink.DATA, 1, b"x" * n)
            self.assertNotEqual(len(f) % 512, 0, n)
            self.assertEqual(usblink.decode(f)[2], b"x" * n)

    def test_rejects_garbage(self):
        self.assertIsNone(usblink.decode(b"PU"))
        self.assertIsNone(usblink.decode(b"XX" + bytes(10)))
        too_long = usblink.FRAME.pack(b"PU", usblink.DATA, 0, 1, 0, 100) + b"short"
        self.assertIsNone(usblink.decode(too_long))


class AssessTest(unittest.TestCase):
    def test_usb3_bridge_on_usb3_port(self):
        info = usblink.assess({"chip": "Prolific PL27A1", "chip_mbps": 5000, "usb_mbps": 5000})
        self.assertEqual(info["level"], "ok")
        self.assertEqual(info["expected_mbs"], 300.0)
        self.assertEqual(info["usb_label"], "USB 3.2 Gen 1 (5 Gbps)")
        self.assertIn("SuperSpeed", " ".join(info["notes"]))

    def test_usb3_bridge_on_usb2_port(self):
        info = usblink.assess({"chip": "Prolific PL27A1", "chip_mbps": 5000, "usb_mbps": 480})
        self.assertEqual(info["level"], "warn")
        self.assertEqual(info["expected_mbs"], 35.0)
        self.assertTrue(info["bottleneck"].startswith("PC USB port"))

    def test_usb2_bridge(self):
        info = usblink.assess({"chip": "Prolific PL25A1", "chip_mbps": 480, "usb_mbps": 5000})
        self.assertEqual(info["level"], "warn")
        self.assertIn("PL27A1", " ".join(info["notes"]))

    def test_speedtest_verdict(self):
        info = usblink.assess({"chip": "Prolific PL27A1", "chip_mbps": 5000, "usb_mbps": 5000})
        self.assertEqual(usblink.interpret_speedtest(info, 280, 290)["verdict"], "ok")
        slow = usblink.interpret_speedtest(info, 36, 38)
        self.assertEqual(slow["verdict"], "warn")
        self.assertIn("USB 2.0", " ".join(slow["notes"]))


class StateTest(unittest.TestCase):
    class Raising:
        def __init__(self, exc):
            self.exc = exc

        def open(self):
            raise self.exc

    def state_for(self, exc):
        link = usblink.Tunnel(self.Raising(exc)).start()
        try:
            self.assertTrue(link.wait(3, states=("no_cable", "no_access", "no_backend")))
            return link.status()
        finally:
            link.stop()

    def test_states(self):
        st = self.state_for(usblink.NoDevice("none"))
        self.assertEqual((st["state"], st["connected"], st["link"]), ("no_cable", False, None))
        st = self.state_for(usblink.NoAccess("Zadig please"))
        self.assertEqual((st["state"], st["detail"]), ("no_access", "Zadig please"))
        self.assertEqual(self.state_for(usblink.NoBackend("pip"))["state"], "no_backend")

    def test_refuses_local_clients_until_connected(self):
        link = usblink.Tunnel(self.Raising(usblink.NoDevice("none"))).start()
        try:
            with self.assertRaises((ConnectionError, OSError)):
                with ps5cable.Connection("127.0.0.1", link.port, timeout=2) as c:
                    c.ping()
        finally:
            link.stop()

    def test_waiting_until_payload_answers(self):
        cable = free_port()
        backend = usblink.FakeCableBackend(f"127.0.0.1:{cable}")
        link = usblink.Tunnel(backend).start()
        silent = socket.create_connection(("127.0.0.1", cable))
        try:
            self.assertTrue(link.wait(5, states=("waiting",)))
            self.assertIn("ps5cable.elf", link.detail)
            hello = silent.recv(4 + 12 + 8)
            self.assertEqual(hello[4:6], b"PU")
            self.assertEqual(hello[6], usblink.HELLO)
        finally:
            silent.close()
            link.stop()


class TunnelTest(unittest.TestCase):
    def setUp(self):
        self.ps5 = FakePS5()

    def tearDown(self):
        self.ps5.close()

    def conn(self):
        return ps5cable.Connection("127.0.0.1", self.ps5.port, timeout=5)

    def test_banner_and_parallel_channels(self):
        self.assertIn("ps5cable", self.ps5.tunnel.banner)
        errors = []

        def worker():
            try:
                with self.conn() as c:
                    for _ in range(20):
                        c.ping()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        deadline = time.monotonic() + 5
        while self.ps5.tunnel.chans and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.ps5.tunnel.chans, {})

    def test_unplug_closes_channels_and_replug_reconnects(self):
        c = self.conn()
        c.ping()
        self.ps5.kill_payload()
        self.assertTrue(self.ps5.tunnel.wait(5, states=("no_cable",)))
        with self.assertRaises((ConnectionError, OSError)):
            c.ping()
        c.close()
        self.ps5.restart_payload()
        self.assertTrue(self.ps5.tunnel.wait(10))
        with self.conn() as c:
            self.assertEqual(c.ping()[0], ps5cable.PROTO_VER)

    def test_channel_unknown_to_payload_is_closed(self):
        # What a PC tunnel sees when the payload restarts under it: the
        # payload answers frames for channels it does not know with CLOSE.
        mine, theirs = socket.socketpair()
        tunnel = self.ps5.tunnel
        with tunnel.clock:
            tunnel.chans[4242] = usblink._Chan(4242, mine)
        threading.Thread(target=tunnel._pump, args=(tunnel.chans[4242],), daemon=True).start()
        tunnel._send(usblink.DATA, 4242, b"stray")
        theirs.settimeout(5)
        self.assertEqual(theirs.recv(10), b"")
        theirs.close()

    def test_silent_link_drops_to_waiting(self):
        old = usblink.LINK_TIMEOUT
        usblink.LINK_TIMEOUT = 1.0
        try:
            os.kill(self.ps5.proc.pid, signal.SIGSTOP)  # cable up, payload frozen
            self.assertTrue(self.ps5.tunnel.wait(5, states=("waiting",)))
            os.kill(self.ps5.proc.pid, signal.SIGCONT)
            self.assertTrue(self.ps5.tunnel.wait(10))
        finally:
            usblink.LINK_TIMEOUT = old


class FakeConn:
    def __init__(self, dirs, files):
        self.dirs, self.files = set(dirs), files

    def stat(self, p):
        if p in self.dirs:
            return (ps5cable.KIND_DIR, 0, 0)
        if p in self.files:
            return (ps5cable.KIND_FILE, len(self.files[p]), 0)
        return None

    def read_small(self, p, limit):
        if p not in self.files:
            raise ps5cable.RemoteError("missing")
        return self.files[p]


class GameFolderTest(unittest.TestCase):
    def test_parse_scan_paths(self):
        ini = "# scanpath=/data/ignored\ndebug=1\nscanpath = /mnt/usb0/games/\nscanpath=/data/homebrew\n"
        self.assertEqual(hub.parse_scan_paths(ini), ["/mnt/usb0/games", "/data/homebrew"])

    def test_presets_follow_drives_present(self):
        conn = FakeConn({"/data", "/data/homebrew", "/mnt/usb0", "/mnt/ext1"},
                        {hub.SMP_CONFIG: b"scanpath=/mnt/usb0/games\n"})
        out = hub.build_remote_places(conn)
        names = [p["name"] for p in out["places"]]
        self.assertEqual(names[:2], ["Internal", "Homebrew"])
        self.assertIn("USB 1", names)
        self.assertIn("M.2 / Extended 2", names)
        self.assertNotIn("USB 2", names)

        folders = {f["path"]: f for f in out["game_folders"]}
        self.assertTrue(folders["/data/homebrew"]["exists"])
        self.assertTrue(folders["/data/homebrew"]["recommended"])
        self.assertFalse(folders["/mnt/usb0/homebrew"]["exists"])
        for p in ("/data/etaHEN/games", "/data/exfat", "/mnt/ext1/homebrew",
                  "/mnt/usb0/games", hub.PKG_LIBRARY):
            self.assertIn(p, folders)
        self.assertEqual(out["scan_paths"], ["/mnt/usb0/games"])


@unittest.skipUnless(os.path.isdir("/dev/shm") and
                     os.stat("/dev/shm").st_dev != os.stat(tempfile.gettempdir()).st_dev,
                     "needs two filesystems")
class CrossDriveRenameTest(unittest.TestCase):
    def test_rename_between_drives_is_refused(self):
        port = free_port()
        proc = subprocess.Popen([str(HOST_SERVER), "-p", str(port)], stdout=subprocess.DEVNULL)
        src = Path(tempfile.mkdtemp()) / "game.exfat"
        src.write_bytes(b"x")
        dst = f"/dev/shm/ps5cable-test-{os.getpid()}.exfat"
        try:
            wait_port(port)
            with ps5cable.Connection("127.0.0.1", port) as c:
                with self.assertRaisesRegex(ps5cable.RemoteError, "different drives"):
                    c.rename(str(src), dst)
                self.assertTrue(src.exists())
                self.assertFalse(os.path.exists(dst))
                c.rename(str(src), str(src.with_name("moved.exfat")))
                self.assertTrue(src.with_name("moved.exfat").exists())
        finally:
            proc.kill()
            proc.wait()
            shutil.rmtree(src.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

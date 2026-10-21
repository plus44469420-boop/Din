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


class FakeUsbError(Exception):
    def __init__(self, msg, errno=None, backend_error_code=None):
        super().__init__(msg)
        self.errno, self.backend_error_code = errno, backend_error_code


class FakeTimeout(FakeUsbError):
    pass


class FakeEndpoint:
    def __init__(self, addr):
        self.bEndpointAddress, self.bmAttributes = addr, 2  # bulk


class FakeDevice:
    idVendor, idProduct, speed = 0x067B, 0x27A1, 4

    def __init__(self, kernel_driver=True):
        self.calls = []
        self.kernel_driver = kernel_driver
        self.intf = [FakeEndpoint(0x81), FakeEndpoint(0x02)]
        self.inbox = []

    def is_kernel_driver_active(self, i):
        return self.kernel_driver

    def detach_kernel_driver(self, i):
        self.calls.append(("detach", i))

    def get_active_configuration(self):
        return {(0, 0): self.intf}

    def ctrl_transfer(self, *args, **kw):
        self.calls.append(("ctrl",) + args)

    def read(self, ep, buf, timeout):
        if not self.inbox:
            raise FakeTimeout("timeout", backend_error_code=-7)
        item = self.inbox.pop(0)
        if isinstance(item, Exception):
            raise item
        buf[:len(item)] = __import__("array").array("B", item)
        return len(item)

    def write(self, ep, data, timeout):
        self.calls.append(("write", ep, bytes(data)))
        return len(data)


class FakeCore:
    USBError = FakeUsbError
    USBTimeoutError = FakeTimeout

    class NoBackendError(Exception):
        pass

    def __init__(self, devs):
        self.devs = devs

    def find(self, find_all, backend, custom_match):
        return [d for d in self.devs if custom_match(d)]


class FakeUtil:
    ENDPOINT_IN, ENDPOINT_OUT, ENDPOINT_TYPE_BULK = 0x80, 0x00, 2

    def __init__(self, claim_error=None):
        self.claim_error = claim_error
        self.claimed = []

    @staticmethod
    def endpoint_direction(addr):
        return addr & 0x80

    @staticmethod
    def endpoint_type(attrs):
        return attrs & 3

    @staticmethod
    def find_descriptor(intf, custom_match):
        return next((e for e in intf if custom_match(e)), None)

    def claim_interface(self, dev, i):
        if self.claim_error:
            raise self.claim_error
        self.claimed.append(i)

    def release_interface(self, dev, i):
        pass

    def dispose_resources(self, dev):
        pass


class PyUsbBackendTest(unittest.TestCase):
    def backend(self, devs, util=None):
        b = usblink.PyUsbBackend()
        b._usb = (FakeCore(devs), util or FakeUtil(), None)
        return b

    def test_no_cable(self):
        with self.assertRaises(usblink.NoDevice):
            self.backend([]).open()

    def test_opens_bridge_and_resets_it(self):
        dev = FakeDevice()
        tr = self.backend([dev]).open()
        self.assertEqual((tr.ep_in, tr.ep_out), (0x81, 0x02))
        self.assertIn(("detach", 0), dev.calls)
        self.assertIn(("ctrl", 0xC0, usblink.PL_SET_FEATURE, usblink.PL_RESET, 0, 0), dev.calls)
        self.assertEqual(tr.info["chip"], "Prolific PL27A1")
        self.assertEqual(tr.info["usb_mbps"], 5000)

        self.assertIsNone(tr.recv(10))
        frame = usblink.encode(usblink.HELLO, 0, b"12345678banner")
        dev.inbox.append(frame)
        self.assertEqual(usblink.decode(tr.recv(10)), (usblink.HELLO, 0, b"12345678banner"))
        dev.inbox.append(FakeUsbError("No such device", errno=19))
        with self.assertRaises(ConnectionError):
            tr.recv(10)
        tr.send(frame, 10)
        self.assertEqual(dev.calls[-1], ("write", 0x02, frame))

    def test_driver_problem_is_no_access(self):
        util = FakeUtil(claim_error=FakeUsbError("Operation not supported", backend_error_code=-12))
        with self.assertRaises(usblink.NoAccess) as cm:
            self.backend([FakeDevice()], util).open()
        self.assertIn("PL27A1", str(cm.exception))


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

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
import types
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(info["expected_mbs"], 250.0)
        self.assertEqual(info["usb_label"], "USB 3.2 Gen 1 (5 Gbps)")
        self.assertIn("SuperSpeed", " ".join(info["notes"]))

    def test_usb3_bridge_on_usb2_port(self):
        info = usblink.assess({"chip": "Prolific PL27A1", "chip_mbps": 5000, "usb_mbps": 480})
        self.assertEqual(info["level"], "warn")
        self.assertEqual(info["expected_mbs"], 35.0)
        self.assertTrue(info["bottleneck"].startswith("PC USB port"))
        # A USB-A end in a black (USB 2) port is the usual cause on a PC.
        self.assertIn("black or white USB-A ports are USB 2.0", " ".join(info["notes"]))

    def test_every_cable_type_is_covered(self):
        info = usblink.assess({"chip": "Prolific PL27A1", "chip_mbps": 5000, "usb_mbps": 5000})
        ends = {t["ends"]: t for t in info["cable_types"]}
        self.assertEqual(set(ends), {"USB-C ⇄ USB-C", "USB-A ⇄ USB-C", "USB-A ⇄ USB-A"})
        self.assertIn("rear USB-A", ends["USB-A ⇄ USB-C"]["ps5"])
        self.assertIn("USB-C", ends["USB-A ⇄ USB-C"]["pc"])

    def test_usb2_bridge(self):
        info = usblink.assess({"chip": "Prolific PL25A1", "chip_mbps": 480, "usb_mbps": 5000})
        self.assertEqual(info["level"], "warn")
        self.assertIn("PL27A1", " ".join(info["notes"]))

    def test_speedtest_verdict(self):
        info = usblink.assess({"chip": "Prolific PL27A1", "chip_mbps": 5000, "usb_mbps": 5000})
        self.assertEqual(usblink.interpret_speedtest(info, 200, 240)["verdict"], "ok")
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

    def find(self, find_all, backend, custom_match=None):
        return [d for d in self.devs if custom_match is None or custom_match(d)]


class ScanDevice:
    def __init__(self, vid, pid, name=None, dev_class=0, intf_classes=(), speed=3):
        self.idVendor, self.idProduct, self.speed = vid, pid, speed
        self.bDeviceClass = dev_class
        self.iProduct = 2 if name is not None else 0
        self.name = name
        self.configs = [[types.SimpleNamespace(bInterfaceClass=c) for c in intf_classes]]

    def __iter__(self):
        return iter(self.configs)


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
        err = getattr(dev, "claim_error", None) or self.claim_error
        if err:
            raise err
        self.claimed.append(i)

    def release_interface(self, dev, i):
        pass

    def dispose_resources(self, dev):
        pass

    @staticmethod
    def get_string(dev, index):
        if dev.name is Ellipsis:
            raise FakeUsbError("Access denied")
        return dev.name


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

    def test_skips_a_part_of_the_cable_it_cannot_open(self):
        bad = FakeDevice()
        bad.claim_error = NotImplementedError("the composite parent has no WinUSB driver")
        good = FakeDevice()
        tr = self.backend([bad, good]).open()
        self.assertIs(tr.dev, good)

    def test_windows_cable_without_winusb_needs_the_driver(self):
        util = FakeUtil(claim_error=NotImplementedError("Operation not supported"))
        with mock.patch.object(usblink.os, "name", "nt"):
            with self.assertRaises(usblink.NoDriver) as cm:
                self.backend([FakeDevice()], util).open()
        self.assertIn("Install driver", str(cm.exception))

    def test_scan_lists_devices_bridge_first(self):
        devs = [ScanDevice(0x046D, 0xC52B, "USB Receiver", intf_classes=(3, 3)),
                ScanDevice(0x1D6B, 0x0003, "Root hub", dev_class=9),
                ScanDevice(0x0781, 0x5581, ..., intf_classes=(8,), speed=4),
                ScanDevice(0x067B, 0x27A1, "should not be read", speed=4)]
        rows = self.backend(devs).scan()
        self.assertEqual([r["id"] for r in rows], ["067b:27a1", "046d:c52b", "0781:5581"])
        bridge, receiver, drive = rows
        self.assertEqual((bridge["name"], bridge["bridge"], bridge["speed"]),
                         ("Prolific PL27A1", True, "USB 3.2 Gen 1"))
        self.assertEqual((receiver["name"], receiver["kind"]),
                         ("USB Receiver", "Keyboard, mouse or controller"))
        self.assertEqual((drive["name"], drive["bridge"]), ("USB drive", False))


class StateTest(unittest.TestCase):
    class Raising:
        def __init__(self, exc):
            self.exc = exc

        def open(self):
            raise self.exc

    def state_for(self, exc):
        link = usblink.Tunnel(self.Raising(exc)).start()
        try:
            self.assertTrue(link.wait(3, states=("no_cable", "no_access", "no_driver",
                                                 "no_backend")))
            return link.status()
        finally:
            link.stop()

    def test_states(self):
        st = self.state_for(usblink.NoDevice("none"))
        self.assertEqual((st["state"], st["connected"], st["link"]), ("no_cable", False, None))
        st = self.state_for(usblink.NoAccess("Zadig please"))
        self.assertEqual((st["state"], st["detail"]), ("no_access", "Zadig please"))
        self.assertEqual(self.state_for(usblink.NoBackend("pip"))["state"], "no_backend")
        self.assertEqual(self.state_for(usblink.NoDriver("driver"))["state"], "no_driver")

    def test_rescan_tries_again_without_waiting(self):
        tries = []

        class Counting:
            def open(self):
                tries.append(time.monotonic())
                raise usblink.NoAccess("busy")  # retried every 2 s on its own

        link = usblink.Tunnel(Counting()).start()
        try:
            self.assertTrue(link.wait(3, states=("no_access",)))
            before = len(tries)
            started = time.monotonic()
            link.rescan(wait=1.5)
            self.assertGreater(len(tries), before)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(link.scan_devices(), {"devices": [], "error": None})
        finally:
            link.stop()

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
        self.assertIn("PS5 Cable", self.ps5.tunnel.banner)
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


class TransferNoticeTest(unittest.TestCase):
    def test_payload_announces_a_transfer_on_its_console(self):
        build_payload()
        cable = free_port()
        proc = subprocess.Popen(
            [str(HOST_SERVER), "-p", str(free_port()), "-C", f"127.0.0.1:{cable}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        link = usblink.Tunnel(usblink.FakeCableBackend(f"127.0.0.1:{cable}")).start()
        try:
            self.assertTrue(link.wait(10), link.detail)
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "hello.bin"
                src.write_bytes(b"hello")
                with ps5cable.Connection("127.0.0.1", link.port, timeout=5) as conn:
                    conn.put_file(str(src), "/tmp/ps5cable-notice-hello.bin")
            os.remove("/tmp/ps5cable-notice-hello.bin")
        finally:
            link.stop()
            proc.kill()
            out = proc.stdout.read()
            proc.stdout.close()
            proc.wait(timeout=5)
        self.assertIn("PC to PS5, ps5cable-notice-hello.bin", out)


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

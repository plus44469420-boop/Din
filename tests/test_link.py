"""Connection-state detection, auto-start of the payload, game-folder presets
and the cross-drive rename guard."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
sys.path.insert(0, str(ROOT / "tests"))

import hub  # noqa: E402
import ps5cable  # noqa: E402
from test_e2e import free_port  # noqa: E402

HOST_SERVER = ROOT / "payload" / "ps5cable-host"


def setUpModule():
    subprocess.run(["make", "-s", "host"], cwd=ROOT / "payload", check=True)


def wait_port(port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(port)


class FakeLoader:
    """Stands in for the PS5's ELF loader: accepts an ELF on a TCP port and
    "runs" it by starting the desktop build of the payload server."""

    def __init__(self, payload_port):
        self.payload_port = payload_port
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            data = bytearray()
            with conn:
                conn.settimeout(2)
                try:
                    while chunk := conn.recv(65536):
                        data.extend(chunk)
                except OSError:
                    pass
            if data:
                self.received.append(bytes(data))
                if self.proc is None:
                    self.proc = subprocess.Popen(
                        [str(HOST_SERVER), "-p", str(self.payload_port)],
                        stdout=subprocess.DEVNULL)

    def close(self):
        self.srv.close()
        if self.proc:
            self.proc.kill()
            self.proc.wait()


class ProbeTest(unittest.TestCase):
    def test_offline_when_nothing_answers(self):
        st = ps5cable.probe("192.0.2.1", timeout=0.3)
        self.assertEqual(st["state"], "offline")
        self.assertFalse(st["same_subnet"])

    def test_reachable_when_host_up_but_nothing_listening(self):
        st = ps5cable.probe("127.0.0.1", free_port(), free_port(), timeout=0.5)
        self.assertEqual(st["state"], "reachable")

    def test_loader_when_only_elf_loader_listens(self):
        with socket.socket() as loader:
            loader.bind(("127.0.0.1", 0))
            loader.listen(1)
            st = ps5cable.probe("127.0.0.1", free_port(), loader.getsockname()[1], timeout=0.5)
        self.assertEqual(st["state"], "loader")

    def test_connected_reports_banner(self):
        port = free_port()
        proc = subprocess.Popen([str(HOST_SERVER), "-p", str(port)], stdout=subprocess.DEVNULL)
        try:
            wait_port(port)
            st = ps5cable.probe("127.0.0.1", port, timeout=0.5)
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(st["state"], "connected")
        self.assertIn("ps5cable", st["banner"])
        self.assertEqual(st["local_ip"], "127.0.0.1")
        self.assertTrue(st["same_subnet"])


@unittest.skipUnless((ROOT / "payload" / "ps5cable.elf").is_file(),
                     "needs payload/ps5cable.elf (built with the PS5 SDK)")
class AutoPayloadTest(unittest.TestCase):
    def test_plugging_in_turns_the_hub_green(self):
        payload_port = free_port()
        loader = FakeLoader(payload_port)
        tmp = Path(tempfile.mkdtemp())
        settings = tmp / "s.json"
        settings.write_text(json.dumps({"host": "127.0.0.1", "port": payload_port,
                                        "loader_port": loader.port}))
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "pc" / "hub.py"), "--no-browser", "--stay",
             "--settings", str(settings)], stdout=subprocess.PIPE, text=True)
        try:
            m = re.search(r"(http://127\.0\.0\.1:\d+)/\?t=(\S+)", proc.stdout.readline())
            base, token = m.group(1), m.group(2)

            def state():
                req = urllib.request.Request(base + "/api/state", headers={"X-Hub-Token": token})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())["status"]

            deadline = time.monotonic() + 20
            seen = set()
            while time.monotonic() < deadline:
                st = state()
                seen.add(st["state"])
                if st["connected"]:
                    break
                time.sleep(0.3)
            self.assertTrue(st["connected"], seen)
            self.assertEqual(len(loader.received), 1)
            self.assertEqual(loader.received[0],
                             (ROOT / "payload" / "ps5cable.elf").read_bytes())
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            loader.close()
            shutil.rmtree(tmp, ignore_errors=True)


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

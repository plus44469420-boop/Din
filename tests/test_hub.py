"""Tests for the ps5cable Hub HTTP API, run against the desktop payload build
on a fake USB cable."""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from fakecable import build_payload, free_port, start_payload  # noqa: E402
from test_e2e import tree_digest  # noqa: E402


class HubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_payload()
        cls.tmp = Path(tempfile.mkdtemp(prefix="ps5cable-hub-"))
        cable = free_port()
        cls.ps5 = start_payload(cable)
        settings = cls.tmp / "settings.json"
        # Settings from the Ethernet versions are ignored.
        settings.write_text(json.dumps({"host": "192.168.88.2", "port": 9113, "jobs": 3}))
        env = dict(os.environ, PS5CABLE_FAKE_CABLE=f"127.0.0.1:{cable}")
        cls.hub = subprocess.Popen(
            [sys.executable, str(ROOT / "pc" / "hub.py"), "--no-browser", "--stay",
             "--settings", str(settings)],
            stdout=subprocess.PIPE, text=True, env=env)
        line = cls.hub.stdout.readline()
        m = re.search(r"(http://127\.0\.0\.1:\d+)/\?t=(\S+)", line)
        assert m, line
        cls.base, cls.token = m.group(1), m.group(2)
        deadline = time.monotonic() + 15
        while not cls.api(cls, "GET", "/api/state")["status"]["connected"]:
            assert time.monotonic() < deadline, "hub never connected over the fake cable"
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.hub, cls.ps5):
            p.kill()
            p.wait()
        cls.hub.stdout.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def api(self, method, path, body=None, token=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-Hub-Token", self.token if token is None else token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def wait_job(self, job_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = {j["id"]: j for j in self.api("GET", "/api/jobs")["jobs"]}
            if jobs[job_id]["state"] not in ("planning", "running"):
                return jobs[job_id]
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_requires_token_and_local_host(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api("GET", "/api/state", token="wrong")
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/", timeout=5)
        self.assertEqual(cm.exception.code, 403)

    def test_serves_ui_with_token_injected(self):
        with urllib.request.urlopen(f"{self.base}/?t={self.token}", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("ps5cable Hub", html)
        self.assertIn(self.token, html)
        self.assertNotIn("__HUB_TOKEN__", html)

    def test_state_reports_connection(self):
        st = self.api("GET", "/api/state")
        self.assertTrue(st["status"]["connected"], st["status"])
        self.assertEqual(st["status"]["state"], "connected")
        self.assertIn("ps5cable", st["status"]["banner"])
        if os.path.isdir("/data"):  # the desktop payload reports the host's /data
            self.assertGreater(st["status"]["total"], 0)
        self.assertNotIn("host", st["settings"])
        self.assertEqual(st["settings"]["jobs"], 3)

    def test_state_reports_usb_cable(self):
        link = self.api("GET", "/api/state")["status"]["link"]
        self.assertEqual(link["connection"], "USB bridge cable")
        self.assertEqual([t["ends"] for t in link["cable_types"]],
                         ["USB-C ⇄ USB-C", "USB-A ⇄ USB-C", "USB-A ⇄ USB-A"])
        self.assertEqual(link["chip"], "Fake cable (test)")
        self.assertEqual(self.api("GET", "/api/link")["link"]["summary"], link["summary"])

    def test_state_has_cable_guide_before_any_cable(self):
        guide = self.api("GET", "/api/state")["usb_guide"]
        self.assertIn("USB-A ⇄ USB-C", [t["ends"] for t in guide["cable_types"]])
        self.assertIn("blue", guide["pc_port_tip"])
        self.assertIn("rear USB-A", guide["ps5_port_tip"])

    def test_network_endpoints_are_gone(self):
        for path in ("/api/connect", "/api/discover", "/api/send-payload"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.api("POST", path, {})
            self.assertEqual(cm.exception.code, 404, path)

    def test_speedtest(self):
        r = self.api("POST", "/api/speedtest", {"seconds": 1})
        self.assertGreater(r["up_mbs"], 0)
        self.assertGreater(r["down_mbs"], 0)
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(self.api("GET", "/api/state")["status"]["speedtest"]["up_mbs"],
                         r["up_mbs"])

    def test_upload_and_download_jobs(self):
        pc = self.tmp / "pc"
        game = pc / "PPSA09999"
        (game / "sce_sys").mkdir(parents=True)
        (game / "eboot.bin").write_bytes(random.Random(1).randbytes(3_000_000))
        (game / "sce_sys" / "param.json").write_text("{}")
        (pc / "single.txt").write_text("hello")
        ps5 = self.tmp / "ps5"
        ps5.mkdir()

        job = self.api("POST", "/api/transfer", {
            "direction": "upload", "sources": [str(game), str(pc / "single.txt")],
            "dest": str(ps5), "resume": True})
        job = self.wait_job(job["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["files_total"], 3)
        self.assertEqual(job["done"], job["total"])
        self.assertEqual(tree_digest(game), tree_digest(ps5 / "PPSA09999"))

        listing = self.api("GET", "/api/remote/list?path=" + str(ps5))
        self.assertEqual(sorted(e["name"] for e in listing["entries"]),
                         ["PPSA09999", "single.txt"])

        again = self.wait_job(self.api("POST", "/api/transfer", {
            "direction": "upload", "sources": [str(game)], "dest": str(ps5),
            "resume": True})["id"])
        self.assertEqual((again["files_total"], again["skipped"]), (0, 2))

        back = self.tmp / "back"
        back.mkdir()
        job = self.wait_job(self.api("POST", "/api/transfer", {
            "direction": "download", "sources": [str(ps5 / "PPSA09999")],
            "dest": str(back)})["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(tree_digest(game), tree_digest(back / "PPSA09999"))

    def test_cancel_stops_transfer(self):
        big = self.tmp / "big.bin"
        with open(big, "wb") as f:
            f.truncate(3 * 1024 ** 3)
        dest = self.tmp / "cancel-dest"
        dest.mkdir()
        job = self.api("POST", "/api/transfer", {
            "direction": "upload", "sources": [str(big)], "dest": str(dest)})
        time.sleep(0.3)
        self.api("POST", "/api/jobs/cancel", {"id": job["id"]})
        job = self.wait_job(job["id"])
        self.assertEqual(job["state"], "cancelled")
        self.assertLess(job["done"], job["total"])
        big.unlink()

    def test_remote_file_management(self):
        root = self.tmp / "mgmt"
        self.api("POST", "/api/remote/mkdir", {"path": str(root / "a" / "b")})
        self.assertTrue((root / "a" / "b").is_dir())
        self.api("POST", "/api/remote/rename", {"src": str(root / "a"), "dst": str(root / "z")})
        self.assertTrue((root / "z" / "b").is_dir())
        self.api("POST", "/api/remote/delete", {"paths": [str(root / "z")]})
        self.assertFalse((root / "z").exists())
        for p in ("/", "/system/common", "/system_ex", "/mnt/usb0", "/user/app", "/data", "/data/../system"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.api("POST", "/api/remote/delete", {"paths": [p]})
            self.assertEqual(cm.exception.code, 403, p)
            self.assertIn("Not allowed", json.loads(cm.exception.read())["error"])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api("POST", "/api/remote/rename", {"src": str(root), "dst": "/system/x"})
        self.assertEqual(cm.exception.code, 403)

        (root / "one").mkdir()
        (root / "two").mkdir()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api("POST", "/api/remote/rename", {"src": str(root / "one"), "dst": str(root / "two")})
        self.assertEqual(cm.exception.code, 409)
        self.assertTrue((root / "one").is_dir())

        listing = self.api("GET", "/api/remote/list?path=/")
        self.assertGreater(listing["total"], 0)
        self.assertTrue(all(e["protected"] for e in listing["entries"]))
        listing = self.api("GET", "/api/remote/list?path=" + str(root))
        self.assertFalse(any(e["protected"] for e in listing["entries"]))

    def test_remote_copy_move_and_size(self):
        root = self.tmp / "cm"
        game = root / "homebrew" / "PPSA01234"
        (game / "sce_sys").mkdir(parents=True)
        (game / "eboot.bin").write_bytes(random.Random(3).randbytes(2_000_000))
        (game / "sce_sys" / "param.json").write_text("{}")
        (root / "usb").mkdir()
        want = tree_digest(game)

        size = self.api("POST", "/api/remote/size", {"paths": [str(game)]})
        self.assertEqual((size["size"], size["files"], size["folders"]), (2_000_002, 2, 1))

        job = self.api("POST", "/api/remote/copy", {"sources": [str(game)], "dest": str(root / "usb")})
        self.assertEqual(job["direction"], "copy")
        job = self.wait_job(job["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual((job["files_total"], job["total"]), (2, 2_000_002))
        self.assertEqual(tree_digest(root / "usb" / "PPSA01234"), want)

        job = self.wait_job(self.api("POST", "/api/remote/copy",
                                     {"sources": [str(game)], "dest": str(root / "homebrew")})["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(tree_digest(root / "homebrew" / "PPSA01234 (copy)"), want)

        for body, code in (({"sources": [str(game)], "dest": str(root / "usb")}, 409),
                           ({"sources": [str(root)], "dest": str(game)}, 400),
                           ({"sources": [str(root / "nope")], "dest": str(root)}, 404),
                           ({"sources": ["/user/app"], "dest": str(root), "move": True}, 403)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.api("POST", "/api/remote/copy", body)
            self.assertEqual(cm.exception.code, code, body)

        (root / "moved").mkdir()
        job = self.wait_job(self.api("POST", "/api/remote/copy", {
            "sources": [str(root / "homebrew" / "PPSA01234 (copy)")], "dest": str(root / "moved"),
            "move": True})["id"])
        self.assertEqual((job["state"], job["renamed"], job["files_total"]), ("done", 1, 0), job)
        self.assertFalse((root / "homebrew" / "PPSA01234 (copy)").exists())
        self.assertEqual(tree_digest(root / "moved" / "PPSA01234 (copy)"), want)

    @unittest.skipUnless(os.path.isdir("/dev/shm") and os.stat("/dev/shm").st_dev != os.stat(tempfile.gettempdir()).st_dev,
                         "needs a second filesystem")
    def test_move_across_drives_copies_then_deletes(self):
        game = self.tmp / "xdev" / "PPSA05555"
        game.mkdir(parents=True)
        (game / "eboot.bin").write_bytes(b"x" * 100_000)
        want = tree_digest(game)
        other = Path(tempfile.mkdtemp(prefix="ps5cable-drive2-", dir="/dev/shm"))
        try:
            job = self.wait_job(self.api("POST", "/api/remote/copy", {
                "sources": [str(game)], "dest": str(other), "move": True})["id"])
            self.assertEqual((job["state"], job["renamed"], job["files_total"]), ("done", 0, 1), job)
            self.assertFalse(game.exists())
            self.assertEqual(tree_digest(other / "PPSA05555"), want)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_local_list(self):
        d = self.tmp / "ll"
        (d / "sub").mkdir(parents=True)
        (d / "f.bin").write_bytes(b"12345")
        out = self.api("GET", "/api/local/list?path=" + str(d))
        entries = {e["name"]: e for e in out["entries"]}
        self.assertTrue(entries["sub"]["dir"])
        self.assertEqual(entries["f.bin"]["size"], 5)
        self.assertEqual(out["parent"], str(self.tmp))


if __name__ == "__main__":
    unittest.main()

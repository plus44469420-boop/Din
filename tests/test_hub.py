"""Tests for the ps5cable Hub HTTP API, run against the desktop payload build."""

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

from test_e2e import free_port, tree_digest  # noqa: E402
from test_linkinfo import fake_usb_sysfs  # noqa: E402


class HubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-s", "host"], cwd=ROOT / "payload", check=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="ps5cable-hub-"))
        cls.ps5_port = free_port()
        cls.ps5 = subprocess.Popen(
            [str(ROOT / "payload" / "ps5cable-host"), "-p", str(cls.ps5_port)],
            stdout=subprocess.DEVNULL)
        settings = cls.tmp / "settings.json"
        settings.write_text(json.dumps({"host": "127.0.0.1", "port": cls.ps5_port}))
        # Pretend the cable runs through a gigabit USB adapter stuck on a USB 2 port.
        sysfs = fake_usb_sysfs(cls.tmp / "sys", usb_speed="480")
        env = dict(os.environ, PS5CABLE_SYSFS_ROOT=str(sysfs), PS5CABLE_IFACE="enx001")
        cls.hub = subprocess.Popen(
            [sys.executable, str(ROOT / "pc" / "hub.py"), "--no-browser", "--stay",
             "--settings", str(settings)],
            stdout=subprocess.PIPE, text=True, env=env)
        line = cls.hub.stdout.readline()
        m = re.search(r"(http://127\.0\.0\.1:\d+)/\?t=(\S+)", line)
        assert m, line
        cls.base, cls.token = m.group(1), m.group(2)

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
        self.assertIn("ps5cable", st["status"]["banner"])

    def test_state_reports_cable_adapter(self):
        link = self.api("GET", "/api/state")["status"]["link"]
        self.assertEqual(link["chip"], "Realtek RTL8153 (Gigabit)")
        self.assertEqual(link["usb_label"], "USB 2.0 Hi-Speed (480 Mbps)")
        self.assertEqual(link["level"], "warn")
        self.assertEqual(link["expected_mbs"], 38.0)
        self.assertTrue(link["bottleneck"].startswith("USB link"))
        self.assertEqual(len(link["limits"]), 3)
        self.assertIn("USB 2.0", " ".join(link["notes"]))
        self.assertEqual(self.api("GET", "/api/link?refresh=1")["link"]["summary"],
                         link["summary"])

    def test_speedtest(self):
        r = self.api("POST", "/api/speedtest", {"seconds": 1})
        self.assertGreater(r["up_mbs"], 0)
        self.assertGreater(r["down_mbs"], 0)
        self.assertIn(r["verdict"], ("ok", "warn"))
        self.assertEqual(r["expected_mbs"], 38.0)
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
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api("POST", "/api/remote/delete", {"paths": ["/"]})
        self.assertEqual(cm.exception.code, 400)

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

"""Convert screen API: ps5cable-convert driven through the Hub, against a fake game folder."""

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

from fakecable import free_port, start_payload  # noqa: E402
from test_e2e import tree_digest  # noqa: E402

CONVERTER = ROOT / "converter" / "target" / "release" / (
    "ps5cable-convert.exe" if os.name == "nt" else "ps5cable-convert")


def make_game(root, title="PPSA01234", size=4_000_000):
    game = root / title
    (game / "sce_sys").mkdir(parents=True)
    (game / "data").mkdir()
    (game / "sce_sys" / "param.json").write_text(json.dumps({
        "contentId": f"UP9000-{title}_00-TESTGAME00000000", "titleId": title,
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Test Game"}},
        "requiredSystemSoftwareVersion": "0x0400000000000000", "applicationCategoryType": 0,
        "contentVersion": "01.000.000", "masterVersion": "01.00"}))
    block = random.Random(7).randbytes(min(size, 1 << 20))
    with open(game / "eboot.bin", "wb") as f:
        for off in range(0, size, len(block)):
            f.write(block[: size - off])
    (game / "data" / "level.txt").write_text("level data\n" * 50_000)
    (game / "data" / "zeros.bin").write_bytes(bytes(3_000_000))
    return game


@unittest.skipUnless(CONVERTER.is_file() or shutil.which("cargo"), "needs cargo or a built converter")
class ConvertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CONVERTER.is_file():
            subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=ROOT / "converter", check=True)
        subprocess.run(["make", "-s", "host"], cwd=ROOT / "payload", check=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="ps5cable-convert-"))
        cls.game = make_game(cls.tmp / "pc")
        cls.out = cls.tmp / "out"
        cable = free_port()
        cls.ps5 = start_payload(cable)
        settings = cls.tmp / "settings.json"
        settings.write_text(json.dumps({"convert_output": str(cls.out)}))
        cls.hub = subprocess.Popen(
            [sys.executable, str(ROOT / "pc" / "hub.py"), "--no-browser", "--stay", "--settings", str(settings)],
            stdout=subprocess.PIPE, text=True,
            env=dict(os.environ, PS5CABLE_FAKE_CABLE=f"127.0.0.1:{cable}"))
        m = re.search(r"(http://127\.0\.0\.1:\d+)/\?t=(\S+)", cls.hub.stdout.readline())
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

    def api(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-Hub-Token", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def api_error(self, path, body):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api("POST", path, body)
        return json.loads(cm.exception.read())["error"]

    def convert(self, timeout=120, **req):
        job = self.api("POST", "/api/convert/start", req)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = {j["id"]: j for j in self.api("GET", "/api/convert/jobs")["jobs"]}
            if jobs[job["id"]]["state"] not in ("queued", "running"):
                return jobs[job["id"]]
            time.sleep(0.1)
        self.fail("conversion did not finish")

    def test_inspect_and_estimate(self):
        info = self.api("POST", "/api/convert/inspect", {"source": str(self.game)})
        self.assertEqual(info["content_id"], "UP9000-PPSA01234_00-TESTGAME00000000")
        self.assertEqual(info["files"], 4)
        self.assertTrue(info["ps5_title"])
        self.assertFalse(info["is_image"])
        self.assertEqual(info["output_dir"], self.api("GET", "/api/state")["settings"]["convert_output"])
        self.assertEqual(info["ampr_packs"]["kind"], "no_ampr")
        est = self.api("POST", "/api/convert/estimate", {"source": str(self.game)})
        self.assertTrue({"fast", "balanced", "smallest"} <= set(est))
        self.assertIn("nope", self.api_error("/api/convert/inspect", {"source": str(self.tmp / "nope")}))

    def test_images_round_trip_to_folders(self):
        for fmt, compress, ext in (("ffpkg", False, "ffpkg"), ("exfat", False, "exfat"),
                                   ("ffpfs", False, "ffpfs"), ("ffpkg", True, "ffpfsc")):
            with self.subTest(fmt=fmt, compress=compress):
                out = self.tmp / f"img-{fmt}-{compress}"
                job = self.convert(make="image", source=str(self.game), output_dir=str(out),
                                   format=fmt, compress=compress, full_verify=True)
                self.assertEqual(job["state"], "done", job)
                image = Path(job["result"]["path"])
                self.assertEqual(image, out / f"PPSA01234.{ext}")
                self.assertEqual(job["result"]["files"], 4)
                back = self.convert(make="folder", source=str(image), output_dir=str(out / "back"), name="game")
                self.assertEqual(back["state"], "done", back)
                self.assertEqual(tree_digest(self.game), tree_digest(out / "back" / "game"))

    def test_package_options(self):
        out = self.tmp / "pkg"
        job = self.convert(make="pkg", source=str(self.game), output_dir=str(out), compression="fast",
                           language="de-DE", name="test", firmware="5.10")
        self.assertEqual(job["state"], "done", job)
        pkg = Path(job["result"]["path"])
        self.assertEqual(pkg, out / "test.pkg")
        self.assertEqual(job["result"]["firmware"], "5.10")
        back = self.convert(make="folder", source=str(pkg), output_dir=str(out))
        self.assertEqual(back["state"], "done", back)
        param = json.loads((out / "test" / "sce_sys" / "param.json").read_text())
        self.assertEqual(param["requiredSystemSoftwareVersion"], "0x0510000000000000")
        self.assertEqual((out / "test" / "eboot.bin").read_bytes(), (self.game / "eboot.bin").read_bytes())

    def test_refusals_match_ps5upload(self):
        self.assertIn("plain .exfat", self.api_error("/api/convert/start", {
            "make": "image", "source": str(self.game), "format": "ffpkg", "lz4": True}))
        job = self.convert(make="image", source=str(self.game), output_dir=str(self.tmp / "lz"),
                           format="exfat", lz4=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("AMPR", job["error"])
        job = self.convert(make="folder", source=str(self.game), output_dir=str(self.tmp / "f"))
        self.assertEqual(job["state"], "failed")
        self.assertIn("already a folder", job["error"])

    def test_cancel_removes_partial_output(self):
        big = make_game(self.tmp / "big", "PPSA05555", size=600_000_000)
        out = self.tmp / "cancel"
        job = self.api("POST", "/api/convert/start", {"make": "image", "source": str(big),
                                                      "output_dir": str(out), "format": "exfat",
                                                      "full_verify": True})
        queued = self.api("POST", "/api/convert/start", {"make": "pkg", "source": str(big),
                                                         "output_dir": str(out)})
        self.assertEqual(queued["state"], "queued")
        self.api("POST", "/api/convert/cancel", {"id": queued["id"]})
        time.sleep(0.4)
        self.api("POST", "/api/convert/cancel", {"id": job["id"]})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            jobs = {j["id"]: j for j in self.api("GET", "/api/convert/jobs")["jobs"]}
            if jobs[job["id"]]["state"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        self.assertEqual(jobs[job["id"]]["state"], "cancelled", jobs[job["id"]])
        self.assertEqual(jobs[queued["id"]]["state"], "cancelled")
        self.assertEqual([p.name for p in out.iterdir()] if out.exists() else [], [])
        shutil.rmtree(big.parent)

    def test_send_result_to_ps5(self):
        job = self.convert(make="image", source=str(self.game), output_dir=str(self.tmp / "send"),
                           format="ffpkg", compress=True)
        dest = self.tmp / "ps5" / "homebrew"
        dest.mkdir(parents=True)
        up = self.api("POST", "/api/convert/send", {"id": job["id"], "dest": str(dest)})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            j = {x["id"]: x for x in self.api("GET", "/api/jobs")["jobs"]}[up["id"]]
            if j["state"] not in ("planning", "running"):
                break
            time.sleep(0.1)
        self.assertEqual(j["state"], "done", j)
        self.assertEqual((dest / "PPSA01234.ffpfsc").read_bytes(), Path(job["result"]["path"]).read_bytes())

    def test_prefs_are_remembered(self):
        self.convert(make="image", source=str(self.game), output_dir=str(self.tmp / "prefs"),
                     format="ffpfs", verify_chunk_kib=4096)
        prefs = self.api("GET", "/api/state")["settings"]["convert_prefs"]
        self.assertEqual((prefs["make"], prefs["format"], prefs["verify_chunk_kib"]), ("image", "ffpfs", 4096))


if __name__ == "__main__":
    unittest.main()

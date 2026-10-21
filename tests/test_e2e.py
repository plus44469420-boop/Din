"""End-to-end tests: the desktop build of the payload server driven by the
real PC client over the USB link (on a fake cable).
Run with: python3 -m unittest discover -s tests -v"""

import contextlib
import io
import random
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
sys.path.insert(0, str(ROOT / "tests"))

import ps5cable  # noqa: E402
import usblink  # noqa: E402
from fakecable import FakePS5, build_payload  # noqa: E402


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = ps5cable.main(list(argv))
    return rc, out.getvalue()


def tree_digest(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        rel = p.relative_to(root).as_posix()
        out[rel] = p.read_bytes() if p.is_file() else None
    return out


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_payload()
        cls.ps5 = FakePS5()
        cls.port = cls.ps5.port
        ps5cable.LINK = cls.ps5.tunnel

    @classmethod
    def tearDownClass(cls):
        ps5cable.LINK = None
        cls.ps5.close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ps5cable-test-"))
        self.remote = self.tmp / "remote"
        self.remote.mkdir()
        self.base = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conn(self):
        return ps5cable.Connection("127.0.0.1", self.port)

    def make_tree(self, root):
        rng = random.Random(1234)
        (root / "sce_sys" / "nested" / "deeper").mkdir(parents=True)
        (root / "empty_dir").mkdir()
        (root / "eboot.bin").write_bytes(rng.randbytes(9 * 1024 * 1024 + 17))
        (root / "sce_sys" / "param.json").write_text('{"titleId": "TEST00001"}')
        (root / "sce_sys" / "nested" / "deeper" / "zero.bin").write_bytes(b"")
        (root / "unicode é名前.txt").write_text("héllo")
        for i in range(40):
            (root / "sce_sys" / f"small_{i}.dat").write_bytes(rng.randbytes(rng.randint(1, 5000)))

    def test_ping(self):
        with self.conn() as c:
            ver, banner = c.ping()
        self.assertEqual(ver, ps5cable.PROTO_VER)
        self.assertIn("ps5cable", banner)

    def test_put_get_roundtrip_tree(self):
        src = self.tmp / "PPSA00000"
        self.make_tree(src)

        rc, _ = run_cli(*self.base, "put", "-j", "6", str(src), str(self.remote / "games"))
        self.assertEqual(rc, 0)
        self.assertEqual(tree_digest(src), tree_digest(self.remote / "games" / "PPSA00000"))

        back = self.tmp / "back"
        rc, _ = run_cli(*self.base, "get", str(self.remote / "games" / "PPSA00000"), str(back))
        self.assertEqual(rc, 0)
        self.assertEqual(tree_digest(src), tree_digest(back / "PPSA00000"))

    def test_put_resume_continues_partial_file(self):
        data = random.Random(7).randbytes(6 * 1024 * 1024)
        local = self.tmp / "big.bin"
        local.write_bytes(data)
        (self.remote / "big.bin").write_bytes(data[:2_500_000])

        rc, out = run_cli(*self.base, "put", "--resume", str(local), str(self.remote))
        self.assertEqual(rc, 0)
        self.assertIn(ps5cable.human(len(data) - 2_500_000), out)
        self.assertEqual((self.remote / "big.bin").read_bytes(), data)

        rc, out = run_cli(*self.base, "put", "--resume", str(local), str(self.remote))
        self.assertEqual(rc, 0)
        self.assertIn("Uploading 0 files", out)

    def test_get_resume_continues_partial_file(self):
        data = random.Random(8).randbytes(5 * 1024 * 1024 + 3)
        (self.remote / "dump.bin").write_bytes(data)
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        (out_dir / "dump.bin").write_bytes(data[:1234567])

        rc, _ = run_cli(*self.base, "get", "--resume", str(self.remote / "dump.bin"), str(out_dir))
        self.assertEqual(rc, 0)
        self.assertEqual((out_dir / "dump.bin").read_bytes(), data)

    def test_file_management(self):
        with self.conn() as c:
            c.mkdir(str(self.remote / "a" / "b" / "c"))
            self.assertEqual(c.stat(str(self.remote / "a" / "b" / "c"))[0], ps5cable.KIND_DIR)
            (self.remote / "a" / "b" / "f.txt").write_text("x")

            names = {e[0]: e[1] for e in c.listdir(str(self.remote / "a" / "b"))}
            self.assertEqual(names, {"c": ps5cable.KIND_DIR, "f.txt": ps5cable.KIND_FILE})

            c.rename(str(self.remote / "a" / "b" / "f.txt"), str(self.remote / "g.txt"))
            self.assertEqual((self.remote / "g.txt").read_text(), "x")

            with self.assertRaises(ps5cable.RemoteError):
                c.delete(str(self.remote / "a"))
            c.delete(str(self.remote / "a"), recursive=True)
            self.assertFalse((self.remote / "a").exists())
            self.assertIsNone(c.stat(str(self.remote / "a")))

            free, total = c.df(str(self.remote))
            self.assertGreater(total, 0)
            self.assertLessEqual(free, total)

    def test_copy_on_console_in_chunks(self):
        data = random.Random(9).randbytes(3 * 1024 * 1024 + 5)
        (self.remote / "src.bin").write_bytes(data)
        seen = []
        with self.conn() as c:
            c.copy_file(str(self.remote / "src.bin"), str(self.remote / "new" / "dst.bin"),
                        progress=seen.append, chunk=1024 * 1024)
            self.assertEqual((self.remote / "new" / "dst.bin").read_bytes(), data)
            self.assertEqual(seen, [1024 * 1024] * 3 + [5])

            (self.remote / "part.bin").write_bytes(data[:777] + b"garbage")
            c.copy_file(str(self.remote / "src.bin"), str(self.remote / "part.bin"), offset=777)
            self.assertEqual((self.remote / "part.bin").read_bytes(), data)

            (self.remote / "empty").write_bytes(b"")
            c.copy_file(str(self.remote / "empty"), str(self.remote / "empty2"))
            self.assertEqual((self.remote / "empty2").read_bytes(), b"")

            with self.assertRaisesRegex(ps5cable.RemoteError, "same file"):
                c.copy_file(str(self.remote / "src.bin"), str(self.remote / "src.bin"))
            with self.assertRaisesRegex(ps5cable.RemoteError, "not a regular file"):
                c.copy_file(str(self.remote), str(self.tmp / "x"))
            self.assertEqual((self.remote / "src.bin").read_bytes(), data)

    def test_du_counts_tree(self):
        src = self.remote / "PPSA00000"
        self.make_tree(src)
        files = [p for p in src.rglob("*") if p.is_file()]
        with self.conn() as c:
            size, nfiles, folders = c.du(str(src))
            self.assertEqual(size, sum(p.stat().st_size for p in files))
            self.assertEqual(nfiles, len(files))
            self.assertEqual(folders, sum(1 for p in src.rglob("*") if p.is_dir()))
            self.assertEqual(c.du(str(src / "eboot.bin"))[1], 1)
            with self.assertRaises(ps5cable.RemoteError):
                c.du(str(self.remote / "missing"))

    def test_cp_tree_and_cancel(self):
        src = self.remote / "PPSA00000"
        self.make_tree(src)
        (self.remote / "dest").mkdir()
        rc, _ = run_cli(*self.base, "cp", str(src), str(self.remote / "dest"))
        self.assertEqual(rc, 0)
        self.assertEqual(tree_digest(src), tree_digest(self.remote / "dest" / "PPSA00000"))
        with self.assertRaisesRegex(SystemExit, "already exists"):
            run_cli(*self.base, "cp", str(src), str(self.remote / "dest"))

        rc, out = run_cli(*self.base, "du", str(src))
        self.assertEqual(rc, 0)
        self.assertIn("44 files, 4 folders", out)

        cancel = threading.Event()
        with self.conn() as c:
            plan, _, _ = ps5cable.plan_remote_copy(c, [str(src)], str(self.remote))
        plan.sort(key=lambda w: w[2], reverse=True)
        self.assertTrue((self.remote / "PPSA00000 (copy)" / "empty_dir").is_dir())
        old = ps5cable.COPY_CHUNK
        ps5cable.COPY_CHUNK = 1024 * 1024
        try:
            failures = ps5cable.run_remote_copy("127.0.0.1", self.port, plan,
                                                on_bytes=lambda n: cancel.set(), cancel=cancel)
        finally:
            ps5cable.COPY_CHUNK = old
        self.assertEqual(failures, [])
        copied = [p for p in (self.remote / "PPSA00000 (copy)").rglob("*") if p.is_file()]
        self.assertEqual(copied, [], "the half-copied file is removed on cancel")

    def test_errors_keep_connection_usable(self):
        with self.conn() as c:
            with self.assertRaisesRegex(ps5cable.RemoteError, "No such file"):
                c.listdir(str(self.remote / "missing"))
            with self.assertRaisesRegex(ps5cable.RemoteError, "absolute"):
                c.listdir("relative/path")
            with self.assertRaises(ps5cable.RemoteError):
                c.get_file(str(self.remote / "missing"), str(self.tmp / "x"))
            self.assertEqual(c.ping()[0], ps5cable.PROTO_VER)

    def test_put_into_unwritable_location_reports_error(self):
        blocker = self.remote / "file_not_dir"
        blocker.write_text("x")
        local = self.tmp / "f.bin"
        local.write_bytes(b"abc")
        with self.conn() as c:
            with self.assertRaises(ps5cable.RemoteError):
                c.put_file(str(local), str(blocker / "child.bin"))
            self.assertEqual(c.ping()[0], ps5cable.PROTO_VER)

    def test_status_reports_usb_link(self):
        rc, out = run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("PS5 CONNECTED over USB", out)
        self.assertIn("Fake cable", out)
        self.assertIn("/data:", out)

    def test_large_file_crosses_many_frames(self):
        data = random.Random(11).randbytes(3 * usblink.MAX_DATA + 12345)
        (self.tmp / "frames.bin").write_bytes(data)
        with self.conn() as c:
            c.put_file(str(self.tmp / "frames.bin"), str(self.remote / "frames.bin"))
            c.get_file(str(self.remote / "frames.bin"), str(self.tmp / "back.bin"))
        self.assertEqual((self.remote / "frames.bin").read_bytes(), data)
        self.assertEqual((self.tmp / "back.bin").read_bytes(), data)


class NoCableTest(unittest.TestCase):
    class Unplugged:
        def open(self):
            raise usblink.NoDevice("No USB bridge cable found.")

    def test_commands_explain_missing_cable(self):
        link = usblink.Tunnel(self.Unplugged()).start()
        ps5cable.LINK = link
        try:
            rc, out = run_cli("--wait", "0.5", "status")
            self.assertEqual(rc, 1)
            self.assertIn("No USB bridge cable found", out)
            with self.assertRaisesRegex(SystemExit, "PS5 not connected over USB"):
                run_cli("--wait", "0.2", "ls", "/data")
        finally:
            ps5cable.LINK = None
            link.stop()


if __name__ == "__main__":
    unittest.main()

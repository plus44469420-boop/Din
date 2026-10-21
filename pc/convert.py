"""Runs ps5cable-convert (converter/) for the Hub: game folders and images into packages,
game images or folders, the same conversions and options as ps5upload's Convert Games.

Conversions are queued and run one at a time: each reads and writes tens of gigabytes, and
two at once would only fight over the same disk."""

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

EXE_NAME = "ps5cable-convert.exe" if os.name == "nt" else "ps5cable-convert"
DEFAULT_OUTPUT = Path.home() / "Downloads" / "fpkgs"
CACHE_DIR = Path.home() / ".ps5cable" / "cache"
RUNTIME_FILE = "libSceAmpr.sprx-0.4.2.1"

COMPRESSION = ("fast", "balanced", "smallest", "stored")
IMAGE_FORMATS = ("ffpkg", "exfat", "ffpfs")
VERIFY_CHUNK_KIB = (1024, 4096, 8192, 16384, 32768)
IMAGE_EXTS = (".exfat", ".ffpkg", ".ufs2", ".ffpfs", ".ffpfsc", ".pkg", ".fpkg")

STAGE_LABELS = {
    "check": "Checking", "plan": "Planning", "compress": "Compressing", "write": "Writing",
    "verify": "Verifying", "pack": "Packing LZ4 asset packs",
}


class ConvertError(Exception):
    pass


def find_converter():
    here = Path(__file__).resolve().parent
    base = Path(getattr(sys, "_MEIPASS", here))
    for p in (base / EXE_NAME,
              Path(sys.executable).resolve().parent / EXE_NAME,
              here / EXE_NAME,
              here.parent / "converter" / "target" / "release" / EXE_NAME,
              here.parent / "converter" / "target" / "debug" / EXE_NAME):
        if p.is_file():
            return p
    return None


def _no_window():
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


def run_once(args, timeout=600):
    """Runs a quick command (inspect, estimate, runtime-info) and returns its result."""
    exe = find_converter()
    if not exe:
        raise ConvertError("ps5cable-convert was not found. Build it with "
                           "'cargo build --release' in converter/, or put it next to the app.")
    p = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", **_no_window())
    last = None
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") in ("result", "error"):
            last = ev
    if not last:
        raise ConvertError((p.stderr or p.stdout or "the converter failed").strip()[-500:])
    if last["event"] == "error":
        raise ConvertError(last["message"])
    return last["result"]


def inspect(source, output_dir):
    out = run_once(["inspect", source, "--output-dir", output_dir])
    out["output_dir"] = output_dir
    cid = out.get("content_id") or ""
    # A PS5 fake package installs but does not play above FW 11.60; an image does.
    out["ps5_title"] = "PPSA" in cid or Path(source).name.upper().startswith("PPSA")
    return out


def estimate(source, output_dir):
    return run_once(["estimate", source, "--output-dir", output_dir])


def ensure_runtime():
    """ampr_emu 0.4.2.1 for --lz4-install-runtime: fetched once from its GitHub release,
    kept only if it matches the digest the converter expects."""
    info = run_once(["runtime-info"])
    path = CACHE_DIR / RUNTIME_FILE
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]:
        return path
    with urllib.request.urlopen(info["url"], timeout=60) as r:
        data = r.read(16 << 20)
    if hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise ConvertError("The downloaded ampr_emu does not match the expected digest; not used.")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def build_args(req):
    """Command line for one conversion request (validated here, so a bad request fails
    before it is queued)."""
    make = req.get("make")
    source = (req.get("source") or "").strip()
    if not source:
        raise ConvertError("Pick a game folder or image first.")
    out = (req.get("output_dir") or "").strip() or str(DEFAULT_OUTPUT)
    args = [source, "--output-dir", out]
    opt = lambda key, flag: args.extend([flag, str(req[key]).strip()]) if str(req.get(key) or "").strip() else None
    if make == "pkg":
        cmd = "pkg"
        level = req.get("compression") or "balanced"
        if level not in COMPRESSION:
            raise ConvertError(f"Unknown compression {level!r}.")
        args += ["--compression", level]
        opt("content_id", "--content-id")
        opt("name", "--name")
        opt("passcode", "--passcode")
        opt("firmware", "--firmware")
        opt("language", "--language")
    elif make == "image":
        cmd = "image"
        fmt = req.get("format") or "ffpkg"
        if fmt not in IMAGE_FORMATS:
            raise ConvertError(f"Unknown image format {fmt!r}.")
        args += ["--format", fmt]
        lz4 = bool(req.get("lz4"))
        if lz4:
            if fmt != "exfat":
                raise ConvertError("LZ4 asset packs go into a plain .exfat image.")
            args.append("--lz4")
            opt("lz4_profile", "--lz4-profile")
            if req.get("lz4_install_runtime"):
                args += ["--lz4-install-runtime", "--lz4-runtime", str(ensure_runtime())]
        elif req.get("compress"):
            args.append("--compress")
        chunk = int(req.get("verify_chunk_kib") or 8192)
        if chunk not in VERIFY_CHUNK_KIB:
            raise ConvertError(f"Unknown read-back size {chunk} KiB.")
        args += ["--verify-chunk-kib", str(chunk)]
    elif make == "folder":
        cmd = "folder"
        opt("name", "--name")
    elif make == "compress":
        cmd = "compress"
        args = [source]
        if str(req.get("output_dir") or "").strip():
            args += ["--output-dir", out]
        opt("level", "--level")
    else:
        raise ConvertError(f"Unknown conversion {make!r}.")
    if req.get("full_verify"):
        args.append("--full-verify")
    return [cmd, *args]


class ConvertJob:
    def __init__(self, req, argv):
        self.id = uuid.uuid4().hex[:12]
        self.req = req
        self.argv = argv
        self.make = req.get("make")
        self.source = req.get("source")
        self.state = "queued"
        self.stage = None
        self.stage_index = 0
        self.stage_count = 0
        self.done = 0
        self.total = 0
        self.result = None
        self.error = None
        self.log = []
        self.created = time.time()
        self.started = None
        self.finished = None
        self.proc = None
        self.cancelled = False
        self.lock = threading.Lock()

    def on_event(self, ev):
        with self.lock:
            kind = ev.get("event")
            if kind == "stage":
                self.stage = ev["id"]
                self.stage_index, self.stage_count = ev["index"], ev["count"]
                self.done, self.total = ev["done"], ev["total"]
            elif kind == "log":
                self.log = (self.log + [ev["message"]])[-40:]
            elif kind == "result":
                self.result = ev["result"]
                self.result["elapsed_ms"] = ev.get("elapsed_ms")
            elif kind == "error":
                self.error = ev["message"]

    def to_dict(self):
        with self.lock:
            pct = None
            if self.stage_count:
                inside = self.done / self.total if self.total else 0
                pct = 100 * (self.stage_index + inside) / self.stage_count
            return {
                "id": self.id, "make": self.make, "source": self.source, "state": self.state,
                "stage": self.stage, "stage_label": STAGE_LABELS.get(self.stage, self.stage),
                "stage_index": self.stage_index, "stage_count": self.stage_count,
                "done": self.done, "total": self.total, "percent": pct,
                "result": self.result, "error": self.error, "log": self.log[-8:],
                "options": {k: v for k, v in self.req.items() if k != "passcode"},
                "created": self.created, "started": self.started, "finished": self.finished,
            }


class Converter:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, req):
        if not find_converter():
            raise ConvertError("The converter (ps5cable-convert) was not found next to the app.")
        job = ConvertJob(req, build_args(req))
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job)
        return job

    def busy(self):
        with self.lock:
            return any(j.state in ("queued", "running") for j in self.jobs.values())

    def list(self):
        with self.lock:
            jobs = list(self.jobs.values())
        return [j.to_dict() for j in sorted(jobs, key=lambda j: j.created, reverse=True)]

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job:
            raise ConvertError("No such conversion.")
        with job.lock:
            job.cancelled = True
            if job.state == "queued":
                job.state, job.error, job.finished = "cancelled", "cancelled", time.time()
                return
            proc = job.proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("cancel\n")
                proc.stdin.flush()
            except OSError:
                pass

    def clear(self):
        with self.lock:
            self.jobs = {k: j for k, j in self.jobs.items() if j.state in ("queued", "running")}

    def _worker(self):
        while True:
            job = self.queue.get()
            with job.lock:
                if job.state != "queued":
                    continue
                job.state, job.started = "running", time.time()
            try:
                self._run(job)
            except Exception as e:  # keep the worker alive
                job.on_event({"event": "error", "message": str(e)})
            with job.lock:
                job.finished = time.time()
                if job.result is not None:
                    job.state = "done"
                elif job.cancelled:
                    job.state = "cancelled"
                else:
                    job.state = "failed"
                    job.error = job.error or "the converter stopped unexpectedly"

    def _run(self, job):
        exe = find_converter()
        proc = subprocess.Popen([str(exe), *job.argv, "--cancel-on-stdin"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, **_no_window())
        with job.lock:
            job.proc = proc
            cancel_now = job.cancelled
        if cancel_now:
            proc.stdin.write("cancel\n")
            proc.stdin.flush()
        for line in proc.stdout:
            try:
                job.on_event(json.loads(line))
            except ValueError:
                continue
        err = proc.stderr.read()
        proc.wait()
        if proc.returncode and not job.error:
            job.on_event({"event": "error", "message": (err.strip() or f"exit code {proc.returncode}")[-500:]})

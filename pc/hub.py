#!/usr/bin/env python3
"""ps5cable Hub - a two-pane PC <-> PS5 file manager.

Runs a small web server on 127.0.0.1 and opens it in an app window (Edge or
Chrome in --app mode when available, otherwise the default browser). Pick
files on the PC side and send them to the PS5, or the other way around.

    python hub.py                 # start and open the window
    python hub.py --no-browser    # just print the URL
"""

import argparse
import json
import os
import posixpath
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert  # noqa: E402
import linkinfo  # noqa: E402
import ps5cable  # noqa: E402

APP_NAME = "ps5cable Hub"
# Adapter detection shells out to PowerShell on Windows, which takes ~1 s.
LINK_INFO_TTL = 15
SETTINGS_PATH = Path.home() / ".ps5cable-hub.json"
# Without a heartbeat for this long (and no transfer running) the hub exits,
# so closing the window does not leave a hidden server behind. Background tabs
# throttle timers to about once a minute, hence the generous margin.
IDLE_EXIT_SECONDS = 180


def resource_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_payload():
    for p in (resource_dir() / "ps5cable.elf",
              Path(sys.executable).resolve().parent / "ps5cable.elf",
              Path(__file__).resolve().parent / "ps5cable.elf",
              Path(__file__).resolve().parent.parent / "payload" / "ps5cable.elf"):
        if p.is_file():
            return p
    return None


class Settings:
    DEFAULTS = {"host": ps5cable.DEFAULT_HOST, "port": ps5cable.DEFAULT_PORT,
                "local_dir": str(Path.home()), "remote_dir": "/data",
                "resume": True, "jobs": 4, "auto_payload": True,
                "loader_port": ps5cable.ELFLDR_PORT,
                "convert_output": str(convert.DEFAULT_OUTPUT), "convert_prefs": {}}

    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.data[key]

    def update(self, **kw):
        with self.lock:
            self.data.update({k: v for k, v in kw.items() if k in self.DEFAULTS})
            try:
                self.path.write_text(json.dumps(self.data, indent=2))
            except OSError:
                pass
            return dict(self.data)

    def snapshot(self):
        with self.lock:
            return dict(self.data)


class Job:
    def __init__(self, upload, sources, dest):
        self.id = uuid.uuid4().hex[:12]
        self.upload = upload
        self.sources = list(sources)
        self.dest = dest
        self.state = "planning"
        self.total = 0
        self.done = 0
        self.files_total = 0
        self.files_done = 0
        self.skipped = 0
        self.errors = []
        self.current = ""
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self._rate_window = [(time.monotonic(), 0)]

    def add_bytes(self, n):
        with self.lock:
            self.done += n
            now = time.monotonic()
            if now - self._rate_window[-1][0] >= 0.25:
                self._rate_window.append((now, self.done))
                self._rate_window = self._rate_window[-20:]

    def file_done(self, item, err):
        with self.lock:
            self.files_done += 1
            self.current = item[0]
            if err is not None and not isinstance(err, ps5cable.Cancelled):
                self.errors.append({"file": item[0], "error": str(err)})

    def rate(self):
        t0, b0 = self._rate_window[0]
        t1, b1 = time.monotonic(), self.done
        return (b1 - b0) / (t1 - t0) if t1 - t0 > 0.2 else 0.0

    def to_dict(self):
        with self.lock:
            rate = self.rate() if self.state == "running" else 0.0
            return {
                "id": self.id, "direction": "upload" if self.upload else "download",
                "sources": self.sources, "dest": self.dest, "state": self.state,
                "total": self.total, "done": self.done,
                "files_total": self.files_total, "files_done": self.files_done,
                "skipped": self.skipped, "errors": self.errors[-50:],
                "error_count": len(self.errors), "current": self.current,
                "rate": rate,
                "eta": (self.total - self.done) / rate if rate > 0 else None,
                "started": self.started, "finished": self.finished,
            }


class Hub:
    def __init__(self, settings):
        self.settings = settings
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.last_seen = time.monotonic()
        self.status_lock = threading.Lock()
        self.status_cache = None
        self.places_cache = None
        self.payload_sent_at = -1e9
        self.payload_lock = threading.Lock()
        self.link_cache = (None, None, -1e9)  # (local_ip, info, monotonic time)
        self.link_lock = threading.Lock()
        self.speedtest_lock = threading.Lock()
        self.last_speedtest = None
        self.converter = convert.Converter()

    # --- PS5 helpers --------------------------------------------------------

    def target(self):
        return self.settings.get("host"), int(self.settings.get("port"))

    def conn(self, timeout=5.0):
        host, port = self.target()
        return ps5cable.Connection(host, port, timeout=timeout)

    def refresh_status(self):
        """Probes the cable and, when the PS5's ELF loader is up but our payload
        is not, sends the payload so plugging in is all it takes."""
        host, port = self.target()
        loader_port = int(self.settings.get("loader_port"))
        st = ps5cable.probe(host, port, loader_port)
        if st["state"] == "loader" and self.settings.get("auto_payload"):
            with self.payload_lock:
                due = time.monotonic() - self.payload_sent_at > 20
                if due:
                    self.payload_sent_at = time.monotonic()
            elf = find_payload() if due else None
            if elf:
                try:
                    send_elf(host, loader_port, elf)
                    st["state"] = "starting"
                except OSError as e:
                    st["error"] = f"auto-send failed: {e}"
        st["connected"] = st["state"] == "connected"
        st["auto_payload"] = bool(self.settings.get("auto_payload"))
        prev_state = (self.status_cache or {}).get("state")
        st["link"] = self.link_info(st.get("local_ip"), force=prev_state != st["state"])
        st["speedtest"] = self.last_speedtest
        with self.status_lock:
            prev = self.status_cache
            self.status_cache = st
        if st["connected"] and not (prev and prev.get("connected")):
            self.places_cache = None
        return st

    def status(self):
        with self.status_lock:
            st = self.status_cache
        st = dict(st) if st is not None else self.refresh_status()
        st["speedtest"] = self.last_speedtest
        return st

    def monitor(self):
        while True:
            try:
                st = self.refresh_status()
            except Exception as e:  # keep the monitor alive whatever happens
                st = {"connected": False, "state": "offline", "error": str(e)}
                with self.status_lock:
                    self.status_cache = st
            time.sleep(5 if st.get("connected") else 2)

    def link_info(self, local_ip, force=False):
        """Adapter type and speed limits for the interface facing the PS5."""
        with self.link_lock:
            ip, info, at = self.link_cache
            if force or ip != local_ip or time.monotonic() - at > LINK_INFO_TTL:
                info = linkinfo.detect(local_ip)
                self.link_cache = (local_ip, info, time.monotonic())
                if ip != local_ip:
                    self.last_speedtest = None
            return info

    def speed_test(self, seconds):
        if not self.speedtest_lock.acquire(blocking=False):
            raise ApiError("A speed test is already running.", HTTPStatus.CONFLICT)
        try:
            with self.conn(timeout=15) as c:
                raw = c.speed_test(seconds=seconds)
            st = self.status()
            info = st.get("link") or self.link_info(st.get("local_ip"))
            result = linkinfo.interpret_speedtest(info, raw["up_mbs"], raw["down_mbs"])
            result["at"] = time.time()
            result["expected_mbs"] = info.get("expected_mbs")
            self.last_speedtest = result
            return result
        finally:
            self.speedtest_lock.release()

    def remote_places(self):
        if self.places_cache is None:
            with self.conn() as c:
                self.places_cache = build_remote_places(c)
        return self.places_cache

    # --- jobs ---------------------------------------------------------------

    def start_job(self, upload, sources, dest, resume, jobs):
        job = Job(upload, sources, dest)
        with self.jobs_lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run_job, args=(job, resume, jobs),
                         daemon=True).start()
        return job

    def _run_job(self, job, resume, jobs):
        host, port = self.target()
        try:
            with ps5cable.Connection(host, port) as c:
                planner = ps5cable.plan_upload if job.upload else ps5cable.plan_download
                plan, skipped = planner(c, job.sources, job.dest, resume)
            with job.lock:
                job.total = sum(size - off for _, _, size, off in plan)
                job.files_total = len(plan)
                job.skipped = skipped
                job.state = "running"
            ps5cable.run_transfers(host, port, plan, job.upload, jobs,
                                   on_bytes=job.add_bytes, on_file=job.file_done,
                                   cancel=job.cancel)
            with job.lock:
                if job.cancel.is_set():
                    job.state = "cancelled"
                else:
                    job.state = "failed" if job.errors else "done"
        except Exception as e:
            with job.lock:
                job.errors.append({"file": "", "error": str(e)})
                job.state = "cancelled" if job.cancel.is_set() else "failed"
        finally:
            with job.lock:
                job.finished = time.time()

    def busy(self):
        with self.jobs_lock:
            if any(j.state in ("planning", "running") for j in self.jobs.values()):
                return True
        return self.converter.busy()

    def jobs_list(self):
        with self.jobs_lock:
            jobs = list(self.jobs.values())
        return [j.to_dict() for j in sorted(jobs, key=lambda j: j.started, reverse=True)]

    def clear_finished(self):
        with self.jobs_lock:
            for jid in [j for j, job in self.jobs.items()
                        if job.state not in ("planning", "running")]:
                del self.jobs[jid]


# --- local filesystem -------------------------------------------------------

def local_places():
    home = Path.home()
    places = [{"name": "Home", "path": str(home)}]
    for name in ("Desktop", "Downloads", "Documents", "Videos", "Music", "Pictures"):
        p = home / name
        if p.is_dir():
            places.append({"name": name, "path": str(p)})
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                places.append({"name": f"{letter}:", "path": root, "drive": True})
    else:
        places.append({"name": "/", "path": "/", "drive": True})
        for base in ("/Volumes", "/media", "/mnt", f"/run/media/{home.name}"):
            if os.path.isdir(base):
                for d in sorted(os.listdir(base)):
                    full = os.path.join(base, d)
                    if os.path.isdir(full):
                        places.append({"name": d, "path": full, "drive": True})
    return places


def local_list(path):
    path = os.path.abspath(os.path.expanduser(path))
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st = e.stat()
                is_dir = e.is_dir()
            except OSError:
                continue
            if e.name.startswith(".") or (os.name == "nt" and _hidden_windows(st)):
                hidden = True
            else:
                hidden = False
            entries.append({"name": e.name, "dir": is_dir,
                            "size": 0 if is_dir else st.st_size,
                            "mtime": int(st.st_mtime), "hidden": hidden})
    parent = os.path.dirname(path)
    if parent == path:
        parent = None
    try:
        usage = shutil.disk_usage(path)
        free, total = usage.free, usage.total
    except OSError:
        free = total = None
    return {"path": path, "parent": parent, "sep": os.sep, "entries": entries,
            "free": free, "total": total}


def _hidden_windows(st):
    return bool(getattr(st, "st_file_attributes", 0) & 0x2)


def remote_list(hub, path):
    path = path or "/"
    with hub.conn() as c:
        raw = c.listdir(path)
    entries = [{"name": n, "dir": k == ps5cable.KIND_DIR, "size": s, "mtime": m,
                "hidden": n.startswith(".")}
               for n, k, s, m in raw]
    parent = None if path == "/" else (posixpath.dirname(path.rstrip("/")) or "/")
    return {"path": path, "parent": parent, "sep": "/", "entries": entries}


# Folder conventions the PS5 scene tools already scan, as used by ps5upload
# (github.com/phantomptr/ps5upload): ShadowMount+ mounts game images and
# folders from <drive>/homebrew unless /data/shadowmount/config.ini lists
# its own `scanpath=` entries, etaHEN's loader scans <drive>/etaHEN/games, and
# ps5upload keeps uploaded packages in /user/data/ps5upload/pkg_library.
SMP_CONFIG = "/data/shadowmount/config.ini"
DRIVE_CANDIDATES = (["/data", "/mnt/ext0", "/mnt/ext1"]
                    + [f"/mnt/usb{i}" for i in range(8)])
GAME_SUBDIRS = [
    ("homebrew", "Games & homebrew (recommended). ShadowMount+ scans here by default"),
    ("etaHEN/games", "etaHEN game folder. etaHEN's app loader scans here"),
    ("exfat", "Game images (.exfat / .ffpkg / .ffpfs)"),
]
PKG_LIBRARY = "/user/data/ps5upload/pkg_library"


def drive_label(path):
    if path == "/data":
        return "Internal"
    name = posixpath.basename(path)
    if name.startswith("usb"):
        return f"USB {int(name[3:]) + 1}"
    if name.startswith("ext"):
        return "M.2 / Extended" + ("" if name == "ext0" else f" {int(name[3:]) + 1}")
    return path


def parse_scan_paths(ini):
    out = []
    for line in ini.splitlines():
        line = line.strip()
        if line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        if key.lower() == "scanpath" and value:
            out.append(value.rstrip("/") or "/")
    return out


def build_remote_places(conn):
    def is_dir(p):
        st = conn.stat(p)
        return bool(st) and st[0] == ps5cable.KIND_DIR

    drives = [d for d in DRIVE_CANDIDATES if is_dir(d)]
    scan_paths = []
    try:
        ini = conn.read_small(SMP_CONFIG, 256 * 1024)
        scan_paths = parse_scan_paths(ini.decode("utf-8", "replace"))
    except (ps5cable.RemoteError, OSError, ValueError):
        pass

    folders = []
    for d in drives:
        for sub, hint in GAME_SUBDIRS:
            p = f"{d}/{sub}"
            folders.append({"drive": drive_label(d), "path": p, "hint": hint,
                            "exists": is_dir(p),
                            "recommended": sub == "homebrew"})
    for p in scan_paths:
        if not any(f["path"] == p for f in folders):
            folders.append({"drive": "ShadowMount+ config", "path": p,
                            "hint": "Listed as a scanpath in ShadowMount+'s config.ini",
                            "exists": is_dir(p), "recommended": False})
    folders.append({"drive": "Packages", "path": PKG_LIBRARY,
                    "hint": "Where ps5upload keeps .pkg files. Packages still have to be "
                            "installed afterwards (ps5upload or etaHEN)",
                    "exists": is_dir(PKG_LIBRARY), "recommended": False})

    places = [{"name": drive_label(d), "path": d, "drive": True} for d in drives]
    places.insert(1 if drives else 0, {"name": "Homebrew", "path": "/data/homebrew"})
    places += [{"name": "Apps", "path": "/user/app"}, {"name": "Root /", "path": "/"}]
    return {"places": places, "game_folders": folders, "scan_paths": scan_paths}


def send_elf(host, loader_port, path):
    data = Path(path).read_bytes()
    with socket.create_connection((host, loader_port), timeout=5) as s:
        s.sendall(data)


# --- HTTP -------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def make_handler(hub, token, ui_html, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ps5cable-hub"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, status=HTTPStatus.OK):
            self._send(status, json.dumps(obj))

        def _check(self):
            # Host check blocks DNS rebinding; the token blocks other local web
            # pages from driving the API.
            if self.headers.get("Host") not in allowed_hosts:
                raise ApiError("bad host", HTTPStatus.FORBIDDEN)
            if not secrets.compare_digest(self.headers.get("X-Hub-Token", ""), token):
                raise ApiError("bad token", HTTPStatus.FORBIDDEN)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ApiError("request too large")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise ApiError("invalid JSON")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                if self.headers.get("Host") not in allowed_hosts:
                    return self._send(HTTPStatus.FORBIDDEN, "forbidden", "text/plain")
                if parse_qs(url.query).get("t", [""])[0] != token:
                    return self._send(HTTPStatus.FORBIDDEN,
                                      "Open the link printed by ps5cable Hub.", "text/plain")
                return self._send(HTTPStatus.OK, ui_html.replace("__HUB_TOKEN__", token),
                                  "text/html; charset=utf-8")
            if url.path == "/favicon.ico":
                return self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            self._dispatch("GET", url.path, {k: v[0] for k, v in parse_qs(url.query).items()})

        def do_POST(self):
            url = urlparse(self.path)
            try:
                body = self._body()
            except ApiError as e:
                return self._json({"error": str(e)}, e.status)
            self._dispatch("POST", url.path, body)

        def _dispatch(self, method, path, args):
            try:
                self._check()
                hub.last_seen = time.monotonic()
                route = ROUTES.get((method, path))
                if route is None:
                    raise ApiError("not found", HTTPStatus.NOT_FOUND)
                self._json(route(hub, args))
            except ApiError as e:
                self._json({"error": str(e)}, e.status)
            except ps5cable.RemoteError as e:
                self._json({"error": f"PS5: {e}"}, HTTPStatus.BAD_GATEWAY)
            except (ConnectionError, TimeoutError, OSError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_GATEWAY)

    return Handler


def _require(args, *keys):
    for k in keys:
        if k not in args or args[k] in (None, ""):
            raise ApiError(f"missing '{k}'")


def api_state(hub, args):
    return {"app": APP_NAME, "settings": hub.settings.snapshot(),
            "status": hub.status(), "payload": str(find_payload() or ""),
            "local_places": local_places(), "os": os.name}


def api_heartbeat(hub, args):
    return {"jobs": hub.jobs_list()}


def api_settings(hub, args):
    for key in ("port", "loader_port"):
        if key in args:
            args[key] = int(args[key])
    if "jobs" in args:
        args["jobs"] = max(1, min(16, int(args["jobs"])))
    return {"settings": hub.settings.update(**args)}


def api_connect(hub, args):
    if args.get("host"):
        hub.settings.update(host=args["host"].strip())
    return hub.refresh_status()


def api_remote_places(hub, args):
    if not hub.status().get("connected"):
        raise ApiError("PS5 not connected", HTTPStatus.CONFLICT)
    if args.get("refresh"):
        hub.places_cache = None
    return hub.remote_places()


def api_link(hub, args):
    st = hub.status()
    return {"link": hub.link_info(st.get("local_ip"), force=bool(args.get("refresh"))),
            "speedtest": hub.last_speedtest}


def api_speedtest(hub, args):
    if not hub.status().get("connected"):
        raise ApiError("PS5 not connected", HTTPStatus.CONFLICT)
    if hub.busy():
        raise ApiError("Wait for the current transfer to finish; it would skew the result.",
                       HTTPStatus.CONFLICT)
    seconds = max(1.0, min(10.0, float(args.get("seconds") or 3)))
    return hub.speed_test(seconds)


def _converting(fn):
    def wrapped(hub, args):
        try:
            return fn(hub, args)
        except convert.ConvertError as e:
            raise ApiError(str(e))
    return wrapped


def _convert_output(hub, args):
    out = (args.get("output_dir") or "").strip() or hub.settings.get("convert_output")
    return str(Path(out).expanduser())


@_converting
def api_convert_inspect(hub, args):
    _require(args, "source")
    out = _convert_output(hub, args)
    info = convert.inspect(str(Path(args["source"]).expanduser()), out)
    info["converter"] = str(convert.find_converter() or "")
    return info


@_converting
def api_convert_estimate(hub, args):
    _require(args, "source")
    return convert.estimate(str(Path(args["source"]).expanduser()), _convert_output(hub, args))


@_converting
def api_convert_start(hub, args):
    _require(args, "make", "source")
    args["source"] = str(Path(args["source"]).expanduser())
    if args["make"] != "compress" or (args.get("output_dir") or "").strip():
        args["output_dir"] = _convert_output(hub, args)
    prefs = {k: v for k, v in args.items()
             if k in ("make", "compression", "language", "format", "compress", "lz4",
                      "lz4_install_runtime", "verify_chunk_kib", "full_verify")}
    hub.settings.update(convert_output=args.get("output_dir") or hub.settings.get("convert_output"),
                        convert_prefs={**hub.settings.get("convert_prefs"), **prefs})
    return hub.converter.submit(args).to_dict()


def api_convert_jobs(hub, args):
    return {"jobs": hub.converter.list(), "converter": str(convert.find_converter() or "")}


@_converting
def api_convert_cancel(hub, args):
    _require(args, "id")
    hub.converter.cancel(args["id"])
    return {"jobs": hub.converter.list()}


def api_convert_clear(hub, args):
    hub.converter.clear()
    return {"jobs": hub.converter.list()}


def api_convert_send(hub, args):
    """Copies a finished conversion to the PS5, as a normal upload job."""
    _require(args, "id", "dest")
    job = hub.converter.get(args["id"])
    if not job or job.state != "done":
        raise ApiError("That conversion has not finished.")
    if not hub.status().get("connected"):
        raise ApiError("PS5 not connected", HTTPStatus.CONFLICT)
    return hub.start_job(True, [job.result["path"]], args["dest"], True,
                         int(hub.settings.get("jobs"))).to_dict()


def api_discover(hub, args):
    found = ps5cable.discover(int(hub.settings.get("port")), timeout=1.5)
    return {"found": [{"host": ip, "banner": b} for ip, b in found]}


def api_send_payload(hub, args):
    path = Path(args["path"]) if args.get("path") else find_payload()
    if not path or not path.is_file():
        raise ApiError("ps5cable.elf not found. Put it next to the app or pick it.")
    hub.payload_sent_at = time.monotonic()
    send_elf(hub.settings.get("host"),
             int(args.get("loader_port") or hub.settings.get("loader_port")), path)
    for _ in range(16):
        time.sleep(0.5)
        st = hub.refresh_status()
        if st["connected"]:
            return st
    raise ApiError("Payload sent, but it did not answer. Check the PS5 screen.",
                   HTTPStatus.BAD_GATEWAY)


def api_local_list(hub, args):
    path = args.get("path") or hub.settings.get("local_dir")
    try:
        out = local_list(path)
    except FileNotFoundError:
        out = local_list(str(Path.home()))
    except PermissionError:
        raise ApiError(f"Permission denied: {path}")
    hub.settings.update(local_dir=out["path"])
    return out


def api_local_mkdir(hub, args):
    _require(args, "path")
    os.makedirs(args["path"], exist_ok=True)
    return {"ok": True}


def api_local_open(hub, args):
    _require(args, "path")
    p = args["path"]
    if os.name == "nt":
        os.startfile(p)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])
    return {"ok": True}


def api_remote_list(hub, args):
    path = args.get("path") or hub.settings.get("remote_dir")
    out = remote_list(hub, path)
    hub.settings.update(remote_dir=out["path"])
    return out


def api_remote_mkdir(hub, args):
    _require(args, "path")
    with hub.conn() as c:
        c.mkdir(args["path"])
    hub.places_cache = None
    return {"ok": True}


def api_remote_delete(hub, args):
    _require(args, "paths")
    with hub.conn() as c:
        for p in args["paths"]:
            if p in ("/", ""):
                raise ApiError("refusing to delete /")
            c.delete(p, recursive=True)
    hub.places_cache = None
    return {"ok": True}


def api_remote_rename(hub, args):
    _require(args, "src", "dst")
    with hub.conn() as c:
        c.rename(args["src"], args["dst"])
    hub.places_cache = None
    return {"ok": True}


def api_transfer(hub, args):
    _require(args, "direction", "sources", "dest")
    if args["direction"] not in ("upload", "download"):
        raise ApiError("direction must be upload or download")
    if not isinstance(args["sources"], list) or not args["sources"]:
        raise ApiError("nothing selected")
    job = hub.start_job(args["direction"] == "upload", args["sources"], args["dest"],
                        bool(args.get("resume", hub.settings.get("resume"))),
                        int(args.get("jobs") or hub.settings.get("jobs")))
    return job.to_dict()


def api_cancel(hub, args):
    _require(args, "id")
    with hub.jobs_lock:
        job = hub.jobs.get(args["id"])
    if not job:
        raise ApiError("no such job", HTTPStatus.NOT_FOUND)
    job.cancel.set()
    return job.to_dict()


def api_clear(hub, args):
    hub.clear_finished()
    return {"jobs": hub.jobs_list()}


def api_quit(hub, args):
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True}


ROUTES = {
    ("GET", "/api/state"): api_state,
    ("GET", "/api/jobs"): api_heartbeat,
    ("GET", "/api/local/list"): api_local_list,
    ("GET", "/api/remote/list"): api_remote_list,
    ("GET", "/api/remote/places"): api_remote_places,
    ("POST", "/api/settings"): api_settings,
    ("POST", "/api/connect"): api_connect,
    ("GET", "/api/link"): api_link,
    ("POST", "/api/speedtest"): api_speedtest,
    ("GET", "/api/convert/jobs"): api_convert_jobs,
    ("POST", "/api/convert/inspect"): api_convert_inspect,
    ("POST", "/api/convert/estimate"): api_convert_estimate,
    ("POST", "/api/convert/start"): api_convert_start,
    ("POST", "/api/convert/cancel"): api_convert_cancel,
    ("POST", "/api/convert/clear"): api_convert_clear,
    ("POST", "/api/convert/send"): api_convert_send,
    ("POST", "/api/discover"): api_discover,
    ("POST", "/api/send-payload"): api_send_payload,
    ("POST", "/api/local/mkdir"): api_local_mkdir,
    ("POST", "/api/local/open"): api_local_open,
    ("POST", "/api/remote/mkdir"): api_remote_mkdir,
    ("POST", "/api/remote/delete"): api_remote_delete,
    ("POST", "/api/remote/rename"): api_remote_rename,
    ("POST", "/api/transfer"): api_transfer,
    ("POST", "/api/jobs/cancel"): api_cancel,
    ("POST", "/api/jobs/clear"): api_clear,
    ("POST", "/api/quit"): api_quit,
}


def open_app_window(url):
    """Prefers a chromeless Edge/Chrome app window; falls back to a tab."""
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                               Path(base) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                       Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    for exe in candidates:
        if exe.is_file():
            try:
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=1360,860"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    webbrowser.open(url)


def idle_watchdog(hub):
    while True:
        time.sleep(15)
        if not hub.busy() and time.monotonic() - hub.last_seen > IDLE_EXIT_SECONDS:
            os._exit(0)


def main(argv=None):
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument("--port", type=int, default=0, help="local UI port (default: random)")
    p.add_argument("--no-browser", action="store_true", help="do not open a window")
    p.add_argument("--stay", action="store_true",
                   help="keep running after the window is closed")
    p.add_argument("--settings", type=Path, default=SETTINGS_PATH)
    args = p.parse_args(argv)

    hub = Hub(Settings(args.settings))
    token = secrets.token_urlsafe(24)
    ui_html = (resource_dir() / "hub_ui.html").read_text(encoding="utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), None)
    port = server.server_address[1]
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    server.RequestHandlerClass = make_handler(hub, token, ui_html, allowed)
    server.daemon_threads = True

    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"{APP_NAME} running at {url}", flush=True)
    if not args.no_browser:
        open_app_window(url)
    threading.Thread(target=hub.monitor, daemon=True).start()
    if not args.stay:
        threading.Thread(target=idle_watchdog, args=(hub,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

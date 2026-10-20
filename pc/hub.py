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
import ps5cable  # noqa: E402

APP_NAME = "ps5cable Hub"
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
                "resume": True, "jobs": 4}

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

    # --- PS5 helpers --------------------------------------------------------

    def target(self):
        return self.settings.get("host"), int(self.settings.get("port"))

    def conn(self, timeout=5.0):
        host, port = self.target()
        return ps5cable.Connection(host, port, timeout=timeout)

    def status(self):
        host, port = self.target()
        try:
            with ps5cable.Connection(host, port, timeout=1.5) as c:
                _, banner = c.ping()
                try:
                    free, total = c.df("/data")
                except ps5cable.RemoteError:
                    free = total = None
            return {"connected": True, "host": host, "port": port,
                    "banner": banner, "free": free, "total": total}
        except OSError as e:
            return {"connected": False, "host": host, "port": port, "error": str(e)}

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
            return any(j.state in ("planning", "running") for j in self.jobs.values())

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


REMOTE_PLACES = [
    {"name": "Internal /data", "path": "/data"},
    {"name": "Homebrew", "path": "/data/homebrew"},
    {"name": "USB 1", "path": "/mnt/usb0"},
    {"name": "USB 2", "path": "/mnt/usb1"},
    {"name": "M.2 / ext", "path": "/mnt/ext0"},
    {"name": "Apps", "path": "/user/app"},
    {"name": "Root /", "path": "/"},
]


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
            "local_places": local_places(), "remote_places": REMOTE_PLACES,
            "os": os.name}


def api_heartbeat(hub, args):
    return {"jobs": hub.jobs_list()}


def api_settings(hub, args):
    if "port" in args:
        args["port"] = int(args["port"])
    if "jobs" in args:
        args["jobs"] = max(1, min(16, int(args["jobs"])))
    return {"settings": hub.settings.update(**args)}


def api_connect(hub, args):
    if args.get("host"):
        hub.settings.update(host=args["host"].strip())
    return hub.status()


def api_discover(hub, args):
    found = ps5cable.discover(int(hub.settings.get("port")), timeout=1.5)
    return {"found": [{"host": ip, "banner": b} for ip, b in found]}


def api_send_payload(hub, args):
    path = Path(args["path"]) if args.get("path") else find_payload()
    if not path or not path.is_file():
        raise ApiError("ps5cable.elf not found. Put it next to the app or pick it.")
    host = hub.settings.get("host")
    data = path.read_bytes()
    with socket.create_connection((host, int(args.get("loader_port") or ps5cable.ELFLDR_PORT)),
                                  timeout=5) as s:
        s.sendall(data)
    for _ in range(16):
        time.sleep(0.5)
        st = hub.status()
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
    return {"ok": True}


def api_remote_delete(hub, args):
    _require(args, "paths")
    with hub.conn() as c:
        for p in args["paths"]:
            if p in ("/", ""):
                raise ApiError("refusing to delete /")
            c.delete(p, recursive=True)
    return {"ok": True}


def api_remote_rename(hub, args):
    _require(args, "src", "dst")
    with hub.conn() as c:
        c.rename(args["src"], args["dst"])
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
    ("POST", "/api/settings"): api_settings,
    ("POST", "/api/connect"): api_connect,
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
    if not args.stay:
        threading.Thread(target=idle_watchdog, args=(hub,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

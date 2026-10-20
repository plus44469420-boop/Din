#!/usr/bin/env python3
"""ps5cable - move files between a PC and a jailbroken PS5 over a direct cable.

Talks to the ps5cable payload (payload/ps5cable.elf) running on the console.
Standard library only; works on Windows, macOS and Linux with Python 3.8+.

Examples:
    python ps5cable.py send-payload ps5cable.elf
    python ps5cable.py put "D:/Games/PPSA01234" /data/homebrew
    python ps5cable.py get /data/homebrew/PPSA01234 ./backup
    python ps5cable.py ls /data
"""

import argparse
import os
import posixpath
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_PORT = 9113
ELFLDR_PORT = 9021
DEFAULT_HOST = "192.168.88.2"
MAGIC = b"P5CX"
PROTO_VER = 1

CMD_PING, CMD_LIST, CMD_STAT, CMD_MKDIR, CMD_DELETE = 1, 2, 3, 4, 5
CMD_RENAME, CMD_PUT, CMD_GET, CMD_DF, CMD_SHUTDOWN = 6, 7, 8, 9, 10

F_RECURSIVE = 1 << 0
F_MKDIRS = 1 << 1

KIND_FILE, KIND_DIR, KIND_OTHER = 0, 1, 2

REQ = struct.Struct("<4sBBHQQ")
RES = struct.Struct("<BBHIQQ")
ENTRY = struct.Struct("<BQqH")

CHUNK = 4 * 1024 * 1024
SOCK_BUF = 4 * 1024 * 1024


class RemoteError(Exception):
    """The payload rejected a request; the connection is still usable."""


class Connection:
    def __init__(self, host, port=DEFAULT_PORT, timeout=15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(120.0)
        for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, opt, SOCK_BUF)
            except OSError:
                pass
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _recv_exact(self, n):
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = self.sock.recv_into(view[got:], n - got)
            if not k:
                raise ConnectionError("PS5 closed the connection")
            got += k
        return bytes(buf)

    def _request(self, cmd, path="", flags=0, a=0, b=0):
        raw = path.encode("utf-8") if isinstance(path, str) else path
        self.sock.sendall(REQ.pack(MAGIC, cmd, flags, len(raw), a, b) + raw)

    def _response(self):
        status, kind, _, mode, a, b = RES.unpack(self._recv_exact(RES.size))
        if status != 0:
            raise RemoteError(self._recv_exact(a).decode("utf-8", "replace"))
        return kind, mode, a, b

    def call(self, cmd, path="", flags=0, a=0, b=0):
        self._request(cmd, path, flags, a, b)
        return self._response()

    def ping(self):
        _, _, ver, n = self.call(CMD_PING)
        return ver, self._recv_exact(n).decode()

    def stat(self, path):
        """Returns (kind, size, mtime) or None if the path does not exist."""
        try:
            kind, _, size, mtime = self.call(CMD_STAT, path)
        except RemoteError:
            return None
        return kind, size, mtime

    def listdir(self, path):
        _, _, n, _ = self.call(CMD_LIST, path)
        blob = self._recv_exact(n)
        out, i = [], 0
        while i < len(blob):
            kind, size, mtime, nl = ENTRY.unpack_from(blob, i)
            i += ENTRY.size
            out.append((blob[i:i + nl].decode("utf-8", "replace"), kind, size, mtime))
            i += nl
        return out

    def mkdir(self, path):
        self.call(CMD_MKDIR, path)

    def delete(self, path, recursive=False):
        self.call(CMD_DELETE, path, F_RECURSIVE if recursive else 0)

    def rename(self, src, dst):
        self.call(CMD_RENAME, src.encode() + b"\0" + dst.encode())

    def df(self, path):
        _, _, free, total = self.call(CMD_DF, path)
        return free, total

    def shutdown(self):
        self.call(CMD_SHUTDOWN)

    def put_file(self, local, remote, offset=0, progress=None):
        size = os.path.getsize(local)
        self._request(CMD_PUT, remote, F_MKDIRS, size, offset)
        self._response()
        with open(local, "rb") as f:
            f.seek(offset)
            remaining = size - offset
            while remaining:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    raise IOError(f"{local} shrank while uploading")
                try:
                    self.sock.sendall(chunk)
                except OSError:
                    # The payload replies with the reason (e.g. disk full)
                    # before closing, so surface that instead of a bare EPIPE.
                    self._response()
                    raise
                remaining -= len(chunk)
                if progress:
                    progress(len(chunk))
        self._response()

    def get_file(self, remote, local, offset=0, progress=None):
        self._request(CMD_GET, remote, 0, offset)
        _, _, remaining, _ = self._response()
        buf = bytearray(CHUNK)
        view = memoryview(buf)
        with open(local, "r+b" if offset else "wb") as f:
            if offset:
                f.seek(offset)
                f.truncate()
            while remaining:
                k = self.sock.recv_into(view, min(CHUNK, remaining))
                if not k:
                    raise ConnectionError(f"transfer of {remote} was cut short")
                f.write(view[:k])
                remaining -= k
                if progress:
                    progress(k)


class Progress:
    def __init__(self, total_bytes, total_files):
        self.total = total_bytes
        self.files = total_files
        self.done = 0
        self.files_done = 0
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.last = 0.0
        self.tty = sys.stderr.isatty()

    def add(self, n):
        with self.lock:
            self.done += n
            now = time.monotonic()
            if now - self.last >= 0.5:
                self.last = now
                self._draw(now)

    def file_finished(self):
        with self.lock:
            self.files_done += 1

    def _draw(self, now):
        elapsed = max(now - self.start, 1e-6)
        rate = self.done / elapsed
        pct = 100.0 * self.done / self.total if self.total else 100.0
        eta = (self.total - self.done) / rate if rate > 0 else 0
        line = (f"{pct:5.1f}%  {human(self.done)}/{human(self.total)}  "
                f"{human(rate)}/s  files {self.files_done}/{self.files}  "
                f"ETA {fmt_time(eta)}")
        if self.tty:
            sys.stderr.write("\r" + line.ljust(78))
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def finish(self):
        with self.lock:
            self._draw(time.monotonic())
            elapsed = time.monotonic() - self.start
        if self.tty:
            sys.stderr.write("\n")
        print(f"Transferred {human(self.done)} in {fmt_time(elapsed)} "
              f"({human(self.done / max(elapsed, 1e-6))}/s)")


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def fmt_time(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def discover(port=DEFAULT_PORT, timeout=1.5, targets=None):
    """Broadcasts on every local IPv4 interface and returns [(ip, banner)]."""
    if targets is None:
        targets = {"255.255.255.255"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    targets.add(ip.rsplit(".", 1)[0] + ".255")
        except socket.gaierror:
            pass
        # The setup scripts in this repo use this subnet for the direct cable.
        targets.add("192.168.88.255")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)
    found = {}
    try:
        for t in targets:
            try:
                sock.sendto(b"P5CX?", (t, port))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (ip, _) = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(b"P5CX!"):
                found[ip] = data[5:].decode(errors="replace").strip()
    finally:
        sock.close()
    return sorted(found.items())


def resolve_host(args):
    if args.host:
        return args.host
    env = os.environ.get("PS5_HOST")
    if env:
        return env
    found = discover(args.port)
    if found:
        return found[0][0]
    return DEFAULT_HOST


def connect(args):
    host = resolve_host(args)
    try:
        return Connection(host, args.port)
    except OSError as e:
        sys.exit(f"Cannot reach the ps5cable payload at {host}:{args.port}: {e}\n"
                 "Is the cable plugged in, is the payload running, and are both "
                 "sides on the same subnet? Try: python ps5cable.py discover")


def remote_join(*parts):
    return posixpath.join(*parts)


# --- commands -------------------------------------------------------------

def cmd_discover(args):
    found = discover(args.port, timeout=2.0)
    if not found:
        print("No PS5 answered. Check the cable, the IP setup, and that the "
              "payload is running.")
        return 1
    for ip, banner in found:
        print(f"{ip}\tps5cable {banner}")
    return 0


def cmd_send_payload(args):
    # Discovery needs the payload, which is not running yet.
    host = args.host or os.environ.get("PS5_HOST") or DEFAULT_HOST
    data = Path(args.elf).read_bytes()
    with socket.create_connection((host, args.loader_port), timeout=10) as s:
        s.sendall(data)
    print(f"Sent {args.elf} ({human(len(data))}) to {host}:{args.loader_port}")
    for _ in range(20):
        time.sleep(0.5)
        try:
            with Connection(host, args.port, timeout=2) as c:
                _, banner = c.ping()
                print(f"Payload is up: {banner}")
                return 0
        except OSError:
            pass
    print("Payload sent, but it did not answer yet. Watch the PS5 screen for a "
          "notification.")
    return 1


def cmd_ping(args):
    with connect(args) as c:
        ver, banner = c.ping()
        print(f"{banner} (protocol {ver})")
    return 0


def cmd_ls(args):
    with connect(args) as c:
        entries = sorted(c.listdir(args.path), key=lambda e: (e[1] != KIND_DIR, e[0].lower()))
    for name, kind, size, mtime in entries:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        if kind == KIND_DIR:
            print(f"{'<DIR>':>12}  {stamp}  {name}/")
        else:
            print(f"{human(size):>12}  {stamp}  {name}")
    return 0


def cmd_mkdir(args):
    with connect(args) as c:
        for p in args.paths:
            c.mkdir(p)
    return 0


def cmd_rm(args):
    with connect(args) as c:
        for p in args.paths:
            c.delete(p, recursive=args.recursive)
    return 0


def cmd_mv(args):
    with connect(args) as c:
        c.rename(args.src, args.dst)
    return 0


def cmd_df(args):
    with connect(args) as c:
        free, total = c.df(args.path)
    print(f"{args.path}: {human(free)} free of {human(total)}")
    return 0


def cmd_shutdown(args):
    with connect(args) as c:
        c.shutdown()
    print("Payload stopped.")
    return 0


def _run_parallel(host, port, jobs, work, worker_fn, progress):
    """Runs worker_fn(conn, item) for each item, one persistent connection per
    worker thread."""
    local = threading.local()
    conns, conns_lock = [], threading.Lock()

    def task(item):
        if getattr(local, "conn", None) is None:
            local.conn = Connection(host, port)
            with conns_lock:
                conns.append(local.conn)
        try:
            worker_fn(local.conn, item)
        except Exception:
            # A failure mid-stream leaves unread bytes on the socket, so the
            # connection cannot be reused for the next file.
            local.conn.close()
            local.conn = None
            raise
        finally:
            progress.file_finished()

    failures = []
    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(task, item): item for item in work}
            for fut in as_completed(futures):
                err = fut.exception()
                if err:
                    failures.append((futures[fut], err))
    finally:
        for c in conns:
            c.close()
    progress.finish()
    for item, err in failures:
        print(f"FAILED {item[0]}: {err}", file=sys.stderr)
    return 1 if failures else 0


def cmd_put(args):
    host = resolve_host(args)
    dest = args.remote.rstrip("/") or "/"
    work, dirs = [], []

    for src in args.local:
        src_path = Path(src)
        if not src_path.exists():
            sys.exit(f"No such file or directory: {src}")
        base = remote_join(dest, src_path.name)
        if src_path.is_dir():
            dirs.append(base)
            for root, dnames, fnames in os.walk(src_path):
                rel = Path(root).relative_to(src_path).as_posix()
                rdir = base if rel == "." else remote_join(base, rel)
                for d in dnames:
                    dirs.append(remote_join(rdir, d))
                for f in fnames:
                    lp = os.path.join(root, f)
                    work.append((lp, remote_join(rdir, f), os.path.getsize(lp)))
        else:
            work.append((str(src_path), base, src_path.stat().st_size))

    with Connection(host, args.port) as c:
        if args.resume:
            plan = []
            for lp, rp, size in work:
                st = c.stat(rp)
                if st and st[0] == KIND_FILE and st[1] == size:
                    continue
                offset = st[1] if st and st[0] == KIND_FILE and st[1] < size else 0
                plan.append((lp, rp, size, offset))
        else:
            plan = [(lp, rp, size, 0) for lp, rp, size in work]
        for d in dirs:
            c.mkdir(d)

    skipped = len(work) - len(plan)
    total = sum(size - off for _, _, size, off in plan)
    print(f"Uploading {len(plan)} files ({human(total)}) to {host}:{dest}"
          + (f", skipping {skipped} already complete" if skipped else ""))
    progress = Progress(total, len(plan))
    # Largest first keeps the long transfers from becoming the tail.
    plan.sort(key=lambda w: w[2], reverse=True)

    def upload(conn, item):
        lp, rp, _, off = item
        conn.put_file(lp, rp, off, progress.add)

    return _run_parallel(host, args.port, args.jobs, plan, upload, progress)


def cmd_get(args):
    host = resolve_host(args)
    src = args.remote.rstrip("/") or "/"
    dest = Path(args.local)
    work = []

    with Connection(host, args.port) as c:
        st = c.stat(src)
        if st is None:
            sys.exit(f"No such file or directory on PS5: {src}")
        name = posixpath.basename(src) or "root"
        if st[0] == KIND_DIR:
            stack = [(src, dest / name)]
            while stack:
                rdir, ldir = stack.pop()
                ldir.mkdir(parents=True, exist_ok=True)
                for ename, kind, size, _ in c.listdir(rdir):
                    rp = remote_join(rdir, ename)
                    if kind == KIND_DIR:
                        stack.append((rp, ldir / ename))
                    elif kind == KIND_FILE:
                        work.append((rp, ldir / ename, size))
        else:
            target = dest / name if dest.is_dir() else dest
            target.parent.mkdir(parents=True, exist_ok=True)
            work.append((src, target, st[1]))

    plan = []
    for rp, lp, size in work:
        offset = 0
        if args.resume and lp.exists():
            have = lp.stat().st_size
            if have == size:
                continue
            if have < size:
                offset = have
        plan.append((rp, lp, size, offset))

    total = sum(size - off for _, _, size, off in plan)
    print(f"Downloading {len(plan)} files ({human(total)}) from {host}:{src}")
    progress = Progress(total, len(plan))
    plan.sort(key=lambda w: w[2], reverse=True)

    def download(conn, item):
        rp, lp, _, off = item
        conn.get_file(rp, str(lp), off, progress.add)

    return _run_parallel(host, args.port, args.jobs, plan, download, progress)


def build_parser():
    p = argparse.ArgumentParser(
        prog="ps5cable",
        description="Transfer files between this PC and a PS5 over a direct cable.")
    p.add_argument("--host", help="PS5 IP (default: $PS5_HOST, then auto-discover, "
                                  f"then {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"ps5cable payload port (default {DEFAULT_PORT})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("discover", help="find PS5s running the payload")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("send-payload", help="send ps5cable.elf to the ELF loader")
    s.add_argument("elf", nargs="?", default=str(Path(__file__).resolve().parent.parent
                                                  / "payload" / "ps5cable.elf"))
    s.add_argument("--loader-port", type=int, default=ELFLDR_PORT)
    s.set_defaults(func=cmd_send_payload)

    s = sub.add_parser("ping", help="check the payload is reachable")
    s.set_defaults(func=cmd_ping)

    s = sub.add_parser("ls", help="list a PS5 directory")
    s.add_argument("path", nargs="?", default="/data")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("put", help="upload files/folders to a PS5 directory")
    s.add_argument("local", nargs="+", help="local files or folders")
    s.add_argument("remote", help="destination directory on the PS5, e.g. /data/homebrew")
    s.add_argument("-j", "--jobs", type=int, default=4,
                   help="parallel connections (default 4; helps with many small files)")
    s.add_argument("--resume", action="store_true",
                   help="skip files already complete and continue partial ones")
    s.set_defaults(func=cmd_put)

    s = sub.add_parser("get", help="download a PS5 file/folder")
    s.add_argument("remote")
    s.add_argument("local", nargs="?", default=".")
    s.add_argument("-j", "--jobs", type=int, default=4)
    s.add_argument("--resume", action="store_true")
    s.set_defaults(func=cmd_get)

    s = sub.add_parser("mkdir", help="create directories (with parents)")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_mkdir)

    s = sub.add_parser("rm", help="delete files or directories")
    s.add_argument("-r", "--recursive", action="store_true")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("mv", help="rename/move on the PS5")
    s.add_argument("src")
    s.add_argument("dst")
    s.set_defaults(func=cmd_mv)

    s = sub.add_parser("df", help="free space")
    s.add_argument("path", nargs="?", default="/data")
    s.set_defaults(func=cmd_df)

    s = sub.add_parser("shutdown", help="stop the payload on the PS5")
    s.set_defaults(func=cmd_shutdown)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RemoteError as e:
        print(f"PS5 error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with --resume to continue.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

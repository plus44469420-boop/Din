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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import linkinfo  # noqa: E402

DEFAULT_PORT = 9113
ELFLDR_PORT = 9021
DEFAULT_HOST = "192.168.88.2"
MAGIC = b"P5CX"
PROTO_VER = 1

CMD_PING, CMD_LIST, CMD_STAT, CMD_MKDIR, CMD_DELETE = 1, 2, 3, 4, 5
CMD_RENAME, CMD_PUT, CMD_GET, CMD_DF, CMD_SHUTDOWN = 6, 7, 8, 9, 10
CMD_SINK, CMD_SOURCE, CMD_COPY, CMD_DU = 11, 12, 13, 14

F_RECURSIVE = 1 << 0
F_MKDIRS = 1 << 1

KIND_FILE, KIND_DIR, KIND_OTHER = 0, 1, 2

REQ = struct.Struct("<4sBBHQQ")
RES = struct.Struct("<BBHIQQ")
ENTRY = struct.Struct("<BQqH")

CHUNK = 4 * 1024 * 1024
SOCK_BUF = 4 * 1024 * 1024
# Console-side copies run in chunks this big, each well inside the socket
# timeout even on a slow USB drive.
COPY_CHUNK = 128 * 1024 * 1024


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

    def du(self, path):
        """Returns (bytes, files, folders) under `path`, counted on the PS5."""
        _, folders, size, files = self.call(CMD_DU, path)
        return size, files, folders

    def copy_file(self, src, dst, offset=0, progress=None, chunk=None):
        """Copies a file on the PS5 without sending it over the cable."""
        raw = src.encode() + b"\0" + dst.encode()
        while True:
            _, _, n, size = self.call(CMD_COPY, raw, 0, offset, chunk or COPY_CHUNK)
            offset += n
            if n and progress:
                progress(n)
            if offset >= size:
                return
            if not n:
                raise RemoteError(f"copy of {src} stopped making progress")

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

    def speed_upload(self, nbytes):
        """Sends nbytes that the PS5 discards; returns MB/s as timed by the PS5."""
        self._request(CMD_SINK, "", 0, nbytes)
        self._response()
        chunk = memoryview(bytes(min(CHUNK, nbytes)))
        remaining = nbytes
        while remaining:
            n = min(len(chunk), remaining)
            self.sock.sendall(chunk[:n])
            remaining -= n
        _, _, _, elapsed_us = self._response()
        return nbytes / 1e6 / max(elapsed_us / 1e6, 1e-6)

    def speed_download(self, nbytes):
        """Receives nbytes of zeros from the PS5; returns MB/s."""
        self._request(CMD_SOURCE, "", 0, nbytes)
        _, _, remaining, _ = self._response()
        buf = memoryview(bytearray(CHUNK))
        start = time.perf_counter()
        while remaining:
            k = self.sock.recv_into(buf, min(CHUNK, remaining))
            if not k:
                raise ConnectionError("speed test cut short")
            remaining -= k
        return nbytes / 1e6 / max(time.perf_counter() - start, 1e-6)

    def speed_test(self, seconds=3.0):
        """Measures the raw cable link both ways without touching storage.
        A short probe sizes the real run to take about `seconds` each way."""
        out = {}
        for name, fn in (("up_mbs", self.speed_upload), ("down_mbs", self.speed_download)):
            rate = fn(8 * 1024 * 1024)
            size = int(min(max(rate * 1e6 * seconds, 16e6), 2e9))
            out[name] = round(fn(size), 1)
        return out

    def read_small(self, remote, limit=1024 * 1024):
        """Returns the contents of a small PS5 file (e.g. a config file)."""
        st = self.stat(remote)
        if st is None or st[0] != KIND_FILE:
            raise RemoteError(f"{remote}: not a file")
        if st[1] > limit:
            raise ValueError(f"{remote} is larger than {limit} bytes")
        self._request(CMD_GET, remote, 0, 0)
        _, _, n, _ = self._response()
        return self._recv_exact(n)

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


def local_ip_for(host):
    """The PC address the OS would use to reach `host` (no packets sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _tcp_state(host, port, timeout):
    """'open', 'refused' (host is up, nothing listening) or 'silent'."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "silent"


def probe(host, port=DEFAULT_PORT, loader_port=ELFLDR_PORT, timeout=1.0):
    """Works out how far along the cable link is.

    state is one of:
      connected  the ps5cable payload answered (banner/free/total filled in)
      loader     the PS5 is on the cable with its ELF loader up; send the payload
      reachable  something answers at `host`, but neither port is open
      offline    nothing answers; `same_subnet` tells whether the PC even has
                 an address on the cable's /24
    """
    out = {"host": host, "port": port, "state": "offline", "banner": None,
           "free": None, "total": None, "local_ip": local_ip_for(host)}
    out["same_subnet"] = bool(out["local_ip"]) and \
        out["local_ip"].rsplit(".", 1)[0] == host.rsplit(".", 1)[0]
    try:
        with Connection(host, port, timeout=timeout) as c:
            c.sock.settimeout(timeout * 3)
            _, out["banner"] = c.ping()
            try:
                out["free"], out["total"] = c.df("/data")
            except RemoteError:
                pass
        out["state"] = "connected"
        return out
    except ConnectionRefusedError:
        out["state"] = "reachable"
    except (OSError, ConnectionError):
        pass
    loader = _tcp_state(host, loader_port, timeout)
    if loader == "open":
        out["state"] = "loader"
    elif loader == "refused":
        out["state"] = "reachable"
    return out


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


STATUS_TEXT = {
    "connected": "PS5 CONNECTED",
    "loader": "PS5 found on the cable, payload not running (run: send-payload)",
    "reachable": "PS5 answers on the cable, but neither the payload nor an ELF loader "
                 "is listening (run the jailbreak first)",
    "offline": "No PS5 on the cable",
}


def cmd_status(args):
    host = args.host or os.environ.get("PS5_HOST") or DEFAULT_HOST
    st = probe(host, args.port)
    print(STATUS_TEXT[st["state"]] + (f": {st['banner']}" if st["banner"] else ""))
    print(f"  PS5 {host}, this PC {st['local_ip'] or 'no route'}")
    if st["total"]:
        print(f"  /data: {human(st['free'])} free of {human(st['total'])}")
    if st["state"] == "offline" and not st["same_subnet"]:
        print(f"  This PC has no address on {host.rsplit('.', 1)[0]}.x, so the cable "
              "adapter is unplugged or not set up (run the setup-direct-link script).")
    if st["local_ip"]:
        print_adapter(linkinfo.detect(st["local_ip"]))
    return 0 if st["state"] == "connected" else 1


def print_adapter(info):
    print(f"  Adapter: {info.get('summary') or 'unknown'}")
    print(f"  Connection: {info['connection']}"
          + (f", interface {info['interface']}" if info.get("interface") else ""))
    if info.get("usb_label"):
        print(f"  USB link: {info['usb_label']}")
    if info.get("link_mbps"):
        print(f"  Ethernet link: {info['link_mbps']} Mbps")
    print(f"  Best case: about {linkinfo.human_rate(info['expected_mbs'])} "
          f"(limited by {info['bottleneck']})")
    for note in info["notes"]:
        print(f"  - {note}")


def cmd_speedtest(args):
    host = resolve_host(args)
    with connect(args) as c:
        print(f"Measuring the cable link to {host} (about 8 seconds)...")
        try:
            res = c.speed_test()
        except RemoteError as e:
            sys.exit(f"The payload on the PS5 is too old for speed tests ({e}). "
                     "Send the new ps5cable.elf.")
    info = linkinfo.detect(local_ip_for(host))
    verdict = linkinfo.interpret_speedtest(info, res["up_mbs"], res["down_mbs"])
    print(f"  PC -> PS5: {linkinfo.human_rate(res['up_mbs'])}")
    print(f"  PS5 -> PC: {linkinfo.human_rate(res['down_mbs'])}")
    print_adapter(info)
    for note in verdict["notes"]:
        print(f"  * {note}")
    return 0


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


def cmd_cp(args):
    host = resolve_host(args)
    with connect(args) as c:
        try:
            plan, _, _ = plan_remote_copy(c, args.src, args.dest)
        except (FileNotFoundError, FileExistsError, ValueError) as e:
            sys.exit(str(e))
    progress = Progress(sum(w[2] for w in plan), len(plan))
    failures = run_remote_copy(host, args.port, plan, on_bytes=progress.add,
                               on_file=lambda item, err: progress.file_finished())
    progress.finish()
    for item, err in failures:
        print(f"FAILED {item[0]}: {err}", file=sys.stderr)
    return 1 if failures else 0


def cmd_du(args):
    with connect(args) as c:
        for p in args.paths:
            size, files, folders = c.du(p)
            print(f"{human(size):>12}  {files} files, {folders} folders  {p}")
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


class Cancelled(Exception):
    """Raised from a progress callback to abort a running transfer."""


def plan_upload(conn, sources, dest, resume=False):
    """Expands local files/folders into (local, remote, size, offset) items.

    Creates the remote directory skeleton (so empty folders survive) and, with
    `resume`, drops files that are already complete on the PS5 and continues
    partial ones. Returns (plan, skipped_count)."""
    dest = dest.rstrip("/") or "/"
    work, dirs = [], []
    for src in sources:
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(f"No such file or directory: {src}")
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

    plan = []
    for lp, rp, size in work:
        offset = 0
        if resume:
            st = conn.stat(rp)
            if st and st[0] == KIND_FILE and st[1] == size:
                continue
            if st and st[0] == KIND_FILE and st[1] < size:
                offset = st[1]
        plan.append((lp, rp, size, offset))
    for d in dirs:
        conn.mkdir(d)
    return plan, len(work) - len(plan)


def plan_download(conn, sources, dest, resume=False):
    """Expands PS5 files/folders into (remote, local, size, offset) items and
    creates the local directory skeleton. Returns (plan, skipped_count)."""
    dest = Path(dest)
    work = []
    for src in sources:
        src = src.rstrip("/") or "/"
        st = conn.stat(src)
        if st is None:
            raise FileNotFoundError(f"No such file or directory on PS5: {src}")
        name = posixpath.basename(src) or "root"
        if st[0] == KIND_DIR:
            stack = [(src, dest / name)]
            while stack:
                rdir, ldir = stack.pop()
                ldir.mkdir(parents=True, exist_ok=True)
                for ename, kind, size, _ in conn.listdir(rdir):
                    rp = remote_join(rdir, ename)
                    if kind == KIND_DIR:
                        stack.append((rp, ldir / ename))
                    elif kind == KIND_FILE:
                        work.append((rp, ldir / ename, size))
        else:
            target = dest / name if dest.is_dir() or len(sources) > 1 else dest
            target.parent.mkdir(parents=True, exist_ok=True)
            work.append((src, target, st[1]))

    plan = []
    for rp, lp, size in work:
        offset = 0
        if resume and lp.exists():
            have = lp.stat().st_size
            if have == size:
                continue
            if have < size:
                offset = have
        plan.append((rp, str(lp), size, offset))
    return plan, len(work) - len(plan)


def _free_name(conn, folder, name, is_dir):
    """`name`, or "name (copy)", "name (copy 2)"... whichever is unused."""
    stem, ext = (name, "") if is_dir else posixpath.splitext(name)
    for i in range(1, 1000):
        tag = " (copy)" if i == 1 else f" (copy {i})"
        candidate = remote_join(folder, f"{stem}{tag}{ext}")
        if conn.stat(candidate) is None:
            return candidate
    raise FileExistsError(f"Too many copies of {name} in {folder}")


def plan_remote_copy(conn, sources, dest, move=False):
    """Plans copying or moving PS5 files/folders into the PS5 folder `dest`.

    A move within one drive is a rename and happens right here. Everything else
    becomes (src, dst, size, 0) copy items, with the folder skeleton created up
    front. Copying into the source's own folder picks a "(copy)" name. Returns
    (plan, renamed_count, to_delete): a move across drives must delete the
    sources in to_delete once their copies have succeeded, because renaming
    across drives crashes the PS5."""
    dest = posixpath.normpath(dest)
    st = conn.stat(dest)
    if st is None or st[0] != KIND_DIR:
        raise FileNotFoundError(f"No such folder on PS5: {dest}")

    targets = []
    for src in sources:
        src = posixpath.normpath(src)
        st = conn.stat(src)
        if st is None:
            raise FileNotFoundError(f"No such file or directory on PS5: {src}")
        if st[0] == KIND_OTHER:
            raise ValueError(f"{src} is not a file or folder")
        if st[0] == KIND_DIR and (dest == src or dest.startswith(src.rstrip("/") + "/")):
            raise ValueError(f"Can't {'move' if move else 'copy'} {src} into itself")
        in_place = posixpath.dirname(src) == dest
        if in_place and move:
            continue
        if in_place:
            target = _free_name(conn, dest, posixpath.basename(src), st[0] == KIND_DIR)
        else:
            target = remote_join(dest, posixpath.basename(src))
            if conn.stat(target) is not None:
                raise FileExistsError(f"{target} already exists on the PS5")
        targets.append((src, target, st))

    plan, dirs, renamed, to_delete = [], [], 0, []
    for src, target, st in targets:
        if move:
            try:
                conn.rename(src, target)
                renamed += 1
                continue
            except RemoteError as e:
                if "different drives" not in str(e):
                    raise
            to_delete.append(src)
        if st[0] == KIND_FILE:
            plan.append((src, target, st[1], 0))
            continue
        stack = [(src, target)]
        while stack:
            sdir, ddir = stack.pop()
            dirs.append(ddir)
            for ename, kind, size, _ in conn.listdir(sdir):
                if kind == KIND_DIR:
                    stack.append((remote_join(sdir, ename), remote_join(ddir, ename)))
                elif kind == KIND_FILE:
                    plan.append((remote_join(sdir, ename), remote_join(ddir, ename), size, 0))
    for d in dirs:
        conn.mkdir(d)
    return plan, renamed, to_delete


def run_remote_copy(host, port, plan, on_bytes=None, on_file=None, cancel=None):
    """Runs a plan from plan_remote_copy on the console, one file at a time
    (both ends are the PS5's own drives, so parallel copies only compete).
    A cancelled copy removes its half-written file. Returns [(item, error)]."""
    failures = []

    def progress(n):
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled")
        if on_bytes:
            on_bytes(n)

    with Connection(host, port) as conn:
        for item in plan:
            src, dst, _, off = item
            err = None
            try:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled")
                conn.copy_file(src, dst, off, progress)
            except Cancelled as e:
                err = e
                try:
                    conn.delete(dst)
                except RemoteError:
                    pass
            except RemoteError as e:
                err = e
                failures.append((item, e))
            finally:
                if on_file:
                    on_file(item, err)
            if isinstance(err, Cancelled):
                break
    return failures


def run_transfers(host, port, plan, upload, jobs=4, on_bytes=None,
                  on_file=None, cancel=None):
    """Runs a plan from plan_upload/plan_download on `jobs` parallel
    connections, largest files first.

    on_bytes(n) is called as data moves; on_file(item, error_or_None) after
    each file. Setting the `cancel` threading.Event stops all workers within
    one chunk. Returns [(item, exception)] for the files that failed."""
    local = threading.local()
    conns, conns_lock = [], threading.Lock()
    plan = sorted(plan, key=lambda w: w[2], reverse=True)

    def progress(n):
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled")
        if on_bytes:
            on_bytes(n)

    def task(item):
        err = None
        try:
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled")
            if getattr(local, "conn", None) is None:
                local.conn = Connection(host, port)
                with conns_lock:
                    conns.append(local.conn)
            src, dst, _, off = item
            try:
                if upload:
                    local.conn.put_file(src, dst, off, progress)
                else:
                    local.conn.get_file(src, dst, off, progress)
            except Exception:
                # A failure mid-stream leaves unread bytes on the socket, so
                # the connection cannot be reused for the next file.
                local.conn.close()
                local.conn = None
                raise
        except Exception as e:
            err = e
            raise
        finally:
            if on_file:
                on_file(item, err)

    failures = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = {pool.submit(task, item): item for item in plan}
            for fut in as_completed(futures):
                err = fut.exception()
                if err:
                    failures.append((futures[fut], err))
    finally:
        for c in conns:
            c.close()
    return failures


def _cli_transfer(args, upload, sources, dest):
    host = resolve_host(args)
    with Connection(host, args.port) as c:
        try:
            if upload:
                plan, skipped = plan_upload(c, sources, dest, args.resume)
            else:
                plan, skipped = plan_download(c, sources, dest, args.resume)
        except FileNotFoundError as e:
            sys.exit(str(e))

    total = sum(size - off for _, _, size, off in plan)
    verb, prep = ("Uploading", "to") if upload else ("Downloading", "from")
    where = dest if upload else ", ".join(sources)
    print(f"{verb} {len(plan)} files ({human(total)}) {prep} {host}:{where}"
          + (f", skipping {skipped} already complete" if skipped else ""))
    progress = Progress(total, len(plan))
    failures = run_transfers(host, args.port, plan, upload, args.jobs,
                             on_bytes=progress.add,
                             on_file=lambda item, err: progress.file_finished())
    progress.finish()
    for item, err in failures:
        print(f"FAILED {item[0]}: {err}", file=sys.stderr)
    return 1 if failures else 0


def cmd_put(args):
    return _cli_transfer(args, True, args.local, args.remote)


def cmd_get(args):
    return _cli_transfer(args, False, [args.remote], args.local)


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

    s = sub.add_parser("status", help="show whether the PS5 is connected over the cable")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("speedtest", help="measure the cable link speed both ways")
    s.set_defaults(func=cmd_speedtest)

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

    s = sub.add_parser("cp", help="copy files/folders into a folder on the PS5 "
                                  "(the data stays on the console)")
    s.add_argument("src", nargs="+")
    s.add_argument("dest", help="destination folder on the PS5")
    s.set_defaults(func=cmd_cp)

    s = sub.add_parser("du", help="total size of PS5 files/folders")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_du)

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

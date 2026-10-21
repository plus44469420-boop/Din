"""Runs the desktop payload behind a fake USB cable, so tests exercise the
same USB link (payload/usblink.c <-> pc/usblink.py) a real bridge cable uses."""

import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

import usblink  # noqa: E402

HOST_SERVER = ROOT / "payload" / "ps5cable-host"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_payload():
    subprocess.run(["make", "-s", "host"], cwd=ROOT / "payload", check=True)


def start_payload(cable_port, server_port=None):
    """The desktop payload, plugged into the fake cable at `cable_port`."""
    return subprocess.Popen(
        [str(HOST_SERVER), "-q", "-p", str(server_port or free_port()),
         "-C", f"127.0.0.1:{cable_port}"], stdout=subprocess.DEVNULL)


class FakePS5:
    """A payload process and a PC-side Tunnel joined by a fake cable."""

    def __init__(self):
        self.cable_port = free_port()
        self.backend = usblink.FakeCableBackend(f"127.0.0.1:{self.cable_port}")
        self.proc = start_payload(self.cable_port)
        self.tunnel = usblink.Tunnel(self.backend).start()
        if not self.tunnel.wait(10):
            self.close()
            raise RuntimeError(f"fake PS5 did not connect: {self.tunnel.detail}")

    @property
    def port(self):
        return self.tunnel.port

    def restart_payload(self):
        self.kill_payload()
        self.proc = start_payload(self.cable_port)

    def kill_payload(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def close(self):
        self.kill_payload()
        self.tunnel.stop()

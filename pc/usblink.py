"""USB-C link to the ps5cable payload over a USB bridge cable.

A PC and a PS5 are both USB hosts, so a plain USB-C cable between them does
nothing. A bridge ("data link" / "transfer") cable has a chip in the middle,
such as Prolific's PL27A1 (USB 3) or PL25A1 (USB 2), that shows up as a USB
device on both ends and passes bulk transfers from one host to the other.

The Tunnel here opens the bridge with pyusb, talks to the payload's USB link
(payload/usblink.c) and exposes it as a TCP listener on 127.0.0.1. Every
connection to that listener becomes one channel on the cable, so the
ps5cable protocol client works over USB without knowing it.

Every bulk transfer is one frame (little-endian):

    magic "PU", type u8, pad u8, chan u16, reserved u16, len u32, data, pad

`pad` (0 or 1 bytes) keeps the transfer from being a multiple of 512 so it
always ends in a short packet and arrives as one read on the other side.
"""

import errno
import os
import select
import socket
import struct
import sys
import threading
import time

VERSION = 1
FRAME = struct.Struct("<2sBBHHI")
MAX_DATA = 256 * 1024
# Bulk reads must be a whole number of max-size packets or a full frame
# overflows the transfer.
READ_BUF = MAX_DATA + 1024
HELLO, OPEN, DATA, CLOSE, PING = 1, 2, 3, 4, 5

SEND_TIMEOUT_MS = 10000
HELLO_EVERY = 1.0
PING_EVERY = 2.0
LINK_TIMEOUT = 6.0

# vendor, product -> chip name, nominal USB speed in Mbps (payload/usblink.h).
BRIDGES = {
    (0x067B, 0x27A1): ("Prolific PL27A1", 5000),
    (0x067B, 0x25A1): ("Prolific PL25A1", 480),
}
# Prolific "QuickLink" reset: flush both directions, mark this side present.
PL_SET_FEATURE = 3
PL_RESET = (1 << 7) | (1 << 4) | (1 << 3) | (1 << 0)

USB_SPEED_MBPS = {1: 1.5, 2: 12, 3: 480, 4: 5000, 5: 10000}
USB_SPEED_LABEL = {480: "USB 2.0 Hi-Speed (480 Mbps)",
                   5000: "USB 3.2 Gen 1 (5 Gbps)",
                   10000: "USB 3.2 Gen 2 (10 Gbps)",
                   12: "USB 1.1 Full Speed (12 Mbps)", 1.5: "USB 1.0 Low Speed"}
# What a bridge chip actually moves at each bus speed, in MB/s.
USB_PRACTICAL_MBS = {480: 35.0, 5000: 300.0, 10000: 300.0, 12: 1.0, 1.5: 0.1}


def encode(kind, chan=0, data=b""):
    total = FRAME.size + len(data)
    pad = 1 if total % 512 == 0 else 0
    return FRAME.pack(b"PU", kind, pad, chan, 0, len(data)) + bytes(data) + b"\0" * pad


def decode(buf):
    """(kind, chan, data) for a well-formed frame, else None."""
    if len(buf) < FRAME.size:
        return None
    magic, kind, _, chan, _, n = FRAME.unpack_from(buf)
    if magic != b"PU" or n > len(buf) - FRAME.size:
        return None
    return kind, chan, bytes(buf[FRAME.size:FRAME.size + n])


def human_rate(mb_s):
    return f"{mb_s:.0f} MB/s" if mb_s >= 10 else f"{mb_s:.1f} MB/s"


class NoDevice(Exception):
    """No bridge cable is plugged into this PC."""


class NoAccess(Exception):
    """A bridge cable is there but cannot be opened (driver or permissions)."""


class NoBackend(Exception):
    """pyusb or libusb is missing."""


# --- transports -------------------------------------------------------------

def _access_hint():
    if os.name == "nt":
        return ("Windows needs the WinUSB driver on the cable: run Zadig, pick the "
                "Prolific device and install WinUSB (see the README).")
    if sys.platform == "darwin":
        return "Unplug the cable, close any transfer app that came with it, and plug it back in."
    return ("No permission to open the cable. Install pc/60-ps5cable.rules into "
            "/etc/udev/rules.d/ and replug it, or run as root.")


class PyUsbBackend:
    """Opens a real bridge cable with pyusb (libusb 1.0 underneath)."""

    def __init__(self):
        self._usb = None

    def _load(self):
        if self._usb:
            return self._usb
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise NoBackend("pyusb is not installed. Run: pip install pyusb libusb-package")
        backend = None
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except ImportError:
            pass
        self._usb = (usb.core, usb.util, backend)
        return self._usb

    def open(self):
        core, util, backend = self._load()
        try:
            devs = list(core.find(find_all=True, backend=backend,
                                  custom_match=lambda d: (d.idVendor, d.idProduct) in BRIDGES))
        except core.NoBackendError:
            raise NoBackend("libusb was not found. Run: pip install libusb-package")
        if not devs:
            raise NoDevice("No USB bridge cable found.")
        dev = devs[0]
        chip, chip_mbps = BRIDGES[(dev.idVendor, dev.idProduct)]
        try:
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except (NotImplementedError, core.USBError):
                pass
            try:
                cfg = dev.get_active_configuration()
            except core.USBError:
                dev.set_configuration()
                cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]
            util.claim_interface(dev, 0)
        except (core.USBError, NotImplementedError) as e:
            util.dispose_resources(dev)
            raise NoAccess(f"Found a {chip} cable but could not open it ({e}). {_access_hint()}")

        def bulk(direction):
            return util.find_descriptor(
                intf, custom_match=lambda ep: util.endpoint_direction(ep.bEndpointAddress) == direction
                and util.endpoint_type(ep.bmAttributes) == util.ENDPOINT_TYPE_BULK)

        ep_in, ep_out = bulk(util.ENDPOINT_IN), bulk(util.ENDPOINT_OUT)
        if ep_in is None or ep_out is None:
            util.dispose_resources(dev)
            raise NoAccess(f"The {chip} cable has no bulk endpoints; it may be in its "
                           "mass-storage (\"Easy Transfer\") mode. Replug it.")
        try:
            dev.ctrl_transfer(0xC0, PL_SET_FEATURE, PL_RESET, 0, 0, timeout=1000)
        except core.USBError:
            pass
        info = {"chip": chip, "chip_mbps": chip_mbps,
                "vendor_id": f"{dev.idVendor:04x}", "product_id": f"{dev.idProduct:04x}",
                "usb_mbps": USB_SPEED_MBPS.get(getattr(dev, "speed", None))}
        return PyUsbTransport(core, util, dev, ep_in, ep_out, info)


class PyUsbTransport:
    def __init__(self, core, util, dev, ep_in, ep_out, info):
        import array
        self.core, self.util, self.dev = core, util, dev
        self.ep_in, self.ep_out = ep_in.bEndpointAddress, ep_out.bEndpointAddress
        self.info = info
        self.buf = array.array("B", bytes(READ_BUF))

    def _is_timeout(self, e):
        return isinstance(e, getattr(self.core, "USBTimeoutError", ())) or \
            e.errno == errno.ETIMEDOUT or getattr(e, "backend_error_code", None) == -7

    def recv(self, timeout_ms):
        """One bulk transfer, None on timeout. ConnectionError when unplugged."""
        try:
            n = self.dev.read(self.ep_in, self.buf, timeout_ms)
        except self.core.USBError as e:
            if self._is_timeout(e):
                return None
            raise ConnectionError(f"USB read failed: {e}")
        return memoryview(self.buf)[:n]

    def send(self, data, timeout_ms):
        try:
            n = self.dev.write(self.ep_out, data, timeout_ms)
        except self.core.USBError as e:
            if self._is_timeout(e):
                raise TimeoutError("the PS5 is not reading from the cable")
            raise ConnectionError(f"USB write failed: {e}")
        if n != len(data):
            raise ConnectionError("short USB write")

    def close(self):
        try:
            self.util.release_interface(self.dev, 0)
        except Exception:
            pass
        self.util.dispose_resources(self.dev)


class FakeCableBackend:
    """Test stand-in for the cable: listens at host:port for the desktop
    payload (ps5cable-host -C host:port), and carries each bulk transfer as a
    u32 length plus the bytes (payload/usb-host.c)."""

    def __init__(self, addr):
        host, port = addr.rsplit(":", 1)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, int(port)))
        self.srv.listen(1)

    def open(self):
        r, _, _ = select.select([self.srv], [], [], 1.0)
        if not r:
            raise NoDevice("No USB bridge cable found.")
        sock, _ = self.srv.accept()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return FakeCableTransport(sock)

    def close(self):
        self.srv.close()


class FakeCableTransport:
    LEN = struct.Struct("<I")

    def __init__(self, sock):
        self.sock = sock
        self.info = {"chip": "Fake cable (test)", "chip_mbps": None,
                     "vendor_id": None, "product_id": None, "usb_mbps": None}

    def _exact(self, n):
        buf = bytearray(n)
        view, got = memoryview(buf), 0
        while got < n:
            k = self.sock.recv_into(view[got:])
            if not k:
                raise ConnectionError("fake cable unplugged")
            got += k
        return buf

    def recv(self, timeout_ms):
        r, _, _ = select.select([self.sock], [], [], timeout_ms / 1000)
        if not r:
            return None
        try:
            (n,) = self.LEN.unpack(self._exact(4))
            return memoryview(self._exact(n))
        except OSError as e:
            raise ConnectionError(str(e))

    def send(self, data, timeout_ms):
        try:
            self.sock.sendall(self.LEN.pack(len(data)) + bytes(data))
        except OSError as e:
            raise ConnectionError(str(e))

    def close(self):
        self.sock.close()


def default_backend():
    fake = os.environ.get("PS5CABLE_FAKE_CABLE")
    return FakeCableBackend(fake) if fake else PyUsbBackend()


# --- link assessment --------------------------------------------------------

def assess(info):
    """Adds expected_mbs, bottleneck, limits, level and notes for the UI."""
    info = dict(info or {})
    chip, chip_mbps, usb = info.get("chip"), info.get("chip_mbps"), info.get("usb_mbps")
    info["connection"] = "USB-C bridge cable"
    if usb:
        info["usb_label"] = USB_SPEED_LABEL.get(usb, f"{usb} Mbps")
    limits = []
    if chip_mbps:
        limits.append((f"{chip} bridge chip ({'USB 3' if chip_mbps > 480 else 'USB 2.0'})",
                       USB_PRACTICAL_MBS.get(chip_mbps, chip_mbps / 8 * 0.5)))
    if usb:
        limits.append((f"PC USB port ({info['usb_label'].split(' (')[0]})",
                       USB_PRACTICAL_MBS.get(usb, usb / 8 * 0.5)))
    notes, level = [], "ok" if chip_mbps else "unknown"
    if limits:
        name, best = min(limits, key=lambda x: x[1])
        info["expected_mbs"], info["bottleneck"] = round(best, 1), name
    else:
        info["expected_mbs"], info["bottleneck"] = None, None
    info["limits"] = [{"name": n, "mbs": round(v, 1)} for n, v in limits]

    if chip_mbps and chip_mbps <= 480:
        level = "warn"
        notes.append(f"The {chip} is a USB 2.0 bridge, about 35 MB/s at best. A PL27A1 "
                     "(USB 3) bridge cable is roughly 8x faster.")
    elif usb and usb <= 480:
        level = "warn"
        notes.append("This PC sees the cable at USB 2.0 speed, about 35 MB/s. Plug it "
                     "straight into a USB 3 / USB-C port with data (not a hub or a "
                     "USB 2 port).")
    elif chip_mbps:
        notes.insert(0, f"USB 3 bridge: up to about {human_rate(info['expected_mbs'])}, "
                        f"roughly {info['expected_mbs'] * 60 / 1024:.0f} GB per minute.")
    if chip_mbps:
        notes.append("The PS5 end must be in a SuperSpeed port too (the rear USB-A ports, "
                     "or the front USB-C port); the original PS5's front USB-A port is "
                     "USB 2.0 only. Run a speed test to check both ends.")
    info["level"] = level
    info["notes"] = notes
    parts = [chip, info.get("usb_label", "").split(" (")[0]]
    info["summary"] = " · ".join(p for p in parts if p) or "USB-C bridge cable"
    return info


def interpret_speedtest(info, up_mbs, down_mbs):
    """Compares a measured speed with what the cable should manage."""
    measured = min(x for x in (up_mbs, down_mbs) if x)
    expected = info.get("expected_mbs")
    notes = []
    if not expected or measured >= expected * 0.6:
        verdict = "ok"
        notes.append(f"Measured {human_rate(measured)}: the USB link is performing as expected.")
    else:
        verdict = "warn"
        notes.append(f"Measured {human_rate(measured)}, expected about {human_rate(expected)}.")
        if 20 <= measured <= 45:
            notes.append("That matches USB 2.0. The PS5 end is probably in a USB 2.0 port "
                         "(the original PS5's front USB-A) or there is a hub in between.")
        else:
            notes.append("Possible causes: a hub or dock in between, a long passive "
                         "extension, or the PC busy with something else.")
    return {"up_mbs": up_mbs, "down_mbs": down_mbs, "verdict": verdict, "notes": notes}


# --- tunnel -----------------------------------------------------------------

STATE_TEXT = {
    "starting": "Looking for the USB bridge cable...",
    "no_backend": "USB support is missing.",
    "no_cable": "No USB bridge cable found. Plug a USB-C bridge (data link) cable "
                "between this PC and the PS5.",
    "no_access": "The USB bridge cable cannot be opened.",
    "waiting": "Bridge cable found. Waiting for the ps5cable payload on the PS5; "
               "load ps5cable.elf with the jailbreak.",
    "connected": "PS5 connected over USB.",
}


class _Chan:
    def __init__(self, cid, sock):
        self.id = cid
        self.sock = sock
        self.peer_closed = False


class Tunnel:
    """Keeps the cable open and relays 127.0.0.1:`port` connections over it."""

    def __init__(self, backend=None, listen_port=0):
        self.backend = backend or default_backend()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", listen_port))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]
        self.state, self.detail = "starting", STATE_TEXT["starting"]
        self.banner = None
        self.info = {}
        self.max_data = MAX_DATA
        self.tr = None
        self.wlock = threading.Lock()
        self.clock = threading.Lock()
        self.chans = {}
        self.next_id = 1
        self.changed = threading.Condition()
        self.started = False
        self.stopped = False

    # -- public --

    def start(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self._run, daemon=True, name="usb-link").start()
            threading.Thread(target=self._accept, daemon=True, name="usb-accept").start()
        return self

    def stop(self):
        self.stopped = True
        self.srv.close()
        tr = self.tr
        if tr:
            tr.close()
        if hasattr(self.backend, "close"):
            self.backend.close()

    @property
    def connected(self):
        return self.state == "connected"

    def wait(self, timeout, states=("connected",)):
        """Waits until the state is one of `states`; True if it got there."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while self.state not in states:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.changed.wait(left)
        return True

    def status(self):
        return {"state": self.state, "connected": self.connected, "detail": self.detail,
                "banner": self.banner, "link": self.link_info()}

    def link_info(self):
        return assess(self.info) if self.info else None

    # -- internals --

    def _set(self, state, detail=None):
        with self.changed:
            self.state, self.detail = state, detail or STATE_TEXT[state]
            self.changed.notify_all()

    def _send(self, kind, chan=0, data=b"", timeout_ms=SEND_TIMEOUT_MS):
        tr = self.tr
        if tr is None:
            raise ConnectionError("USB link is down")
        frame = encode(kind, chan, data)
        with self.wlock:
            tr.send(frame, timeout_ms)

    def _run(self):
        while not self.stopped:
            try:
                tr = self.backend.open()
            except NoDevice:
                self.info = {}
                self._set("no_cable")
                if not isinstance(self.backend, FakeCableBackend):
                    time.sleep(1)
                continue
            except NoAccess as e:
                self._set("no_access", str(e))
                time.sleep(2)
                continue
            except NoBackend as e:
                self._set("no_backend", str(e))
                time.sleep(5)
                continue
            except OSError as e:
                if self.stopped:
                    return
                self._set("no_cable", f"{STATE_TEXT['no_cable']} ({e})")
                time.sleep(1)
                continue
            self.info = tr.info
            self.tr = tr
            try:
                self._session(tr)
            except (ConnectionError, OSError):
                pass
            finally:
                self.tr = None
                self._drop_all()
                self.banner = None
                tr.close()
            if not self.stopped:
                self._set("no_cable")

    def _session(self, tr):
        self._set("waiting")
        connected = False
        last_hello = last_ping = 0.0
        last_rx = time.monotonic()
        while not self.stopped:
            now = time.monotonic()
            if not connected and now - last_hello >= HELLO_EVERY:
                last_hello = now
                try:
                    self._send(HELLO, 0, struct.pack("<II", VERSION, MAX_DATA), 1000)
                except TimeoutError:
                    pass
            if connected and now - last_ping >= PING_EVERY:
                last_ping = now
                try:
                    self._send(PING, 0, b"", 1000)
                except TimeoutError:
                    pass
            if connected and now - last_rx > LINK_TIMEOUT:
                connected = False
                self.banner = None
                self._drop_all()
                self._set("waiting", "The PS5 stopped answering over USB. Waiting for "
                                     "the ps5cable payload.")
            buf = tr.recv(500)
            if buf is None:
                continue
            frame = decode(buf)
            if frame is None:
                continue
            last_rx = time.monotonic()
            kind, chan, data = frame
            if kind == HELLO:
                if not connected and len(data) >= 8:
                    _, peer_max = struct.unpack_from("<II", data)
                    self.max_data = max(4096, min(MAX_DATA, peer_max))
                    self.banner = data[8:].decode("utf-8", "replace")
                    connected = True
                    last_ping = time.monotonic()
                    self._set("connected", f"{STATE_TEXT['connected']} {self.banner}")
            elif kind == DATA:
                ch = self.chans.get(chan)
                if ch:
                    try:
                        ch.sock.sendall(data)
                    except OSError:
                        self._shut(ch)
            elif kind == CLOSE:
                ch = self.chans.get(chan)
                if ch:
                    ch.peer_closed = True
                    self._shut(ch)

    @staticmethod
    def _shut(ch):
        try:
            ch.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _drop_all(self):
        with self.clock:
            chans = list(self.chans.values())
        for ch in chans:
            ch.peer_closed = True
            self._shut(ch)

    def _accept(self):
        while not self.stopped:
            try:
                sock, _ = self.srv.accept()
            except OSError:
                if self.stopped:
                    return
                continue
            if not self.connected:
                sock.close()
                continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Bounds how long a client that stops reading can stall the cable.
            sock.settimeout(60)
            with self.clock:
                for _ in range(65535):
                    cid, self.next_id = self.next_id, self.next_id % 65535 + 1
                    if cid not in self.chans:
                        break
                ch = self.chans[cid] = _Chan(cid, sock)
            try:
                self._send(OPEN, cid)
            except (ConnectionError, TimeoutError, OSError):
                with self.clock:
                    self.chans.pop(cid, None)
                sock.close()
                continue
            threading.Thread(target=self._pump, args=(ch,), daemon=True).start()

    def _pump(self, ch):
        """Local socket -> cable. The only place a channel's socket is closed."""
        try:
            while True:
                try:
                    data = ch.sock.recv(self.max_data)
                except socket.timeout:
                    continue
                if not data:
                    break
                self._send(DATA, ch.id, data)
        except (ConnectionError, TimeoutError, OSError):
            pass
        finally:
            with self.clock:
                self.chans.pop(ch.id, None)
            if not ch.peer_closed:
                try:
                    self._send(CLOSE, ch.id, b"", 1000)
                except (ConnectionError, TimeoutError, OSError):
                    pass
            ch.sock.close()


_shared = None
_shared_lock = threading.Lock()


def shared_tunnel():
    """One tunnel per process: only one program can hold the cable."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Tunnel().start()
        return _shared

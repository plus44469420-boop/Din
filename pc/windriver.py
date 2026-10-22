"""Puts Windows' WinUSB driver on the USB bridge cable, as Zadig does.

libusb can only open a device on Windows when it uses WinUSB, and a bridge
cable comes up with no driver or with its maker's "easy transfer" driver. The
INF written here only points the cable's hardware IDs at Windows' own
winusb.sys, but Windows still wants it in a signed catalog. Like libwdi (the
library inside Zadig) this signs the catalog with a certificate made on this
PC, trusts that certificate, and deletes its private key straight after
signing so nothing else can ever be signed with it.

Installing needs administrator rights: the app starts itself elevated with
--install-driver (one UAC prompt) and reads the result from a JSON file.
"""

import base64
import ctypes
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import usblink

INF_NAME = "ps5cable.inf"
CAT_NAME = "ps5cable.cat"
CERT_SUBJECT = "CN=PS5 Cable USB driver (made on this PC)"
INTERFACE_GUID = "{6E1F6C0B-3C5D-4E0B-9F4E-50533543424C}"
USB_DEVICE_CLASS = "{88BAE032-5A81-49f0-BC3D-A4FF138216D6}"
INSTALLFLAG_FORCE = 0x1
ERROR_NO_SUCH_DEVINST = 0xE000020B
ERROR_CANCELLED = 1223
CREATE_NO_WINDOW = 0x08000000


class DriverError(Exception):
    pass


def supported():
    return os.name == "nt"


_HWID = re.compile(r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})(?:&MI_(\d+))?", re.I)


def parse_hwid(hwid):
    """(vid, pid, interface or None) for a USB hardware id, else None."""
    match = _HWID.match(hwid or "")
    if not match:
        return None
    vid, pid, mi = int(match.group(1), 16), int(match.group(2), 16), match.group(3)
    return vid, pid, (int(mi) if mi is not None else None)


def bridge_nodes(devices):
    """The plugged-in devices that are one of the bridge chips."""
    out = []
    for dev in devices:
        for hwid in dev.get("hwids") or []:
            parsed = parse_hwid(hwid)
            if parsed and (parsed[0], parsed[1]) in usblink.BRIDGES:
                out.append({**dev, "vid": parsed[0], "pid": parsed[1], "mi": parsed[2], "hwid": hwid})
                break
    return out


def default_hwids():
    """The bridge interface only. Matching the whole device replaces Windows'
    composite driver and the cable stops enumerating."""
    return [f"USB\\VID_{vid:04X}&PID_{pid:04X}&MI_00" for vid, pid in sorted(usblink.BRIDGES)]


def inf_hwids(devices):
    """WinUSB goes on the bridge interface (MI_00) only. Matching the whole
    device replaces usbccgp, and Windows then stops a USB-C cable."""
    pids = sorted({n["pid"] for n in bridge_nodes(devices)})
    if not pids:
        return default_hwids()
    return [f"USB\\VID_067B&PID_{pid:04X}&MI_00" for pid in pids]


def bridge_problem(devices):
    """Why Windows can see the cable but this app cannot, or None."""
    nodes = bridge_nodes(devices)
    if not nodes:
        return None
    chip = usblink.BRIDGES[(nodes[0]["vid"], nodes[0]["pid"])][0]
    stopped = [n for n in nodes if n.get("problem")]
    if stopped:
        return (f"Windows sees the USB cable ({chip}) but stopped its driver "
                f"(problem {stopped[0]['problem']}). Click Install driver to repair it, "
                "then unplug the cable and plug it back in.")
    parents = [n for n in nodes if n["mi"] is None]
    bridge = [n for n in nodes if n["mi"] == 0]
    if any((n.get("service") or "").upper() == "WINUSB" for n in parents):
        return ("Windows sees the USB cable, but the driver was installed on the whole "
                "device and that stops a USB-C cable. Click Install driver to repair it, "
                "then unplug the cable and plug it back in.")
    if bridge and any((n.get("service") or "").upper() not in ("WINUSB", "LIBUSBK", "LIBUSB0")
                      for n in bridge):
        return ("Windows sees the USB cable, but the bridge part of it has no driver. "
                "Click Install driver.")
    if parents and not bridge and (parents[0].get("service") or "").upper() in ("USBCCGP", ""):
        return ("Windows sees the USB cable but not the bridge part yet. Unplug it, "
                "plug it back in, then click Install driver.")
    return None


def supplement(rows, devices):
    """Adds bridge cables Windows sees but libusb doesn't (no WinUSB driver yet)."""
    have = {row["id"] for row in rows if row.get("bridge")}
    extra = []
    for node in bridge_nodes(devices):
        ident = f"{node['vid']:04x}:{node['pid']:04x}"
        if ident in have:
            continue
        have.add(ident)
        kind = "USB bridge cable (works with PS5 Cable)"
        if node.get("problem"):
            kind += f" · driver stopped (problem {node['problem']})"
        elif node["mi"] is None and (node.get("service") or "").upper() == "WINUSB":
            kind += " · driver is on the wrong part of the cable"
        extra.append({"id": ident, "name": node.get("name") or usblink.BRIDGES[(node["vid"], node["pid"])][0],
                      "kind": kind, "bridge": True, "speed": None})
    return extra + rows


def make_inf(date=None, hwids=None):
    date = date or time.strftime("%m/%d/%Y")
    hwids = list(hwids or default_hwids())
    models, tokens = [], []
    for hwid in hwids:
        vid, pid, _ = parse_hwid(hwid)
        token = usblink.BRIDGES[(vid, pid)][0].split()[-1]
        models.append(f"%{token}% = Install, {hwid}")
        if token not in tokens:
            tokens.append((token, usblink.BRIDGES[(vid, pid)][0]))
    strings = [f'{token} = "PS5 Cable: {name} USB bridge"' for token, name in tokens]
    sections = []
    for arch in ("NTamd64", "NTx86", "NTarm64"):
        sections += [f"[Cable.{arch}]", *models, ""]
    return "\r\n".join([
        "[Version]",
        'Signature   = "$Windows NT$"',
        "Class       = USBDevice",
        f"ClassGUID   = {USB_DEVICE_CLASS}",
        "Provider    = %Provider%",
        f"CatalogFile = {CAT_NAME}",
        f"DriverVer   = {date},1.0.0.0",
        "",
        "[Manufacturer]",
        "%Provider% = Cable,NTamd64,NTx86,NTarm64",
        "",
        *sections,
        "[Install]",
        "Include = winusb.inf",
        "Needs   = WINUSB.NT",
        "",
        "[Install.Services]",
        "Include = winusb.inf",
        "Needs   = WINUSB.NT.Services",
        "",
        "[Install.HW]",
        "AddReg = Install_AddReg",
        "",
        "[Install_AddReg]",
        f'HKR,,DeviceInterfaceGUIDs,0x10000,"{INTERFACE_GUID}"',
        "",
        "[Strings]",
        'Provider = "PS5 Cable"',
        *strings,
        "",
    ])


# Runs elevated. The certificate goes into the machine's Root and
# TrustedPublisher stores (public part only) so Windows accepts the catalog
# without asking; the private key is deleted as soon as the catalog is signed.
SIGN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Security, PKI
$dir = '__DIR__'
$cat = Join-Path $dir '__CAT__'
$cer = Join-Path $dir 'ps5cable.cer'
New-FileCatalog -Path (Join-Path $dir '__INF__') -CatalogFilePath $cat -CatalogVersion 2.0 | Out-Null
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject '__SUBJECT__' `
    -CertStoreLocation Cert:\LocalMachine\My -NotAfter (Get-Date).AddYears(30)
try {
    Export-Certificate -Cert $cert -FilePath $cer | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
    $sig = Set-AuthenticodeSignature -FilePath $cat -Certificate $cert -HashAlgorithm SHA256
    if ($sig.Status -ne 'Valid') { throw "Signing the driver catalog failed: $($sig.StatusMessage)" }
} finally {
    Remove-Item -Path ('Cert:\LocalMachine\My\' + $cert.Thumbprint) -DeleteKey -ErrorAction SilentlyContinue
}
"""


def driver_dir():
    base = os.environ.get("ProgramData") or tempfile.gettempdir()
    return Path(base) / "PS5 Cable" / "driver"


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def list_devices():
    """USB devices Windows sees, driver and all. libusb only sees WinUSB ones."""
    if not supported():
        return []
    try:
        return _list_devices()
    except Exception:
        return []


def _list_devices():
    from ctypes import wintypes as w

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class DevInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("ClassGuid", GUID),
                    ("DevInst", ctypes.c_ulong), ("Reserved", ctypes.c_void_p)]

    setup = ctypes.WinDLL("setupapi", use_last_error=True)
    get_list = setup.SetupDiGetClassDevsW
    get_list.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint]
    get_list.restype = ctypes.c_void_p
    enum = setup.SetupDiEnumDeviceInfo
    enum.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DevInfo)]
    enum.restype = w.BOOL
    prop = setup.SetupDiGetDeviceRegistryPropertyW
    prop.argtypes = [ctypes.c_void_p, ctypes.POINTER(DevInfo), ctypes.c_ulong, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    prop.restype = w.BOOL
    destroy = setup.SetupDiDestroyDeviceInfoList
    destroy.argtypes = [ctypes.c_void_p]
    status_of = ctypes.WinDLL("cfgmgr32", use_last_error=True).CM_Get_DevNode_Status
    status_of.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                          ctypes.c_ulong, ctypes.c_ulong]
    status_of.restype = ctypes.c_ulong

    info = get_list(None, "USB", None, 0x2 | 0x4)  # present, all classes
    if not info or info == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        index = 0
        while True:
            data = DevInfo()
            data.cbSize = ctypes.sizeof(DevInfo)
            if not enum(info, index, ctypes.byref(data)):
                break
            index += 1
            hwids = _reg_prop(prop, info, data, 1)  # SPDRP_HARDWAREID, multi-sz
            if not hwids:
                continue
            name = (_reg_prop(prop, info, data, 0) or [""])[0]
            service = (_reg_prop(prop, info, data, 4) or [""])[0]
            st, problem = ctypes.c_ulong(), ctypes.c_ulong()
            code = 0
            if status_of(ctypes.byref(st), ctypes.byref(problem), data.DevInst, 0) == 0 \
                    and st.value & 0x400:  # DN_HAS_PROBLEM
                code = int(problem.value)
            out.append({"name": name, "hwids": hwids, "service": service, "problem": code})
    finally:
        destroy(info)
    return out


def _reg_prop(prop, info, data, kind):
    cap = 4096
    buf = ctypes.create_unicode_buffer(cap // 2)
    got = ctypes.c_ulong()
    if not prop(info, ctypes.byref(data), kind, None, ctypes.cast(buf, ctypes.c_void_p),
                cap, ctypes.byref(got)) or not got.value:
        return []
    raw = ctypes.string_at(ctypes.addressof(buf), got.value)
    return [part for part in raw.decode("utf-16-le", "replace").split("\x00") if part]


def _system32(exe):
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / exe)


def _run(cmd, what, env=None):
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       creationflags=CREATE_NO_WINDOW, env=env)
    out = (p.stdout + p.stderr).strip()
    if p.returncode not in (0, 3010):
        raise DriverError(f"{what} failed ({p.returncode}): {out[-600:]}")
    return out


def _sign(d):
    q = lambda s: str(s).replace("'", "''")  # noqa: E731
    script = (SIGN_SCRIPT.replace("__DIR__", q(d)).replace("__CAT__", CAT_NAME)
              .replace("__INF__", INF_NAME).replace("__SUBJECT__", q(CERT_SUBJECT)))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    # Started from PowerShell 7, these point Windows PowerShell at PowerShell
    # 7's modules and command cache, which it can't load; without them it
    # uses its own.
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PSMODULEPATH", "PSMODULEANALYSISCACHEPATH")}
    _run([_system32(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive",
          "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded], "Signing the driver", env)


def _force_install(inf, hwids):
    """Puts the driver on the bridge part of a plugged-in cable."""
    from ctypes import wintypes
    fn = ctypes.WinDLL("newdev", use_last_error=True).UpdateDriverForPlugAndPlayDevicesW
    fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                   ctypes.POINTER(wintypes.BOOL)]
    fn.restype = wintypes.BOOL
    installed, reboot, errors = [], False, []
    for hwid in hwids:
        need_reboot = wintypes.BOOL(False)
        if fn(None, hwid, str(inf), INSTALLFLAG_FORCE, ctypes.byref(need_reboot)):
            installed.append(hwid)
            reboot = reboot or bool(need_reboot.value)
            continue
        err = ctypes.get_last_error() & 0xFFFFFFFF
        if err != ERROR_NO_SUCH_DEVINST:
            errors.append(f"{hwid}: {ctypes.FormatError(err).strip() or hex(err)}")
    return installed, reboot, errors


def _powershell(script, what):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PSMODULEPATH", "PSMODULEANALYSISCACHEPATH")}
    return _run([_system32(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive",
                  "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded], what, env)


def _remove_old_packages():
    """Drops driver packages this app published before. The first ones matched
    the whole cable, which makes Windows stop a USB-C bridge cable."""
    try:
        out = _powershell(
            "Get-WindowsDriver -Online | Where-Object { $_.ProviderName -eq 'PS5 Cable' } "
            "| ForEach-Object { $_.Driver }",
            "Looking for the previous driver")
    except DriverError:
        return []
    names = [line.strip() for line in out.splitlines() if line.strip().lower().endswith(".inf")]
    for name in names:
        try:
            _run([_system32("pnputil.exe"), "/delete-driver", name, "/uninstall", "/force"],
                 "Removing the previous driver")
        except DriverError:
            pass
    if names:
        subprocess.run([_system32("pnputil.exe"), "/scan-devices"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    return names


def install():
    """Signs and installs the driver. Needs administrator rights."""
    if not supported():
        raise DriverError("Only Windows needs a driver for the cable.")
    if not is_admin():
        raise DriverError("Installing a driver needs administrator rights.")
    present = list_devices()
    removed = _remove_old_packages()
    hwids = inf_hwids(present)
    d = driver_dir()
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    inf = d / INF_NAME
    inf.write_bytes(make_inf(hwids=hwids).encode("ascii"))
    _sign(d)
    _run([_system32("pnputil.exe"), "/add-driver", str(inf)], "Adding the driver to Windows")
    installed, reboot, errors = _force_install(inf, hwids)
    if errors and not installed:
        raise DriverError("Windows would not put the driver on the cable: " + "; ".join(errors))
    if removed:
        msg = ("Repaired the cable's driver. The earlier one was installed on the whole "
               "device, which makes Windows stop a USB-C cable. Unplug the cable and plug "
               "it back in.")
    elif installed:
        msg = ("USB driver installed on the bridge part of the cable. If the PS5 still "
               "doesn't show up, unplug the cable and plug it back in.")
    else:
        msg = ("USB driver added. It only applies to a bridge cable (a chip in the middle, "
               "such as the Prolific PL27A1). A normal USB-C to USB-C cable never shows up, "
               "because the PS5 and the PC are both USB hosts.")
    if reboot:
        msg += " Windows asks for a restart to finish."
    return {"ok": True, "on_cable": bool(installed), "repaired": bool(removed),
            "reboot": reboot, "message": msg}


def installer_log_path():
    base = os.environ.get("ProgramData") or tempfile.gettempdir()
    return Path(base) / "PS5 Cable" / "driver.log"


def _log_tail():
    try:
        text = installer_log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def run_elevated_child(result_path):
    """Entry point of the elevated copy of the app (--install-driver RESULT).

    Writes its pid to RESULT.started before doing anything slow, and the
    outcome to RESULT. The app that asked waits on those files: the process
    handle from the admin prompt exits before this copy does.
    """
    import faulthandler
    import traceback

    log_path = installer_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "a", encoding="utf-8")
    except OSError:
        log = None
    if log:
        log.write(f"\n--- install {time.strftime('%H:%M:%S')} ---\n")
        log.flush()
        faulthandler.enable(log)
    if result_path:
        try:
            marker = Path(str(result_path) + ".started")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as e:
            if log:
                log.write(f"could not write the start marker: {e}\n")
                log.flush()
    try:
        result = install()
        code = 0
    except Exception:
        detail = traceback.format_exc()
        if log:
            log.write(detail)
            log.flush()
        result, code = {"ok": False, "message": detail.strip().splitlines()[-1][:500]}, 1
    if result_path:
        try:
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            Path(result_path).write_text(json.dumps(result), encoding="utf-8")
        except OSError as e:
            if log:
                log.write(f"could not write the result: {e}\n")
                log.flush()
            code = 1
    if log:
        log.close()
    return code


def pid_alive(pid):
    """False when `pid` is gone. A process this user isn't allowed to open still counts as alive."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError as e:
            return e.errno == errno.EPERM
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # access denied


def wait_for_result(result_path, timeout=180, startup_grace=60, interval=0.2, alive=None):
    """Waits until the elevated installer writes `result_path`.

    The process handle from the Windows admin prompt exits as soon as the
    elevated copy has been started, so this watches the result file and the
    pid written beside it instead of that handle.
    """
    alive = alive or pid_alive
    result_path, started_path = Path(result_path), Path(str(result_path) + ".started")
    deadline = time.monotonic() + timeout
    pid = None

    def read_result():
        if not result_path.is_file() or result_path.stat().st_size == 0:
            return None
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    while time.monotonic() < deadline:
        found = read_result()
        if found is not None:
            return found
        if pid is None and started_path.is_file():
            try:
                pid = int(started_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = None
        if pid is not None and not alive(pid):
            time.sleep(0.5)
            found = read_result()
            if found is not None:
                return found
            break
        if pid is None and time.monotonic() > deadline - timeout + startup_grace:
            break
        time.sleep(interval)
    detail = _log_tail()
    extra = f" {detail}" if detail else f" The log is {installer_log_path()}."
    if pid is None:
        raise DriverError("Windows didn't start the driver installer. Click Install driver "
                          "and choose Yes on the admin prompt." + extra)
    try:
        still_running = alive(pid)
    except OSError:
        still_running = False
    if still_running:
        raise DriverError("The driver installer is taking too long. Leave this window open "
                          "and click Install driver once more." + extra)
    raise DriverError("The driver installer closed before it finished." + extra)


def _shell_execute_info():
    from ctypes import wintypes as w

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", w.HWND),
                    ("lpVerb", w.LPCWSTR), ("lpFile", w.LPCWSTR), ("lpParameters", w.LPCWSTR),
                    ("lpDirectory", w.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", w.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", w.LPCWSTR), ("hkeyClass", w.HKEY), ("dwHotKey", w.DWORD),
                    ("hIconOrMonitor", w.HANDLE), ("hProcess", w.HANDLE)]
    return ShellExecuteInfo


def windows_argv():
    """Arguments Windows actually passed to this process, without the program name.

    A windowed build (no console) started from the admin prompt can lose those
    arguments from sys.argv. The command line itself still has them. None off Windows.
    """
    if os.name != "nt":
        return None
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    argc = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(kernel32.GetCommandLineW(), ctypes.byref(argc))
    if not argv:
        return None
    try:
        return [argv[i] for i in range(1, argc.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def install_elevated(timeout=300, startup_grace=120):
    """Runs the install in an elevated copy of this app (UAC prompt) and returns its result.

    The process handle from the admin prompt exits as soon as the elevated copy
    has been created. Waiting on that handle reports failure while the installer
    is still running, so this waits on the result file the elevated copy writes.
    """
    if not supported():
        raise DriverError("Only Windows needs a driver for the cable.")
    if is_admin():
        return install()
    # ProgramData, not a temp dir: the elevated copy creates the folder, and
    # this process only reads it. Deleting a temp dir when the prompt's process
    # exits used to remove the result out from under the real installer.
    base = Path(os.environ.get("ProgramData") or tempfile.gettempdir()) / "PS5 Cable"
    result = base / f"install-{os.urandom(8).hex()}.json"
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, ["--install-driver", str(result)]
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.is_file() else sys.executable)
        args = [str(Path(__file__).resolve().with_name("hub.py")), "--install-driver", str(result)]
    info = _shell_execute_info()
    sei = info(cbSize=ctypes.sizeof(info), fMask=0, lpVerb="runas", lpFile=exe,
               lpParameters=subprocess.list2cmdline(args),
               lpDirectory=str(Path(exe).resolve().parent), nShow=1)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        if ctypes.get_last_error() == ERROR_CANCELLED:
            raise DriverError("Cancelled: Windows needs your OK (the admin prompt) to "
                              "install the driver.")
        raise DriverError(f"Could not start the installer: {ctypes.FormatError()}")
    try:
        out = wait_for_result(result, timeout=timeout, startup_grace=startup_grace)
    finally:
        for path in (result, Path(str(result) + ".started")):
            try:
                path.unlink()
            except OSError:
                pass
    if not out.get("ok"):
        raise DriverError(out.get("message") or "Installing the driver failed.")
    return out

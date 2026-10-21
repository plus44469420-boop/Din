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
import json
import os
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


def hardware_ids():
    """Every ID the cable can show up as: whole device, or interface 0 if composite."""
    ids = []
    for vid, pid in usblink.BRIDGES:
        ids += [f"USB\\VID_{vid:04X}&PID_{pid:04X}", f"USB\\VID_{vid:04X}&PID_{pid:04X}&MI_00"]
    return ids


def make_inf(date=None):
    date = date or time.strftime("%m/%d/%Y")
    models = []
    for hwid in hardware_ids():
        vid, pid = hwid[4:].split("&")[:2]
        chip = usblink.BRIDGES[(int(vid[4:], 16), int(pid[4:], 16))][0].split()[-1]
        models.append(f"%{chip}% = Install, {hwid}")
    strings = [f'{name.split()[-1]} = "PS5 Cable: {name} USB bridge"'
               for name, _ in usblink.BRIDGES.values()]
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
    _run([_system32(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive",
          "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded], "Signing the driver")


def _force_install(inf):
    """Puts the driver on every plugged-in cable, even over a driver Windows ranks higher."""
    from ctypes import wintypes
    fn = ctypes.WinDLL("newdev", use_last_error=True).UpdateDriverForPlugAndPlayDevicesW
    fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                   ctypes.POINTER(wintypes.BOOL)]
    fn.restype = wintypes.BOOL
    installed, reboot, errors = [], False, []
    for hwid in hardware_ids():
        need_reboot = wintypes.BOOL(False)
        if fn(None, hwid, str(inf), INSTALLFLAG_FORCE, ctypes.byref(need_reboot)):
            installed.append(hwid)
            reboot = reboot or bool(need_reboot.value)
            continue
        err = ctypes.get_last_error() & 0xFFFFFFFF
        if err != ERROR_NO_SUCH_DEVINST:
            errors.append(f"{hwid}: {ctypes.FormatError(err).strip() or hex(err)}")
    return installed, reboot, errors


def install():
    """Signs and installs the driver. Needs administrator rights."""
    if not supported():
        raise DriverError("Only Windows needs a driver for the cable.")
    if not is_admin():
        raise DriverError("Installing a driver needs administrator rights.")
    d = driver_dir()
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    inf = d / INF_NAME
    inf.write_bytes(make_inf().encode("ascii"))
    _sign(d)
    _run([_system32("pnputil.exe"), "/add-driver", str(inf)], "Adding the driver to Windows")
    installed, reboot, errors = _force_install(inf)
    if errors and not installed:
        raise DriverError("Windows would not put the driver on the cable: " + "; ".join(errors))
    if installed:
        msg = "USB driver installed on the cable."
    else:
        msg = ("USB driver added to Windows. It goes on the cable as soon as the cable is "
               "plugged into this PC; if it doesn't, plug the cable in and install again.")
    if reboot:
        msg += " Windows asks for a restart to finish."
    return {"ok": True, "on_cable": bool(installed), "reboot": reboot, "message": msg}


def run_elevated_child(result_path):
    """Entry point of the elevated copy of the app (--install-driver RESULT)."""
    try:
        result = install()
        code = 0
    except Exception as e:  # reported to the app that asked, not shown here
        result, code = {"ok": False, "message": str(e)}, 1
    if result_path:
        Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    return code


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


def install_elevated(timeout=300):
    """Runs the install in an elevated copy of this app (UAC prompt) and returns its result."""
    if not supported():
        raise DriverError("Only Windows needs a driver for the cable.")
    if is_admin():
        return install()
    tmp = Path(tempfile.mkdtemp(prefix="ps5cable-driver-"))
    result = tmp / "result.json"
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, ["--install-driver", str(result)]
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.is_file() else sys.executable)
        args = [str(Path(__file__).resolve().with_name("hub.py")), "--install-driver", str(result)]
    info = _shell_execute_info()
    sei = info(cbSize=ctypes.sizeof(info), fMask=0x40,  # SEE_MASK_NOCLOSEPROCESS
               lpVerb="runas", lpFile=exe, lpParameters=subprocess.list2cmdline(args), nShow=0)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(sei)):
            if ctypes.get_last_error() == ERROR_CANCELLED:
                raise DriverError("Cancelled: Windows needs your OK (the admin prompt) to "
                                  "install the driver.")
            raise DriverError(f"Could not start the installer: {ctypes.FormatError()}")
        kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000))
        kernel32.CloseHandle(sei.hProcess)
        try:
            out = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DriverError("The driver installer stopped without saying why.")
        if not out.get("ok"):
            raise DriverError(out.get("message") or "Installing the driver failed.")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

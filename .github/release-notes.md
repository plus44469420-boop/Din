Wired file transfer between a PC and a jailbroken PS5, over an Ethernet cable from a USB-C (or built-in) Ethernet port on the PC straight into the PS5's LAN port.

## Downloads

| File | For |
|---|---|
| **`ps5cable-windows.zip`** | Windows 10/11. Unzip and run **`ps5cable-hub.exe`**. The payload and the game converter are built in. Also contains `ps5cable.exe` (command line), `ps5cable-convert.exe`, `ps5cable.elf` and the cable setup script. |
| `ps5cable-linux-x64.tar.gz` | Linux. Needs Python 3.8+. Run `python3 hub.py`. |
| `ps5cable-macos-arm64.tar.gz` | Apple Silicon Macs. Needs Python 3.8+. Run `python3 hub.py`. |
| `ps5cable.elf` | The PS5 payload on its own, for payload managers and autoloaders. The Hub sends it for you. |
| `SHA256SUMS.txt` | Checksums of the files above. |

## Quick start

1. Connect the PC to the PS5's LAN port with an Ethernet cable (a USB-C to Ethernet adapter works; gigabit on a USB 3 port is best).
2. Give the cable fixed addresses.
   - **Windows:** in an Administrator PowerShell, run `powershell -ExecutionPolicy Bypass -File setup-direct-link-windows.ps1`.
   - **Linux or macOS:** run `sudo ./setup-direct-link-unix.sh <interface>`.
   - **PS5:** set up a **LAN cable** connection manually with IP `192.168.88.2`, mask `255.255.255.0`, gateway and DNS `192.168.88.1`.
3. Run your jailbreak so the ELF loader listens on port 9021.
4. Start **ps5cable Hub**. It sends the payload by itself and the light turns green: **PS5 Connected**.

The [README](https://github.com/plus44469420-boop/Din#readme) has the full guide.

## What's in it

- **ps5cable Hub**, a desktop file hub with your PC on one side and the PS5 on the other.
  - Send, drag and drop, and copy back to the PC.
  - Resume, parallel connections, and progress with speed and ETA.
  - Shortcuts to the folders where ps5upload, ShadowMount+ and etaHEN look for games and `.pkg` files.
- **Connection status**: a green **PS5 Connected** light, the USB-C adapter's type, its USB generation and Ethernet speed with the transfer limits they set, and a speed test.
- **Manage the PS5's files**:
  - delete, rename and new folder;
  - Copy to and Move to between folders and drives, with the copy running on the console;
  - folder sizes, free space, a right-click menu and keyboard shortcuts;
  - system folders are locked.
- **Convert games** with ps5upload's Convert Games options:
  - **package (`.pkg`)**: compression level, PlayGo language, content ID, firmware and passcode;
  - **game image**: `.ffpkg`, `.exfat`, `.ffpfs` or compressed `.ffpfsc`, with AMPR LZ4 asset packs;
  - **back to a folder**.
  - Converted files can go to the PS5 in one click.
- **`ps5cable` command line** for Windows, macOS and Linux: `put`, `get`, `ls`, `cp`, `mv`, `rm`, `du`, `df`, `status`, `speedtest`, `send-payload`.

## Notes

- Built with ps5-payload-sdk v0.44 for firmware up to 13.60. Tested against the desktop build of the payload; please report how it goes on your console.
- Copying a `.pkg` only puts the file on the PS5. Install it with etaHEN's package installer or ps5upload.
- The Windows executables aren't code-signed, so SmartScreen may warn the first time. Click **More info → Run anyway**.
- Licensed under GPL-3.0-or-later. The converter uses ps5upload's and PS5 Dump Forge's code (same licence).

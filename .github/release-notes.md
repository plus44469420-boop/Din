File transfer between a PC and a jailbroken PS5 over **one USB cable**: a USB bridge ("data link") cable with a Prolific **PL27A1** chip, with USB-C ⇄ USB-C, USB-A ⇄ USB-C or USB-A ⇄ USB-A ends. No Ethernet, router or Wi-Fi.

## Downloads

| File | For |
|---|---|
| **`ps5cable-windows.zip`** | Windows 10/11. Unzip and run **`ps5cable-hub.exe`**. USB support and the game converter are built in. Also contains `ps5cable.exe` (command line), `ps5cable-convert.exe` and `ps5cable.elf`. |
| `ps5cable-linux-x64.tar.gz` | Linux. Needs Python 3.8+; run `pip install -r requirements.txt`, then `python3 hub.py`. Includes the udev rule for the cable. |
| `ps5cable-macos-arm64.tar.gz` | Apple Silicon Macs. Needs Python 3.8+; run `pip install -r requirements.txt`, then `python3 hub.py`. |
| `ps5cable.elf` | The PS5 payload, for your payload loader or autoloader. |
| `SHA256SUMS.txt` | Checksums of the files above. |

## Quick start

1. Get a **USB 3 bridge cable with the PL27A1 chip**, with whichever ends suit your PC (USB-C or USB-A). A plain USB cable can't work: the PC and the PS5 are both USB hosts.
2. **Windows only, once:** plug the cable into the PC, click **Install driver** in the app and OK the Windows admin prompt. No Zadig needed.
3. Copy `ps5cable.elf` to the PS5 (USB stick + File Manager) and load it with your jailbreak's payload loader, or add it to your autoloader.
4. Plug the cable into the PS5 (rear USB-A, or the SuperSpeed front USB-C) and into a USB 3 port on the PC (USB-C, or a blue USB-A port), and start **ps5cable Hub**. The light turns green: **PS5 Connected**.

The [README](https://github.com/plus44469420-boop/Din#readme) has the full guide.

## What's in it

- **USB link** over USB-C or USB-A bridge cables: up to about 250 MB/s on a USB 3 bridge cable (about 35 MB/s on USB 2.0). The payload only listens on the console's loopback, so nothing on the PS5's network can reach it.
- **ps5cable Hub**, a desktop file hub with your PC on one side and the PS5 on the other.
  - Send, drag and drop, and copy back to the PC.
  - Resume, parallel transfers over the one cable, and progress with speed and ETA.
  - Shortcuts to the folders where ps5upload, ShadowMount+ and etaHEN look for games and `.pkg` files.
- **Connection status**: a green **PS5 Connected** light, the bridge chip and the USB speed of your PC's port with the limits they set, and a speed test.
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
- **`ps5cable` command line** for Windows, macOS and Linux: `put`, `get`, `ls`, `cp`, `mv`, `rm`, `du`, `df`, `status`, `speedtest`.

## Notes

- Built with ps5-payload-sdk v0.44 for firmware up to 13.60. The USB link is tested against a stand-in for the cable, not yet on a real console; please report how it goes, with the payload's `usb:` log lines if the PS5 never connects.
- Copying a `.pkg` only puts the file on the PS5. Install it with etaHEN's package installer or ps5upload.
- The Windows executables aren't code-signed, so SmartScreen may warn the first time. Click **More info → Run anyway**.
- Licensed under GPL-3.0-or-later. The converter uses ps5upload's and PS5 Dump Forge's code (same licence).

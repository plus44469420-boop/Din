**If Install driver said it closed before it finished:** that was the app giving up when the Windows admin prompt closed, while the installer was still running. This build waits for the installer itself. Click **Install driver**, choose **Yes**, and leave it until the message changes. If it still fails, the message includes the last line of `C:\ProgramData\PS5 Cable\driver.log`.

**If a USB-C cable installed its driver and then Windows stopped the device:** click **Install driver** again and replug the cable. The driver has to go on the bridge part of the cable; on the whole device, Windows stops it. A normal USB-C to USB-C cable (no chip in the middle) still cannot connect the PS5. The PS5 shows a message when the cable is plugged in and when a transfer starts.

File transfer between a PC and a jailbroken PS5 over **one USB cable**: a USB bridge ("data link") cable with a Prolific **PL27A1** chip, with USB-C ⇄ USB-C, USB-A ⇄ USB-C or USB-A ⇄ USB-A ends. No Ethernet, router or Wi-Fi.

## Downloads

| File | For |
|---|---|
| **`PS5-Cable-Windows.zip`** | Windows 10/11. Unzip and double-click **`PS5 Cable.exe`**. USB support, the USB driver installer and the game converter are built in. Next to it: `ps5cable.elf` for the PS5. |
| `PS5-Cable-Linux.tar.gz` | Linux. Run **`./ps5-cable`** (needs Python 3.8+; the first run sets up USB support). |
| `PS5-Cable-macOS.tar.gz` | Apple Silicon Macs. Run **`./ps5-cable`** (needs Python 3.8+; the first run sets up USB support). |
| `ps5cable.elf` | The PS5 side on its own, for your payload loader or autoloader. |
| `SHA256SUMS.txt` | Checksums of the files above. |

## Quick start

1. Get a **USB 3 bridge cable with the PL27A1 chip**, with whichever ends suit your PC (USB-C or USB-A). A plain USB cable can't work: the PC and the PS5 are both USB hosts.
2. **Windows only, once:** plug the cable into the PC, click **Install driver** in the app and OK the Windows admin prompt. No Zadig needed.
3. Copy `ps5cable.elf` to the PS5 (USB stick + File Manager) and load it with your jailbreak's payload loader, or add it to your autoloader.
4. Plug the cable into the PS5 (rear USB-A, or the SuperSpeed front USB-C) and into a USB 3 port on the PC (USB-C, or a blue USB-A port), and start **PS5 Cable**. The light turns green: **PS5 Connected**.

The [README](https://github.com/plus44469420-boop/Din#readme) has the full guide.

## What's in it

- **USB link** over USB-C or USB-A bridge cables: up to about 250 MB/s on a USB 3 bridge cable (about 35 MB/s on USB 2.0). The payload only listens on the console's loopback, so nothing on the PS5's network can reach it.
- **PS5 Cable**, a desktop app with your PC on one side and the PS5 on the other.
  - Send, drag and drop, and copy back to the PC.
  - Resume, parallel transfers over the one cable, and progress with speed and ETA.
  - Shortcuts to the folders where ps5upload, ShadowMount+ and etaHEN look for games and `.pkg` files.
- **Connection status**: a green **PS5 Connected** light, **Rescan USB** listing every USB device the PC sees, **Install driver** on Windows (no Zadig), the bridge chip and the USB speed of your PC's port with the limits they set, and a speed test.
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
- **Command line** (`./ps5-cable <command>` on Linux and macOS, `python pc/ps5cable.py` from the source): `put`, `get`, `ls`, `cp`, `mv`, `rm`, `du`, `df`, `status`, `speedtest`.

## Notes

- Built with ps5-payload-sdk v0.44 for firmware up to 13.60. The USB link is tested against a stand-in for the cable, not yet on a real console; please report how it goes, with the payload's `usb:` log lines if the PS5 never connects.
- Copying a `.pkg` only puts the file on the PS5. Install it with etaHEN's package installer or ps5upload.
- `PS5 Cable.exe` isn't code-signed, so SmartScreen may warn the first time. Click **More info → Run anyway**.
- Licensed under GPL-3.0-or-later. The converter uses ps5upload's and PS5 Dump Forge's code (same licence).

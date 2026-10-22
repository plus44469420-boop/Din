# PS5 Cable

File transfer between a PC and a jailbroken PS5 over **one USB cable** (USB-C or USB-A ends), with no Ethernet, router or Wi-Fi. It has three parts:

- **PS5 Cable**, the app (`PS5 Cable.exe` on Windows, `ps5-cable` on Linux and macOS): your PC's files on one side and the PS5's on the other. You pick what to send and watch it transfer. The game converter and the Windows USB driver installer are built in.
- **`ps5cable.elf`**: the PS5 side, a small file server that the jailbreak loads and that talks to the PC over the USB cable.
- A command line with the same features (`ps5-cable <command>`, or `python pc/ps5cable.py` from the source).

![PS5 Cable](docs/hub.png)

```
PC USB-C or USB-A ──[USB bridge ("data link") cable, PL27A1 chip]── PS5 USB-C or rear USB-A
```

## The cable: why it has to be a bridge cable

The PS5's USB ports and your PC's USB ports are all **USB host** ports. Two hosts joined by a plain cable don't exchange any data: USB always needs one host and one device. So a normal USB-C to USB-C, USB-A to USB-C or USB-A to USB-A cable will not work, whatever it is rated for.

A **USB bridge cable** (sold as a "data link", "transfer" or "PC-to-PC" cable) has a small chip in the middle that shows up as a USB device on *both* ends and passes data from one host to the other. PS5 Cable drives that chip directly with its own code on each side, so the PS5 needs no network driver and nothing else gets plugged in.

| Cable | Chip | Best case |
|---|---|---|
| USB 3 bridge cable (e.g. Plugable's transfer cables); any cable that says **Prolific PL27A1** | PL27A1, USB 3.0 (5 Gbps) | **~250 MB/s**, about 15 GB per minute, roughly twice what the PS5's gigabit Ethernet port can do |
| Older USB 2.0 bridge cable | PL25A1, USB 2.0 | ~35 MB/s |

Check that the listing names the **PL27A1** chip. Cables built on other chips aren't supported yet.

### USB-C, USB-A, or one of each

Bridge cables come as **USB-C ⇄ USB-C**, **USB-A ⇄ USB-C** and **USB-A ⇄ USB-A**, and all three work the same way. The chip in the middle is the same, so the app doesn't care which ends the cable has (USB doesn't report the connector shape anyway). A small USB-C ⇄ USB-A adapter on either end is fine too. What sets the speed is the USB generation of the two ports, so both ends need USB 3:

| Cable | PC end | PS5 end |
|---|---|---|
| USB-C ⇄ USB-C | a USB-C port with data | the SuperSpeed front USB-C port |
| USB-A ⇄ USB-C | the USB-C end in a USB-C port, or the USB-A end in a blue USB 3 port | the USB-A end in a rear USB-A port, or the USB-C end in the SuperSpeed front USB-C port |
| USB-A ⇄ USB-A | a blue (USB 3) USB-A port | a rear USB-A port |

- **PS5:** the rear USB-A ports are always SuperSpeed. The original PS5's front USB-A port, and one of the PS5 Slim's two front USB-C ports, are USB 2.0 only (~35 MB/s).
- **PC:** USB-A ports that are blue (or teal or red, or marked SS) are USB 3. Black or white ones are USB 2.0. Plug in directly, not through a USB 2 hub.

A USB-A ⇄ USB-C cable is a good fit for a PC with only USB-C ports and a PS5's rear USB-A port, or for a PC with only USB-A ports and the PS5's front USB-C port.

The app and `ps5-cable status` show the chip and the speed your PC's port negotiated, and the **speed test** measures both directions. It moves data in memory on the PS5, so nothing is written to its storage. A result around 35 MB/s means one end is on USB 2.0.

**Status:** the USB link is tested end to end against a stand-in for the cable, but not yet on a real PS5 with a PL27A1 cable. The PS5 side relies on the system's `libSceUsbd` library behaving like libusb, as it does on the PS4. If the PS5 never turns green with the payload running and the cable plugged in, please open an issue with the payload's `usb:` log lines.

## Setup (one time)

### 1. Get the app

Download the latest release from [Releases](https://github.com/plus44469420-boop/Din/releases/latest):

| Download | What's inside |
|---|---|
| **`PS5-Cable-Windows.zip`** (Windows 10/11) | `PS5 Cable.exe` (the app: double-click it), `ps5cable.elf` (for the PS5), `README.md`, `LICENSE.txt` |
| **`PS5-Cable-Linux.tar.gz`** / **`PS5-Cable-macOS.tar.gz`** (Apple Silicon) | `ps5-cable` (run it to open the app), `ps5cable.elf`, `README.md`, `LICENSE`, and `app/` with the program files. Needs Python 3.8+; the first run sets up USB support (pyusb, libusb) by itself. |
| `ps5cable.elf` | The PS5 side on its own, for your payload loader. |

You can also build everything yourself, as described in Building below.

### 2. PC: let the app open the cable

- **Windows:** the cable needs Windows' own **WinUSB** driver, once, and the app installs it: plug the cable into the PC, click **Install driver** on the PS5 side of the app, and OK the Windows admin prompt. No Zadig needed. The driver goes on the **bridge part** of the cable only. These cables are two devices in one (the bridge, plus a small "easy transfer" drive), and a driver on the whole device makes Windows stop the cable. If an older build already did that, click **Install driver** again: it removes that driver and puts it on the bridge part. Then unplug the cable and plug it back in. **Rescan USB** lists every USB device the PC sees. A normal USB-C to USB-C cable never appears in that list.
- **Linux:** run `sudo cp app/60-ps5cable.rules /etc/udev/rules.d/ && sudo udevadm control --reload`, then replug the cable. The app detaches the kernel's `plusb` network driver from the cable by itself.
- **macOS:** nothing to install.

### 3. PS5: run the payload

The payload has to be running on the PS5 before the PC can talk to it, and it can't be sent over the USB cable. Put it on the console once:

1. Copy `ps5cable.elf` to a USB stick, plug it into the PS5 and use your File Manager to copy it to the PS5 (for example to `/data/ps5cable/`).
2. Load it with your jailbreak's payload loader after the jailbreak runs. Most loaders can load an ELF from a file, and most autoloaders can start it on every jailbreak (for example by listing it in the autoloader's `autoload.txt`, or putting it in etaHEN's payload folder). Check your jailbreak's own instructions for where it looks.
3. The PS5 shows **"PS5 Cable ready: connect the USB bridge cable (USB-C or USB-A) to the PC"**. Loading it again replaces the running copy.

Then plug the cable into the PS5. The payload finds it within a second, and the PS5 shows **"PS5 Cable: PC connected over USB"** when the PC app connects.

## Using the app

1. Run your jailbreak and load the payload (setup step 3).
2. Double-click **`PS5 Cable.exe`**, or run `./ps5-cable` on Linux and macOS. It opens in its own window. From the source, use `pc\start-hub.bat` or `python pc/hub.py`.
3. Plug in the USB cable. The status light in the top bar turns **green and says "PS5 Connected"** as soon as the payload answers over USB. Amber means the cable is there but the PS5 isn't answering yet (payload not running, or the other end not plugged in) or that Windows still needs the USB driver; red means no cable is plugged into the PC. While it's not connected, the PS5 pane lists the steps still missing, with **Rescan USB** (look again now and list the PC's USB devices) and, on Windows, **Install driver**.
   - Next to it, the **cable pill** shows the bridge chip and the USB speed your PC's port negotiated, e.g. *Prolific PL27A1 · USB 3.2 Gen 1 — Max ~250 MB/s*. It turns amber when something limits the link (a USB 2.0 port or a USB 2.0 bridge). Click it for the details and a **Run speed test** button.
4. Pick what to send:
   - The **left pane is your PC**. Use the shortcut chips for Home, Desktop, Downloads and each drive letter, double-click folders to open them, and tick the files and folders you want. Shift-click selects a range, and Ctrl-click adds single items.
   - The **right pane is the PS5**. Its shortcut chips go to `/data`, `/data/homebrew`, USB drives and M.2 storage. The **Game folders** menu lists the usual drop folders on every drive (see below) and creates a missing one when you click it. Open the folder you want to copy into.
   - Click **Send to PS5 →**, or just drag the selection from the left pane onto the right one.
5. The **Transfers** panel shows each job's progress, speed and ETA, with a Cancel button. Folders are copied with everything inside.

Going the other way works the same way: select files on the PS5 side and click **← Copy to PC**, and they land in the folder open on the left.

### Managing files on the PS5

The PS5 pane is a file manager for the console, so you can clean up and reorganise from the PC:

- **Delete** removes the selected files and folders (folders with everything inside) after a confirmation. It can't be undone.
- **Rename**, **New folder**.
- **Copy to…** and **Move to…** offer the console's drives and game folders, or any folder you type. The copy runs on the PS5 itself, so nothing crosses the cable, and it shows up in Transfers with progress and Cancel. A move within one drive is instant. A move to another drive (e.g. internal → USB or M.2) copies and then deletes the original, because renaming across drives crashes the PS5. The original is kept if any file failed to copy. Copying into the same folder makes `name (copy)`.
- **Size** counts a folder on the console (files, subfolders and total) and shows it in the size column. With nothing selected it counts the open folder.
- The header shows the free space of the drive you're in.
- **Right-click** an item for all of these, plus **Download to PC** and **Copy path**.
- Shortcuts: **Del** delete, **F2** rename, **Ctrl+C / Ctrl+X / Ctrl+V** copy, cut and paste between PS5 folders, **Ctrl+A** select all, **Enter** open a folder, **Backspace** go up.

System folders are locked (🔒) and can't be deleted, renamed or moved from the app. That covers every top-level folder (`/system`, `/data`, `/user`, `/mnt`…), anything under `/system*`, `/preinst*` and `/update`, drive mount points such as `/mnt/usb0`, and `/user/app`, `/user/home` and the like. Everything below those, such as `/data/homebrew/PPSA01234` or `/mnt/usb0/homebrew`, is yours to manage. Deleting an installed game's folder from `/user/app` is possible but leaves a broken tile; uninstall from the PS5 instead.

**Skip / resume existing** is on by default. Files already complete on the destination are skipped, and partly copied ones continue from where they stopped, so re-running a cancelled or interrupted transfer only sends what's missing. **Parallel** sets the number of connections (default 4). The app remembers the last folder on each side.

About security: the app listens only on `127.0.0.1`, and every request needs a random token created at launch, so other devices and websites can't use it. Closing the window shuts the app down after a few minutes. It stays open while transfers are running, and the ⏻ button quits immediately.

### Where to put games and .pkg files

These follow the conventions of [ps5upload](https://github.com/phantomptr/ps5upload), ShadowMount+ and etaHEN. `<drive>` is `/data` (internal), `/mnt/ext0` / `/mnt/ext1` (M.2 / extended storage) or `/mnt/usb0`…`/mnt/usb7` (USB drives).

| Folder | Used for |
|---|---|
| `<drive>/homebrew` | Game folders (`PPSAxxxxx`). ShadowMount+ scans it by default; recommended. |
| `<drive>/etaHEN/games` | Game folders for etaHEN's game loader. |
| `<drive>/exfat` | Game images (`.exfat`, `.ffpkg`) for image mounting. |
| `/user/data/ps5upload/pkg_library` | `.pkg` files kept for ps5upload's installer. |

Extra folders listed as `scanpath=` lines in `/data/shadowmount/config.ini` show up in the menu too. Copying a `.pkg` only puts the file on the console; install it afterwards with etaHEN's package installer or ps5upload. A plain rename *between drives* is refused, because a cross-drive rename crashes the PS5 kernel; the app's **Move to…** copies and then deletes instead.

### Converting games (packages, game images, folders)

**Convert** in the top bar, or **Convert…** in the PC pane with a game folder or image selected, opens the same converter ps5upload has under *Convert Games*, with the same options. It runs on the PC, so the PS5 only has to be connected to send the result.

1. **Game**: a decrypted game folder (`sce_sys/param.json` + `eboot.bin`), a game image (`.exfat`, `.ffpkg`, `.ffpfs`, `.ffpfsc`) or a package made with the debug key. **Check** lists its title, content ID, size, the firmware it declares and runs on, and every readiness check.
2. **What to make**:
   - **A package (.pkg)**: compression **Fast**, **Balanced** (recommended), **Smallest** or **Stored** (no compression, for a console that won't start a compressed package), each with its estimated size and time; the **game language** PlayGo declares; and under *Package details* the content ID, file name, minimum firmware (filled in from the game's modules, so a backport declares what it really runs on) and passcode.
   - **A game image** for ShadowMount+: **.ffpkg (UFS2)**, recommended; **.exfat**; or **.ffpfs (PFS)**, experimental. **Compress it into a .ffpfsc** makes it 40–60% smaller and read-only. For an `.exfat` of a game that reads through AMPR, **AMPR LZ4 asset packs**, optionally with ampr_emu 0.4.2.1 put into the image and a custom `.toml` profile. **Read-back check, per read** sets the verification read size.
   - **A game folder**: an image or debug-key package unpacked back into a plain folder.
   - **Full verification** (all three) re-reads every byte of the output instead of a sample.
3. **Run**: conversions queue and run one at a time with progress per stage (plan, compress, write, verify) and can be cancelled. When one finishes, **Send to PS5** copies it over USB: game images to `/data/homebrew` by default, packages to `/user/data/ps5upload/pkg_library`.

On FW 13.60 a PS5 fake package installs but won't play, so for PS5 games make a **game image** (the app marks it as recommended). The converter is `ps5cable-convert` (in `converter/`), built on ps5upload's own writers: its `.exfat` writer and FPKG builder, and quer3q's PS5 Dump Forge `.ffpkg`/`.ffpfs`/`.ffpfsc` writers. Every output is written as `.partial`, read back and hash-checked, and only then renamed. Not ported: ps5upload's ZIP/7z/RAR sources and reading a game straight off the console; unpack archives or copy the game to the PC first.

## Using the command line

On Linux and macOS the command line is `./ps5-cable <command>`; on Windows run it from the source with Python as `python pc/ps5cable.py <command>` (the examples use that form).

1. Load the payload on the PS5 and plug in the cable (see Setup). `python pc/ps5cable.py status` should say **PS5 CONNECTED over USB**.

2. Transfer files:

   ```sh
   # upload a folder (recursive), 4 parallel connections
   python pc/ps5cable.py put "D:\Games\PPSA01234" /data/homebrew

   # an interrupted upload continues where it stopped; finished files are skipped
   python pc/ps5cable.py put --resume "D:\Games\PPSA01234" /data/homebrew

   # download from the PS5
   python pc/ps5cable.py get /data/homebrew/PPSA01234 D:\Backup

   # browse and manage files
   python pc/ps5cable.py ls /data
   python pc/ps5cable.py df /data
   python pc/ps5cable.py mkdir /data/homebrew/new
   python pc/ps5cable.py mv /data/a.bin /data/b.bin           # same drive only
   python pc/ps5cable.py cp /data/homebrew/PPSA01234 /mnt/usb0/homebrew  # copy on the console
   python pc/ps5cable.py du /data/homebrew/PPSA01234          # folder size
   python pc/ps5cable.py rm -r /data/homebrew/old

   python pc/ps5cable.py status     # connected? which cable, and how fast it can go
   python pc/ps5cable.py speedtest  # measure the link both ways (writes nothing on the PS5)
   python pc/ps5cable.py shutdown   # stop the payload
   ```

Every command waits up to 10 seconds for the PS5 on the cable (`--wait` changes that). Only one program can use the cable at a time, so close the app before using the command line.

Useful places on the PS5: `/data` is internal storage that your File Manager can see, `/mnt/usb0` is the first USB drive, and `/mnt/ext0` is M.2 or extended storage.

## Performance notes

- One large file fills the USB link by itself. `-j` (default 4) matters for folders with thousands of small files, where per-file round trips would otherwise dominate.
- Over the test cable (a loopback stand-in for USB) the payload, the USB link code and the client move 2–4 GB/s, so the cable is the only bottleneck.
- Uploads are written in place. If the transfer is interrupted, `--resume` checks each file's size on the PS5 and sends only what's missing.

## Building

The payload uses [ps5-payload-dev/sdk](https://github.com/ps5-payload-dev/sdk). v0.44 or newer is needed for firmware 13.60 kernel offsets.

```sh
sudo apt-get install clang-18 lld-18 llvm-18          # Debian/Ubuntu or WSL
wget https://github.com/ps5-payload-dev/sdk/releases/download/v0.44/ps5-payload-sdk.zip
sudo unzip -d /opt ps5-payload-sdk.zip
export PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk

make -C payload                                       # -> payload/ps5cable.elf
```

The converter needs Rust (stable, 1.85 or newer):

```sh
cargo build --release --manifest-path converter/Cargo.toml   # -> converter/target/release/ps5cable-convert
converter/target/release/ps5cable-convert image "D:\Games\PPSA01234" --format ffpkg --compress
converter/target/release/ps5cable-convert help                # every command and option
```

`make -C payload host` builds the same server for your PC, with a stand-in for the USB cable (`-C host:port`, a TCP connection that carries the same bulk transfers). The end-to-end tests and the app tests run every request through the real USB link code on both sides over that stand-in:

```sh
python3 -m unittest discover -s tests -v
```

To build `PS5 Cable.exe` (CI does the same on `windows-latest`), run this on Windows with `ps5cable.elf` in `payload/`:

```sh
pip install pyinstaller -r pc/requirements.txt
pyinstaller --onefile --noconsole --name "PS5 Cable" --paths pc --collect-all libusb_package --hidden-import usb.backend.libusb1 --add-data "pc/hub_ui.html;." --add-data "payload/ps5cable.elf;." --add-binary "converter/target/release/ps5cable-convert.exe;." pc/hub.py
```

Pushing a `v*` tag makes CI build the Windows, Linux and macOS packages and publish them as a GitHub release, with `.github/release-notes.md` as the description.

## Troubleshooting

- **"No USB bridge cable found", including with a USB-C to USB-C cable.** Click **Rescan USB**. A plain USB-C cable connects two USB hosts, so it never appears in that list and cannot work (see "The cable" above). If the cable is a bridge and it isn't listed, try another port: on the PS5 a rear USB-A port or the SuperSpeed front USB-C port, and on the PC a USB-C port with data or a blue USB-A port.
- **"Cable needs a driver" or Windows said the driver installed and then stopped.** Click **Install driver** and OK the admin prompt (setup step 2). That repairs a driver that was put on the whole cable, which is what makes Windows stop a USB-C bridge cable. Unplug the cable and plug it back in afterwards. If Windows asks for a restart, restart.
- **"No permission to open the cable" (Linux).** Install the udev rule (setup step 2) and replug, or run as root.
- **"USB cable found", but it never turns green.** The PS5 isn't answering: load `ps5cable.elf` on the PS5, check the cable is in the PS5, and watch the PS5's notifications. Unplugging and replugging the PS5 end makes the payload look for the cable again.
- **About 35 MB/s.** One end is on USB 2.0. Move the PS5 end to a rear USB-A port or the SuperSpeed front USB-C port, and plug the PC end straight into a USB-C port or a blue USB 3 USB-A port. The cable panel's port table lists the right ports for each cable type.
- **Logs.** The payload writes to the PS5 kernel log (lines starting with `[ps5cable]` and `usb:`), which a klog viewer payload shows.

## How it works

The payload serves a small file protocol: requests and responses with a fixed 24-byte header, followed by raw file bytes for uploads and downloads ([`payload/proto.h`](payload/proto.h)). It listens only on the console's loopback address, so nothing on the PS5's network can reach it.

The USB link carries that protocol between the two ends of the bridge cable ([`payload/usblink.h`](payload/usblink.h)). On the PS5 the payload opens the bridge through the system's `libSceUsbd` library. On the PC, `pc/usblink.py` opens it with pyusb/libusb and serves a local port that the app and command line connect to. Each connection becomes a channel on the cable, so several transfers run in parallel over one cable. Every USB transfer is one frame: a 12-byte header (`PU`, type, channel, length) and up to 256 KiB of data.

## Credits

The payload approach follows John Törnblom's [ftpsrv](https://github.com/ps5-payload-dev/ftpsrv) and the [ps5-payload-sdk](https://github.com/ps5-payload-dev/sdk). The bridge-chip handling follows Linux's `plusb` driver for the Prolific PL25A1/PL27A1. Licensed under GPL-3.0-or-later, matching the SDK.

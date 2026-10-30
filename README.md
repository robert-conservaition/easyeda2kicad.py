# LCSC → KiCad Importer

Find a part on [LCSC](https://www.lcsc.com/), click one button, and it's ready to place in any KiCad project: symbol, footprint, and 3D model, plus the JLCPCB part number, stock, and price. When the board is done, generate a BOM for JLCPCB assembly from a local dashboard.

It has three pieces:

- **Chrome extension:** adds an **Import to KiCad** button to `lcsc.com` and `jlcpcb.com/parts`
- **Background server:** runs on your Mac at `http://127.0.0.1:7777`, does the import, and hosts the dashboard
- **Shared KiCad library:** every part lands in `~/Documents/Kicad/easyeda2kicad/`, so it's available in all your projects

This is a customized fork of [uPesy/easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py). Install it from this repo, not with `pip install easyeda2kicad`: the PyPI version doesn't have these changes.

## Set up on a new Mac

You need a Mac with [KiCad](https://www.kicad.org/download/macos/) and Google Chrome installed. It takes about 10 minutes.

### 1. Install Apple's command line tools

This provides `git` and Python. If they're already installed, the command just tells you so.

```bash
xcode-select --install
```

### 2. Download this project

```bash
git clone https://github.com/robert-conservaition/easyeda2kicad.py.git ~/Documents/KiCad/easyeda2kicad.py
```

### 3. Bring your parts library (optional)

Your library isn't stored in GitHub. To keep your existing parts, copy this whole folder from your old computer to the same location on the new one:

```text
~/Documents/Kicad/easyeda2kicad/
```

Skip this step if you're starting fresh.

### 4. Run the installer

```bash
~/Documents/KiCad/easyeda2kicad.py/browser-extension/install.sh
```

The installer sets everything up and starts the server, which then starts by itself at every login.

If it asks for **Full Disk Access**, follow the steps it prints. This is a one-time macOS permission that lets the background server use your library in `~/Documents`.

### 5. Add the Chrome extension

1. Go to `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**
4. Press **⌘⇧G**, enter `~/Documents/KiCad/easyeda2kicad.py/browser-extension/chrome`, and click **Select**

### 6. Connect the library to KiCad

Do this once. In KiCad's project window, open the **KiCad** menu → **Preferences**.

1. **Configure Paths:** add a row named `EASYEDA2KICAD` with the path `/Users/YOUR_NAME/Documents/Kicad/easyeda2kicad`
2. **Manage Symbol Libraries:** on the **Global Libraries** tab, add a row with the nickname `easyeda2kicad` and the path `${EASYEDA2KICAD}/easyeda2kicad.kicad_sym`
3. **Manage Footprint Libraries:** on the **Global Libraries** tab, add a row with the nickname `easyeda2kicad` and the path `${EASYEDA2KICAD}/easyeda2kicad.pretty`

The nickname must be exactly `easyeda2kicad`. To confirm it all works, open http://127.0.0.1:7777/ and check that the dashboard shows your parts with no warning.

## Add a part to a project

### 1. Find it on LCSC

Search [lcsc.com](https://www.lcsc.com/) as usual. Check stock, price, and whether JLCPCB can assemble it.

### 2. Import it

- On a **product page**, click the green **Import to KiCad** button by the part name.
- On a **search or category page**, click the small **→ KiCad** button on the part's row.

The same buttons appear on `jlcpcb.com/parts`. When the import finishes, a message shows the KiCad symbol name with a copy button.

### 3. Place it in your schematic

1. Copy the symbol name from that message
2. In the KiCad schematic editor, press **A**
3. Paste the name, choose the part, and place it

If KiCad was already open and the part doesn't appear, close and reopen the schematic.

### 4. Lay it out

In the PCB editor, choose **Tools → Update PCB from Schematic** (**F8**). The footprint and 3D model come with the part.

Always check the footprint and pinout against the datasheet before ordering. Converted parts can contain mistakes.

### 5. Order from JLCPCB

1. Open the dashboard at http://127.0.0.1:7777/
2. Under **JLCPCB SMT BOM**, choose your project
3. Download the `.xlsx` or `.csv` and upload it to JLCPCB assembly

Any parts without a JLCPCB number are flagged so you can fix them first.

## The dashboard

The dashboard is at http://127.0.0.1:7777/.

- **Library status:** whether the server is healthy, and how many parts you have
- **Parts monitor:** search every part, see its stock, price, and class, and refresh one part with **Update**
- **Update All Parts:** re-import everything to refresh stock and pricing, with a live progress bar
- **JLCPCB SMT BOM:** generate an assembly BOM for a project in `~/Documents/KiCad`, or paste a project path

The extension's toolbar icon opens a smaller version with library status, **Update All Parts**, and an LCSC category menu.

## Get the latest version

```bash
cd ~/Documents/KiCad/easyeda2kicad.py && git pull && browser-extension/install.sh
```

Then go to `chrome://extensions` and click the reload icon on the **easyeda2kicad** card. Your parts library isn't affected.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| **✗ Server offline**, or the dashboard won't load | Restart the server (below). If that doesn't help, run `install.sh` again. |
| "No access to your KiCad library" | In System Settings → Privacy & Security → **Full Disk Access**, make sure `python3` is on. Then restart the server. |
| No import button on LCSC | Reload the extension at `chrome://extensions`, then refresh the page. |
| "Extension context invalidated" | Refresh the LCSC page. |
| **✗ Failed** on one part | The part may not be in EasyEDA's database, or the connection dropped. Try again, then check the log (below). |
| Part isn't in KiCad's chooser | Check step 6 of setup, then close and reopen the schematic. |
| 3D model is missing | Check that the `EASYEDA2KICAD` path in step 6 is correct. |

Restart the server:

```bash
launchctl kickstart -k gui/$(id -u)/com.easyeda2kicad.server
```

Read the server log:

```bash
tail -50 /tmp/easyeda2kicad-server.log
```

Uninstall the server and extension support (your parts library is kept):

```bash
~/Documents/KiCad/easyeda2kicad.py/browser-extension/uninstall.sh
```

## Command line

The installer also includes the original command line tool:

```bash
~/.easyeda2kicad/venv/bin/easyeda2kicad --full --lcsc_id=C2040
```

Add `--overwrite` to replace a part that's already in the library, or `--output ~/libs/my_lib` to write to a different library.

## License

AGPL-3.0, the same as the [original project](https://github.com/uPesy/easyeda2kicad.py) by uPesy. See [LICENSE](LICENSE).

There's no guarantee that converted symbols and footprints are correct, so always verify them before ordering boards.

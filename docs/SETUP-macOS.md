# easyeda2kicad — Browser Extension + Dashboard

## macOS Setup Guide

Import LCSC / JLCPCB parts straight into KiCad from your browser, keep stock and
pricing up to date, and generate JLCPCB SMT assembly BOMs from your KiCad
projects — all from a one-click toolbar button and a local dashboard.

## What you get

- A green **Import to KiCad** button on every lcsc.com and jlcpcb.com part page (symbol + footprint + 3D model).
- Imported parts are tagged with JLCPCB Part #, a Key-Attributes Description, In Stock, Unit Price, JLCPCB Class and the update date.
- A local **dashboard** at http://127.0.0.1:7777/ to monitor parts, update the whole library, and build JLCPCB BOMs.
- A background server that starts automatically at every login.

## Requirements

- macOS (Apple Silicon or Intel)
- Google Chrome
- KiCad 7, 8 or 9
- An internet connection (the importer fetches part data from EasyEDA / LCSC)

## Step 1 — Install the Command Line Tools

These provide the `python3` the server runs on. If you already have it, skip this.
Open the Terminal app and run:

```
xcode-select --install
```

Click through the dialog and wait for it to finish.

## Step 2 — Download the project

```
git clone https://github.com/robert-conservaition/easyeda2kicad.py.git
```

No GitHub account is needed — the repository is public. (Alternatively, download
the ZIP from the GitHub page and unzip it.)

## Step 3 — Run the installer

```
cd easyeda2kicad.py/browser-extension
./install.sh
```

The installer does everything automatically:

- Finds a stable `python3` (prefers Apple's at /usr/bin/python3).
- Creates a private virtual environment and installs easyeda2kicad **from this project** (so it includes the JLCPCB fields — not the older public version).
- Installs openpyxl so BOMs can export to .xlsx.
- Creates your KiCad library with a first sample part.
- Installs and starts a background service (launchd) on 127.0.0.1:7777.
- Guides you through the one manual step below.

## Step 4 — Grant Full Disk Access (one time)

Your KiCad library lives in your Documents folder, which macOS protects. A
background service cannot read or write it until you grant **Full Disk Access** to
the Python running it. The installer opens the right settings pane and waits — when
it does:

- In the window that opens, click the **+** button.
- Press **Command-Shift-G**, type the Python path the installer printed (usually `/usr/bin/python3`), and click Open.
- Turn **on** the toggle next to it.
- Return to the Terminal and press Enter.

To confirm it worked, open http://127.0.0.1:7777/ — you should see your parts, not a
permission warning.

## Step 5 — Load the Chrome extension

- Open Chrome and go to `chrome://extensions`
- Turn on **Developer mode** (top-right toggle).
- Click **Load unpacked** and select the `browser-extension/chrome` folder inside the project (the installer prints the exact path).

## Step 6 — Point KiCad at the libraries

- In KiCad: **Preferences → Configure Paths**, add a variable named `EASYEDA2KICAD` set to `/Users/YOUR_NAME/Documents/Kicad/easyeda2kicad/`.
- **Preferences → Manage Symbol Libraries**, add a global library named `easyeda2kicad` pointing to `${EASYEDA2KICAD}/easyeda2kicad.kicad_sym`.
- **Preferences → Manage Footprint Libraries**, add a global library named `easyeda2kicad` pointing to `${EASYEDA2KICAD}/easyeda2kicad.pretty`.

## Using it

- **Import a part:** browse lcsc.com or jlcpcb.com, click **Import to KiCad**, then paste the symbol name (shown in the success toast) into KiCad's symbol chooser.
- **Dashboard:** open http://127.0.0.1:7777/ to see every tracked part with its stock, price and class; filter the list; or click **Update All Parts** for a full refresh with a live progress bar.
- **Generate a BOM:** in the dashboard's JLCPCB SMT BOM section, pick one of your KiCad projects (or paste its path) and download a ready-to-upload .xlsx or .csv.

## Updating

Pull the latest code and re-run the installer. It is safe to run repeatedly.

```
cd easyeda2kicad.py
git pull
cd browser-extension
./install.sh
```

## Troubleshooting

- **Extension says "Failed to fetch":** the server isn't running. Open http://127.0.0.1:7777/ to check. Restart it with `launchctl kickstart -k gui/$(id -u)/com.easyeda2kicad.server`. Also click the reload icon on the extension card in chrome://extensions.
- **Dashboard shows a Full Disk Access warning:** redo Step 4, making sure the toggle is on for the exact Python path the installer printed.
- **Service won't start (exit 78):** usually a moved Python. Re-run `./install.sh`, which repoints it.
- **Logs:** `/tmp/easyeda2kicad-server.log`.

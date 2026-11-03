# easyeda2kicad — Browser Extension + Dashboard

## Windows Setup Guide

Import LCSC / JLCPCB parts straight into KiCad from your browser, keep stock and
pricing up to date, and generate JLCPCB SMT assembly BOMs from your KiCad
projects — all from a one-click toolbar button and a local dashboard.

There is no one-click installer on Windows yet, so this guide sets it up by hand.
The steps are copy-paste PowerShell commands; follow them in order.

## What you get

- A green **Import to KiCad** button on every lcsc.com and jlcpcb.com part page (symbol + footprint + 3D model).
- Imported parts are tagged with JLCPCB Part #, a Key-Attributes Description, In Stock, Unit Price, JLCPCB Class and the update date.
- A local **dashboard** at http://127.0.0.1:7777/ to monitor parts, update the whole library, and build JLCPCB BOMs.
- A background server that starts automatically at every login.

## Requirements

- Windows 10 or 11
- Python 3.9 or newer
- Google Chrome
- KiCad 7, 8 or 9
- An internet connection (the importer fetches part data from EasyEDA / LCSC)

Unlike macOS, Windows needs no "Full Disk Access" step.

## Step 1 — Install Python

- Download Python from https://www.python.org/downloads/windows/
- Run the installer and **check "Add python.exe to PATH"** on the first screen, then Install.
- Verify in a new PowerShell window: `python --version`

## Step 2 — Download the project

If you have Git:

```
git clone https://github.com/robert-conservaition/easyeda2kicad.py.git
```

Otherwise, open https://github.com/robert-conservaition/easyeda2kicad.py, click
**Code → Download ZIP**, and extract it. The repository is public — no account
needed.

## Step 3 — Install easyeda2kicad and the server

Open **PowerShell**, change into the project folder (the one containing
`setup.py`), and run these commands. They build a private environment and install
easyeda2kicad **from this project** (so it includes the JLCPCB fields — not the
older public version), plus openpyxl for .xlsx BOM export.

```
cd easyeda2kicad.py
python -m venv "$env:USERPROFILE\.easyeda2kicad\venv"
& "$env:USERPROFILE\.easyeda2kicad\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$env:USERPROFILE\.easyeda2kicad\venv\Scripts\python.exe" -m pip install .
& "$env:USERPROFILE\.easyeda2kicad\venv\Scripts\python.exe" -m pip install openpyxl
```

Then copy the server to a stable home:

```
New-Item -ItemType Directory -Force "$env:USERPROFILE\.easyeda2kicad" | Out-Null
Copy-Item .\browser-extension\server.py "$env:USERPROFILE\.easyeda2kicad\server.py"
```

## Step 4 — Create your KiCad library

Run a first import. **Note the "Library path:" it prints** — that exact path is
where KiCad will point in Step 7.

```
& "$env:USERPROFILE\.easyeda2kicad\venv\Scripts\easyeda2kicad.exe" --full --lcsc_id=C2040
```

## Step 5 — Test the server

```
& "$env:USERPROFILE\.easyeda2kicad\venv\Scripts\python.exe" "$env:USERPROFILE\.easyeda2kicad\server.py"
```

Open http://127.0.0.1:7777/ — you should see the dashboard with one part. Press
**Ctrl-C** in PowerShell to stop it (the next step makes it start on its own). If
Windows Defender Firewall prompts, the server only listens on 127.0.0.1 (your own
machine), so you can allow or cancel safely.

## Step 6 — Start it automatically at login

This creates a small launcher that runs the server with `pythonw.exe` (no console
window) and places it in your Startup folder.

```
$ee = "$env:USERPROFILE\.easyeda2kicad"
@"
start "" "$ee\venv\Scripts\pythonw.exe" "$ee\server.py"
"@ | Set-Content -Encoding ASCII "$ee\start-easyeda2kicad.bat"
Copy-Item "$ee\start-easyeda2kicad.bat" "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\start-easyeda2kicad.bat"
```

Start it now without rebooting:

```
& "$env:USERPROFILE\.easyeda2kicad\start-easyeda2kicad.bat"
```

Confirm at http://127.0.0.1:7777/.

## Step 7 — Load the Chrome extension

- Open Chrome and go to `chrome://extensions`
- Turn on **Developer mode** (top-right toggle).
- Click **Load unpacked** and select the `browser-extension\chrome` folder inside the project.

## Step 8 — Point KiCad at the libraries

Use the exact path easyeda2kicad printed in Step 4 (usually
`C:\Users\YOUR_NAME\Documents\Kicad\easyeda2kicad\`).

- **Preferences → Configure Paths**, add a variable named `EASYEDA2KICAD` set to that folder.
- **Preferences → Manage Symbol Libraries**, add a global library `easyeda2kicad` pointing to `${EASYEDA2KICAD}/easyeda2kicad.kicad_sym`.
- **Preferences → Manage Footprint Libraries**, add a global library `easyeda2kicad` pointing to `${EASYEDA2KICAD}/easyeda2kicad.pretty`.

## Using it

- **Import a part:** browse lcsc.com or jlcpcb.com, click **Import to KiCad**, then paste the symbol name (shown in the success toast) into KiCad's symbol chooser.
- **Dashboard:** open http://127.0.0.1:7777/ to see every tracked part with its stock, price and class; filter the list; or click **Update All Parts** for a full refresh with a live progress bar.
- **Generate a BOM:** in the dashboard's JLCPCB SMT BOM section, pick one of your KiCad projects (or paste its path) and download a ready-to-upload .xlsx or .csv.

## Updating

Pull the latest code, reinstall into the venv, and refresh the server:

```
cd easyeda2kicad.py
git pull
& "$env:USERPROFILE\.easyeda2kicad\venv\Scripts\python.exe" -m pip install .
Copy-Item .\browser-extension\server.py "$env:USERPROFILE\.easyeda2kicad\server.py" -Force
Stop-Process -Name pythonw -ErrorAction SilentlyContinue
& "$env:USERPROFILE\.easyeda2kicad\start-easyeda2kicad.bat"
```

## Troubleshooting

- **Extension says "Failed to fetch":** the server isn't running. Open http://127.0.0.1:7777/ to check, run `start-easyeda2kicad.bat`, and click the reload icon on the extension card in chrome://extensions.
- **Dashboard shows 0 parts / "library not found":** KiCad and the server disagree on the library path. Use the exact path easyeda2kicad printed in Step 4. If your Documents folder is managed by OneDrive, that path is still correct for the server — just point KiCad at the same place.
- **Server can't find easyeda2kicad:** make sure you launch the server with the venv's Python (`...\.easyeda2kicad\venv\Scripts\python.exe` or `pythonw.exe`), as this guide does.
- **Stop the server:** `Stop-Process -Name pythonw` (note: this stops every pythonw process), or end `pythonw.exe` in Task Manager.
- **"running scripts is disabled" error:** these are commands, not a script file, so they run even under the default policy. If you saved them as a `.ps1`, run it once with `powershell -ExecutionPolicy Bypass -File yourfile.ps1`.

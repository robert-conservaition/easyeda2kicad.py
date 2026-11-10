#!/usr/bin/env python3
"""
Tiny local HTTP server that the Chrome extension calls to trigger easyeda2kicad.
Runs on http://localhost:7777 — started automatically by launchd at login.

When easyeda2kicad can't find a part in EasyEDA's database (many through-hole or
niche SMD parts), the server falls back to querying the LCSC Pro API directly and
generating a minimal-but-complete KiCad symbol from scratch.
"""

import glob
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# Serialises all library work: mutating requests (a single import while
# "Update All" runs) can't corrupt the .kicad_sym via overlapping writes, and
# reads can't observe a half-written file (which would flash 0 symbols).
# Reentrant so a read helper can call another read helper while holding it.
_LIB_LOCK = threading.RLock()

# Live progress for the background "Update All" job.
_UPDATE_LOCK  = threading.Lock()
_UPDATE_STATE = {
    "running": False, "total": 0, "done": 0, "updated": 0, "failed": 0,
    "current": "", "errors": [], "started_at": None, "finished_at": None,
}

PORT = 7777
_START_TIME = time.time()

# ---------------------------------------------------------------------------
# easyeda2kicad location
# ---------------------------------------------------------------------------

def _easyeda2kicad_works(path):
    """Return True when the easyeda2kicad binary can import and run."""
    try:
        result = subprocess.run(
            [path, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and "Traceback" not in combined


def find_easyeda2kicad():
    """
    Locate a working easyeda2kicad executable, cross-platform.

    Order: PATH, then next to the running Python interpreter (so a server
    launched with a venv's python finds that venv's easyeda2kicad even when the
    venv isn't on PATH — the normal case on Windows), then the dedicated
    ~/.easyeda2kicad/venv the installer creates, then common install locations.
    """
    is_win = os.name == "nt"
    exe    = "easyeda2kicad.exe" if is_win else "easyeda2kicad"

    candidates = []
    for name in ("easyeda2kicad", exe):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    # Beside the interpreter running this server: venv/Scripts (Windows) or
    # venv/bin (Unix).
    py_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates += [
        os.path.join(py_dir, exe),
        os.path.join(py_dir, "Scripts", exe),
        os.path.join(py_dir, "bin", exe),
    ]

    # The dedicated venv the installer creates.
    venv = os.path.expanduser("~/.easyeda2kicad/venv")
    candidates += [
        os.path.join(venv, "Scripts", exe),
        os.path.join(venv, "bin", exe),
    ]

    # Repo-local venv and common Unix install locations.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates += [
        os.path.join(repo, ".venv", "Scripts", exe),
        os.path.join(repo, ".venv", "bin", exe),
        os.path.expanduser("~/miniconda3/bin/easyeda2kicad"),
        os.path.expanduser("~/anaconda3/bin/easyeda2kicad"),
        os.path.expanduser("~/.local/bin/easyeda2kicad"),
        "/usr/local/bin/easyeda2kicad",
        "/opt/homebrew/bin/easyeda2kicad",
    ]

    seen = set()
    for c in candidates:
        c = os.path.abspath(c)
        if c in seen or not os.path.isfile(c):
            continue
        seen.add(c)
        if _easyeda2kicad_works(c):
            return c
    return None


EASYEDA2KICAD = find_easyeda2kicad()


def get_easyeda2kicad():
    """Return the easyeda2kicad path, re-probing if the startup lookup failed.

    The lookup can fail transiently when launchd starts the server at login
    (e.g. before ~/Documents is readable), so don't cache a miss forever.
    """
    global EASYEDA2KICAD
    if not EASYEDA2KICAD:
        EASYEDA2KICAD = find_easyeda2kicad()
    return EASYEDA2KICAD


# ---------------------------------------------------------------------------
# Helpers for parsing easyeda2kicad output
# ---------------------------------------------------------------------------

def parse_symbol_name(stderr):
    """Extract 'Symbol name : FOO' from easyeda2kicad stderr."""
    m = re.search(r"Symbol name\s*:\s*(\S+)", stderr)
    return m.group(1) if m else None


def parse_lib_path(stderr):
    """Extract the .kicad_sym path from easyeda2kicad stderr output."""
    m = re.search(r"Library path\s*:\s*(.+\.kicad_sym)", stderr)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Post-import field writer (adds description, stock, date to existing symbols)
# ---------------------------------------------------------------------------

def _remove_property_block(content, sym_start, insert_pos, field_name):
    """
    Remove the (property "field_name" ...) block that falls within
    [sym_start, insert_pos).  Returns (new_content, new_insert_pos).
    """
    pattern = re.compile(
        r'\(\s*property\s*\n?\s*"' + re.escape(field_name) + r'"'
    )
    m = pattern.search(content, sym_start, insert_pos)
    if not m:
        return content, insert_pos

    start = m.start()
    depth = 0
    i = start
    while i < len(content):
        if content[i] == "(":
            depth += 1
        elif content[i] == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                if end < len(content) and content[end] == "\n":
                    end += 1
                removed = end - start
                content = content[:start] + content[end:]
                insert_pos -= removed
                return content, insert_pos
        i += 1
    return content, insert_pos


def _prop_kicad7(name, value, hidden=True, indent="\t\t"):
    """
    Return a KiCad 7-format property block (no id attribute).
    `indent` sets the base indentation for the property line; sub-elements
    are indented one level deeper.
    """
    i0 = indent          # property line
    i1 = indent + "\t"   # at / effects line
    i2 = indent + "\t\t" # font / hide line
    i3 = indent + "\t\t\t"  # size line
    hide_line = f"{i2}(hide yes)\n" if hidden else ""
    safe_val  = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'{i0}(property "{name}" "{safe_val}"\n'
        f'{i1}(at 0 0 0)\n'
        f'{i1}(effects\n'
        f'{i2}(font\n'
        f'{i3}(size 1.27 1.27)\n'
        f'{i2})\n'
        f'{hide_line}'
        f'{i1})\n'
        f'{i0})\n'
    )


def _dedup_symbol(sym_path, symbol_name):
    """
    Remove all but the LAST occurrence of (symbol "NAME" ...) in the library.
    easyeda2kicad --overwrite appends a new entry without removing the old one,
    so after it runs we clean up stale duplicates.
    """
    try:
        with open(sym_path, "r", encoding="utf-8") as f:
            content = f.read()

        pattern = f'(symbol "{symbol_name}"'
        if content.count(pattern) <= 1:
            return

        starts, ends = [], []
        pos = 0
        while True:
            idx = content.find(pattern, pos)
            if idx == -1:
                break
            end = _match_block(content, idx)
            starts.append(idx)
            ends.append(end)
            pos = end

        if len(starts) <= 1:
            return

        # Remove all but the last (newest) entry, in reverse order.
        for start, end in zip(reversed(starts[:-1]), reversed(ends[:-1])):
            block_start = start
            while block_start > 0 and content[block_start - 1] in (" ", "\t"):
                block_start -= 1
            block_end = end
            if block_end < len(content) and content[block_end] == "\n":
                block_end += 1
            content = content[:block_start] + content[block_end:]

        with open(sym_path, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"[easyeda2kicad-server] deduplicated symbol '{symbol_name}'", flush=True)

    except Exception as e:
        print(f"[easyeda2kicad-server] warning: dedup failed: {e}", flush=True)


def add_symbol_fields(sym_path, symbol_name, description, in_stock, price="", jlcpcb_class="",
                      manufacturer="", mpn=""):
    """
    Add/update In Stock and Stock Update Date on an easyeda2kicad-generated symbol.
    Also writes Manufacturer and MPN (manufacturer part number) when known, so the
    BOM works for distributors other than JLCPCB (Digi-Key, Mouser, PCBWay, ...).
    Also updates the Description field only when the browser supplied something
    meaningful (not just the 'LCSC: Cxxxxxx' last-resort fallback).

    Uses KiCad 7 format: no (id N), tab-indented, (hide yes) on its own line.
    Removes any legacy ki_description field from older imports.
    """
    try:
        with open(sym_path, "r", encoding="utf-8") as f:
            content = f.read()

        sym_start = content.find(f'(symbol "{symbol_name}"')
        if sym_start == -1:
            return

        nested_pat = re.compile(
            r'\(symbol "' + re.escape(symbol_name) + r'_\d+_\d+"'
        )
        nested_m = nested_pat.search(content, sym_start)
        if not nested_m:
            return
        insert_pos = nested_m.start()

        # A description is "trivial" when it's just the last-resort fallback.
        desc_is_useful = bool(
            description and
            not re.match(r'^LCSC:\s*C\d+\s*$', description.strip()) and
            len(description.strip()) > 5
        )

        # Strip old/existing versions of every field we're about to re-write.
        fields_to_remove = ["ki_description", "In Stock", "Unit Price", "JLCPCB Class", "Stock Update Date"]
        if manufacturer:
            fields_to_remove.append("Manufacturer")
        if mpn:
            fields_to_remove.append("MPN")
        if desc_is_useful:
            fields_to_remove.append("Description")

        for field in fields_to_remove:
            content, insert_pos = _remove_property_block(
                content, sym_start, insert_pos, field
            )

        nested_m = nested_pat.search(content, sym_start)
        if not nested_m:
            return
        insert_pos = nested_m.start()

        today     = datetime.now()
        today_str = f"{today.strftime('%b')} {today.day}, {today.year}"

        props = []
        if desc_is_useful:
            props.append(_prop_kicad7("Description", description))
        if in_stock:
            props.append(_prop_kicad7("In Stock", str(in_stock)))
        if price:
            props.append(_prop_kicad7("Unit Price", price))
        if jlcpcb_class:
            props.append(_prop_kicad7("JLCPCB Class", jlcpcb_class))
        if manufacturer:
            props.append(_prop_kicad7("Manufacturer", manufacturer))
        if mpn:
            props.append(_prop_kicad7("MPN", mpn))
        props.append(_prop_kicad7("Stock Update Date", today_str))

        content = content[:insert_pos] + "".join(props) + content[insert_pos:]

        with open(sym_path, "w", encoding="utf-8") as f:
            f.write(content)

    except Exception as e:
        print(f"[easyeda2kicad-server] warning: could not write symbol fields: {e}", flush=True)


# ---------------------------------------------------------------------------
# Fallback: generate a minimal KiCad symbol from LCSC Pro API data
# ---------------------------------------------------------------------------

# KiCad standard footprint libraries keyed by (ref, size_token)
_FP_CAP_SMD = {
    "0201": "Capacitor_SMD:C_0201_0603Metric",
    "0402": "Capacitor_SMD:C_0402_1005Metric",
    "0603": "Capacitor_SMD:C_0603_1608Metric",
    "0805": "Capacitor_SMD:C_0805_2012Metric",
    "1206": "Capacitor_SMD:C_1206_3216Metric",
    "1210": "Capacitor_SMD:C_1210_3225Metric",
    "1812": "Capacitor_SMD:C_1812_4532Metric",
    "2220": "Capacitor_SMD:C_2220_5750Metric",
}
_FP_RES_SMD = {
    "0201": "Resistor_SMD:R_0201_0603Metric",
    "0402": "Resistor_SMD:R_0402_1005Metric",
    "0603": "Resistor_SMD:R_0603_1608Metric",
    "0805": "Resistor_SMD:R_0805_2012Metric",
    "1206": "Resistor_SMD:R_1206_3216Metric",
    "1210": "Resistor_SMD:R_1210_3225Metric",
    "2512": "Resistor_SMD:R_2512_6332Metric",
}
_FP_IND_SMD = {
    "0402": "Inductor_SMD:L_0402_1005Metric",
    "0603": "Inductor_SMD:L_0603_1608Metric",
    "0805": "Inductor_SMD:L_0805_2012Metric",
    "1206": "Inductor_SMD:L_1206_3216Metric",
    "1210": "Inductor_SMD:L_1210_3225Metric",
}
# Through-hole radial caps: diameter → (pitch, footprint)
_FP_CAP_TH = [
    (4.0,  "Capacitor_THT:CP_Radial_D4.0mm_P1.50mm"),
    (5.0,  "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm"),
    (6.3,  "Capacitor_THT:CP_Radial_D6.3mm_P2.50mm"),
    (8.0,  "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm"),
    (10.0, "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm"),
    (12.5, "Capacitor_THT:CP_Radial_D12.5mm_P5.00mm"),
    (16.0, "Capacitor_THT:CP_Radial_D16.0mm_P7.50mm"),
    (18.0, "Capacitor_THT:CP_Radial_D18.0mm_P7.50mm"),
    (25.0, "Capacitor_THT:CP_Radial_D25.0mm_P10.00mm"),
]


def _guess_footprint(ref, package):
    """
    Map an EasyEDA package string to the closest KiCad standard library footprint.
    Returns "" when no mapping is found.
    """
    pkg = (package or "").strip()
    pkg_up = pkg.upper()
    pkg_lo = pkg.lower()

    # Extract bare SMD size token (0402, 0603, 0805, etc.)
    m_smd = re.search(r'\b(0201|0402|0603|0805|1206|1210|1812|2220|2512)\b', pkg_up)
    smd_size = m_smd.group(1) if m_smd else None

    if ref == "C":
        if smd_size:
            return _FP_CAP_SMD.get(smd_size, "")
        # Through-hole radial: look for diameter like D12.5 or D12
        dm = re.search(r'[Dd](\d+(?:\.\d+)?)', pkg)
        if dm:
            d = float(dm.group(1))
            best = min(_FP_CAP_TH, key=lambda x: abs(x[0] - d))
            if abs(best[0] - d) <= 2.0:
                return best[1]

    elif ref == "R":
        if smd_size:
            return _FP_RES_SMD.get(smd_size, "")
        # Axial through-hole resistors
        if "through" in pkg_lo or "bd" in pkg_lo or "axial" in pkg_lo:
            return "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"

    elif ref == "L":
        if smd_size:
            return _FP_IND_SMD.get(smd_size, "")

    elif ref == "D":
        if "sod-123" in pkg_lo:
            return "Diode_SMD:D_SOD-123"
        if "sod-323" in pkg_lo:
            return "Diode_SMD:D_SOD-323"
        if "sma" in pkg_lo or "do-214ac" in pkg_lo:
            return "Diode_SMD:D_SMA"
        if "smb" in pkg_lo or "do-214aa" in pkg_lo:
            return "Diode_SMD:D_SMB"
        if "smc" in pkg_lo or "do-214ab" in pkg_lo:
            return "Diode_SMD:D_SMC"
        if smd_size:
            return f"Diode_SMD:D_{smd_size}_2512Metric"

    elif ref == "Q":
        if "sot-23" in pkg_lo:
            return "Package_TO_SOT_SMD:SOT-23"
        if "to-92" in pkg_lo:
            return "Package_TO_SOT_THT:TO-92_Inline"
        if "to-220" in pkg_lo:
            return "Package_TO_SOT_THT:TO-220-3_Vertical"

    return ""


def _detect_component_type(category, mpn):
    """Return (KiCad reference letter, prefix) from category/mpn strings."""
    c = (category or "").lower()
    m = (mpn or "").lower()

    if "capacitor" in c or "capacitance" in c:
        return "C", "C?"
    if "resistor" in c or "resistance" in c:
        return "R", "R?"
    if "inductor" in c or "inductance" in c or "ferrite" in c:
        return "L", "L?"
    if "led" in c:
        return "D", "D?"
    if "diode" in c:
        return "D", "D?"
    if "transistor" in c or "mosfet" in c or "bjt" in c or "igbt" in c:
        return "Q", "Q?"
    if "crystal" in c or "oscillator" in c or "resonator" in c:
        return "Y", "Y?"
    if "fuse" in c:
        return "F", "F?"
    if "connector" in c or "header" in c or "socket" in c:
        return "J", "J?"
    if "switch" in c or "button" in c:
        return "SW", "SW?"
    return "U", "U?"


def _make_symbol_shape(symbol_name, ref):
    """
    Return the inner (symbol "NAME_0_1" ...) block for the given reference type.
    The inner block contains the schematic drawing geometry and pins.
    """
    n = symbol_name.replace("\\", "\\\\").replace('"', '\\"')
    inner = f'"{n}_0_1"'

    if ref == "C":
        # Non-polarised capacitor: two parallel horizontal plates + vertical pins
        return (
            f'    (symbol {inner}\n'
            f'      (polyline\n'
            f'        (pts (xy -1.524 -0.508) (xy 1.524 -0.508))\n'
            f'        (stroke (width 0.508) (type default) (color 0 0 0 0))\n'
            f'        (fill (type none))\n'
            f'      )\n'
            f'      (polyline\n'
            f'        (pts (xy -1.524 0.508) (xy 1.524 0.508))\n'
            f'        (stroke (width 0.508) (type default) (color 0 0 0 0))\n'
            f'        (fill (type none))\n'
            f'      )\n'
            f'      (pin passive line\n'
            f'        (at 0 3.81 270)\n'
            f'        (length 3.302)\n'
            f'        (name "~" (effects (font (size 1.27 1.27))))\n'
            f'        (number "1" (effects (font (size 1.27 1.27))))\n'
            f'      )\n'
            f'      (pin passive line\n'
            f'        (at 0 -3.81 90)\n'
            f'        (length 3.302)\n'
            f'        (name "~" (effects (font (size 1.27 1.27))))\n'
            f'        (number "2" (effects (font (size 1.27 1.27))))\n'
            f'      )\n'
            f'    )\n'
        )

    if ref in ("R", "L", "F"):
        # Rectangular body with horizontal pins (resistor / inductor / fuse)
        return (
            f'    (symbol {inner}\n'
            f'      (rectangle\n'
            f'        (start -2.54 1.02)\n'
            f'        (end 2.54 -1.02)\n'
            f'        (stroke (width 0) (type default) (color 0 0 0 0))\n'
            f'        (fill (type background))\n'
            f'      )\n'
            f'      (pin passive line\n'
            f'        (at -5.08 0 0)\n'
            f'        (length 2.54)\n'
            f'        (name "~" (effects (font (size 1.27 1.27))))\n'
            f'        (number "1" (effects (font (size 1.27 1.27))))\n'
            f'      )\n'
            f'      (pin passive line\n'
            f'        (at 5.08 0 180)\n'
            f'        (length 2.54)\n'
            f'        (name "~" (effects (font (size 1.27 1.27))))\n'
            f'        (number "2" (effects (font (size 1.27 1.27))))\n'
            f'      )\n'
            f'    )\n'
        )

    if ref == "D":
        # Diode: filled triangle + cathode bar + anode/cathode pins
        return (
            f'    (symbol {inner}\n'
            f'      (polyline\n'
            f'        (pts (xy -1.27 -1.27) (xy -1.27 1.27) (xy 1.27 0) (xy -1.27 -1.27))\n'
            f'        (stroke (width 0.254) (type default) (color 0 0 0 0))\n'
            f'        (fill (type background))\n'
            f'      )\n'
            f'      (polyline\n'
            f'        (pts (xy 1.27 -1.27) (xy 1.27 1.27))\n'
            f'        (stroke (width 0.254) (type default) (color 0 0 0 0))\n'
            f'        (fill (type none))\n'
            f'      )\n'
            f'      (pin passive line\n'
            f'        (at -3.81 0 0)\n'
            f'        (length 2.54)\n'
            f'        (name "A" (effects (font (size 1.27 1.27))))\n'
            f'        (number "1" (effects (font (size 1.27 1.27))))\n'
            f'      )\n'
            f'      (pin passive line\n'
            f'        (at 3.81 0 180)\n'
            f'        (length 2.54)\n'
            f'        (name "K" (effects (font (size 1.27 1.27))))\n'
            f'        (number "2" (effects (font (size 1.27 1.27))))\n'
            f'      )\n'
            f'    )\n'
        )

    # Generic fallback: labelled rectangle with two pins on the left
    return (
        f'    (symbol {inner}\n'
        f'      (rectangle\n'
        f'        (start -2.54 2.54)\n'
        f'        (end 2.54 -2.54)\n'
        f'        (stroke (width 0) (type default) (color 0 0 0 0))\n'
        f'        (fill (type background))\n'
        f'      )\n'
        f'      (pin unspecified line\n'
        f'        (at -5.08 1.27 0)\n'
        f'        (length 2.54)\n'
        f'        (name "1" (effects (font (size 1.27 1.27))))\n'
        f'        (number "1" (effects (font (size 1.27 1.27))))\n'
        f'      )\n'
        f'      (pin unspecified line\n'
        f'        (at -5.08 -1.27 0)\n'
        f'        (length 2.54)\n'
        f'        (name "2" (effects (font (size 1.27 1.27))))\n'
        f'        (number "2" (effects (font (size 1.27 1.27))))\n'
        f'      )\n'
        f'    )\n'
    )


def _fetch_lcsc_part_info(lcsc_id):
    """
    Query the EasyEDA Pro / LCSC API for the part.
    Returns a dict or None.
    """
    url = (
        "https://pro.easyeda.com/api/eda/product/list"
        f"?keyword={urllib.parse.quote(lcsc_id)}&limit=20"
    )
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"[easyeda2kicad-server] LCSC API fetch error: {exc}", flush=True)
        return None

    if not data.get("success"):
        print(f"[easyeda2kicad-server] Pro API returned success=false for {lcsc_id}: {data.get('message', '')}", flush=True)
        return None

    products = data.get("result", {}).get("productList", [])
    print(f"[easyeda2kicad-server] Pro API: {len(products)} results for {lcsc_id}, numbers={[p.get('number') for p in products[:5]]}", flush=True)
    # Exact match on LCSC number
    part = next((p for p in products if p.get("number") == lcsc_id), None)
    if not part:
        print(f"[easyeda2kicad-server] Pro API: no exact match for {lcsc_id}", flush=True)
        return None

    # Best-effort category from the paramList in the same response
    category = ""
    for param in data.get("result", {}).get("paramList", []):
        if param.get("parameterName") == "Category":
            vals = param.get("parameterValueList", [])
            if vals:
                category = vals[0]
                break

    lcsc_url = ""
    if part.get("url"):
        lcsc_url = "https://www.lcsc.com" + part["url"]

    # Extract a description from device_info attributes when present
    device_desc = ""
    di = part.get("device_info") or {}
    if di:
        # The EasyEDA Pro description string is "Key:Value Key:Value ..." with duplicates.
        # Parse it into a deduped list and format cleanly.
        raw_desc = (di.get("description") or "").strip()
        if raw_desc:
            seen_vals, parts_list = set(), []
            for token in re.split(r'(?<=[^\s])\s+(?=[A-Z][^:]+:)', raw_desc):
                kv = token.strip()
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    v = v.strip()
                    if v and v not in seen_vals:
                        seen_vals.add(v)
                        parts_list.append(v)
            device_desc = " ".join(parts_list[:8])
        # Fall back to the attributes dict if description string was empty
        if not device_desc:
            attrs = di.get("attributes") or {}
            skip = {"LCSC Part Name", "Supplier Part", "Supplier", "Datasheet",
                    "Symbol", "Footprint", "3D Model", "3D Model Title",
                    "3D Model Transform", "Add into BOM", "Convert to PCB",
                    "Designator", "JLCPCB Part Class"}
            pairs = [str(v).strip() for k, v in attrs.items()
                     if k not in skip and v and str(v).strip()]
            device_desc = " ".join(pairs[:8])

    # Unit price from the first price tier: [[qty, price, price], ...]
    price_str = ""
    price_list = part.get("price", [])
    if price_list and isinstance(price_list[0], (list, tuple)) and len(price_list[0]) >= 2:
        tier = price_list[0]
        price_str = f"${tier[1]} ({tier[0]}+)"

    jlcpcb_class = part.get("JLCPCB Part Class", "")

    return {
        "mpn":          part.get("mpn", ""),
        "manufacturer": part.get("manufacturer", ""),
        "package":      part.get("package", ""),
        "stock":        str(part.get("stock", "")),
        "category":     category,
        "lcsc_url":     lcsc_url,
        "device_desc":  device_desc,
        "price_str":    price_str,
        "jlcpcb_class": jlcpcb_class,
    }


def _user_config():
    """Read ~/.easyeda2kicad/config.json plus env overrides (same rules as the CLI)."""
    data = {}
    try:
        with open(os.path.expanduser("~/.easyeda2kicad/config.json"), encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    for key, env in (("library", "EASYEDA2KICAD_LIBRARY"), ("projects_dir", "EASYEDA2KICAD_PROJECTS")):
        if os.environ.get(env):
            data[key] = os.environ[env]
    return data


def _default_lib_path():
    library = _user_config().get("library")
    if library:
        library = os.path.expanduser(library)
        return library if library.endswith(".kicad_sym") else library + ".kicad_sym"
    return os.path.join(
        os.path.expanduser("~"),
        "Documents", "Kicad", "easyeda2kicad", "easyeda2kicad.kicad_sym",
    )


_SYMBOL_NAME_RE = re.compile(r'\(symbol "((?:[^"\\]|\\.)*)"')


def _iter_top_symbols(content):
    """
    Yield (name, line_start, paren_index, end) for every *top-level* symbol —
    a (symbol ...) that is a direct child of (kicad_symbol_lib), i.e. at paren
    depth 1.  Purely structural, so it works no matter how the file is indented
    (KiCad 9 uses tabs; easyeda2kicad appends with spaces; libraries end up
    mixed).  Nested unit sub-symbols (depth 2, "NAME_0_1") are never yielded.
    """
    i, n, depth = 0, len(content), 0
    while i < n:
        c = content[i]
        if c == '"':                      # skip over string literals
            i += 1
            while i < n and content[i] != '"':
                if content[i] == '\\':
                    i += 1
                i += 1
            i += 1
            continue
        if c == '(':
            if depth == 1 and content.startswith('(symbol "', i):
                m = _SYMBOL_NAME_RE.match(content, i)
                if m:
                    ls = i
                    while ls > 0 and content[ls - 1] in " \t":
                        ls -= 1
                    yield m.group(1), ls, i, _match_block(content, i)
            depth += 1
        elif c == ')':
            depth -= 1
        i += 1


def _remove_existing_symbol(content, symbol_name):
    """Remove a top-level symbol block matching (symbol "symbol_name" ...) from content."""
    for name, line_start, paren, end in _iter_top_symbols(content):
        if name == symbol_name:
            if end < len(content) and content[end] == "\n":
                end += 1
            return content[:line_start] + content[end:]
    return content


def fallback_import(lcsc_id, description, in_stock):
    """
    Generate a minimal KiCad symbol for a part that easyeda2kicad couldn't find.

    Steps:
      1. Query LCSC Pro API for part metadata (MPN, package, category, stock).
      2. Determine component type (C / R / L / D / Q / U …).
      3. Map package → KiCad standard library footprint.
      4. Build a proper (symbol …) block and insert it into the user's library.
    """
    part = _fetch_lcsc_part_info(lcsc_id)
    if not part:
        return {
            "success": False,
            "error": f"Part {lcsc_id} not found in LCSC database either.",
        }

    mpn          = part["mpn"] or lcsc_id
    category     = part["category"]
    package      = part["package"]
    manufacturer = part["manufacturer"]
    lcsc_url     = part["lcsc_url"]
    api_stock    = part["stock"]
    device_desc  = part.get("device_desc", "")
    price_str    = part.get("price_str", "")
    jlcpcb_class = part.get("jlcpcb_class", "")

    # Description priority (Key Attributes → Description):
    #   1. EasyEDA Pro device_info Key Attributes string
    #   2. Scraped from browser (JLCPCB row / LCSC page key attributes)
    #   3. Manufacturer + MPN as last resort
    desc  = device_desc or description or f"{manufacturer} {mpn}".strip()
    desc  = _desc_with_part(desc, lcsc_id)
    stock = in_stock or api_stock

    ref, _prefix  = _detect_component_type(category, mpn)
    footprint     = _guess_footprint(ref, package)

    lib_path = _default_lib_path()
    if not os.path.isfile(lib_path):
        return {"success": False, "error": f"Symbol library not found: {lib_path}"}

    # ----- Build the symbol entry -----

    today     = datetime.now()
    today_str = f"{today.strftime('%b')} {today.day}, {today.year}"

    def esc(s):
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')

    # Properties after Footprint/Datasheet/LCSC Part
    extra = ""
    if desc:
        extra += _prop_kicad7("Description", desc, indent="    ")
    if stock:
        extra += _prop_kicad7("In Stock", str(stock), indent="    ")
    if price_str:
        extra += _prop_kicad7("Unit Price", price_str, indent="    ")
    if jlcpcb_class:
        extra += _prop_kicad7("JLCPCB Class", jlcpcb_class, indent="    ")
    extra += _prop_kicad7("Stock Update Date", today_str, indent="    ")

    shape = _make_symbol_shape(mpn, ref)

    symbol_entry = (
        f'  (symbol "{esc(mpn)}"\n'
        f'    (in_bom yes)\n'
        f'    (on_board yes)\n'
        f'    (property\n'
        f'      "Reference"\n'
        f'      "{ref}"\n'
        f'      (id 0)\n'
        f'      (at 0 5.08 0)\n'
        f'      (effects (font (size 1.27 1.27) ) )\n'
        f'    )\n'
        f'    (property\n'
        f'      "Value"\n'
        f'      "{esc(mpn)}"\n'
        f'      (id 1)\n'
        f'      (at 0 -5.08 0)\n'
        f'      (effects (font (size 1.27 1.27) ) )\n'
        f'    )\n'
        f'    (property\n'
        f'      "Footprint"\n'
        f'      "{esc(footprint)}"\n'
        f'      (id 2)\n'
        f'      (at 0 -7.62 0)\n'
        f'      (effects (font (size 1.27 1.27) ) hide)\n'
        f'    )\n'
        f'    (property\n'
        f'      "Datasheet"\n'
        f'      "{esc(lcsc_url)}"\n'
        f'      (id 3)\n'
        f'      (at 0 -10.16 0)\n'
        f'      (effects (font (size 1.27 1.27) ) hide)\n'
        f'    )\n'
        f'    (property\n'
        f'      "JLCPCB Part #"\n'
        f'      "{esc(lcsc_id)}"\n'
        f'      (id 5)\n'
        f'      (at 0 -12.70 0)\n'
        f'      (effects (font (size 1.27 1.27) ) hide)\n'
        f'    )\n'
        f'{extra}'
        f'{shape}'
        f'  )\n'
    )

    # ----- Read, patch, write the library -----

    try:
        with open(lib_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        return {"success": False, "error": f"Could not read library: {exc}"}

    # Remove any existing entry for this MPN (overwrite)
    content = _remove_existing_symbol(content, mpn)

    # Insert before the library's closing paren
    close_pos = content.rfind(")")
    if close_pos == -1:
        return {"success": False, "error": "Library file is malformed (no closing paren)."}

    content = content[:close_pos] + symbol_entry + content[close_pos:]

    try:
        with open(lib_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        return {"success": False, "error": f"Could not write library: {exc}"}

    fp_note = f"Footprint: {footprint}" if footprint else "Footprint: not assigned (assign manually in KiCad)"
    return {
        "success":     True,
        "symbol_name": mpn,
        "lib_path":    lib_path,
        "fallback":    True,
        "stdout":      "",
        "stderr": (
            f"[fallback] Part {lcsc_id} is not in EasyEDA's database.\n"
            f"Generated minimal symbol from LCSC API.\n"
            f"Category: {category}  Package: {package}\n"
            f"{fp_note}"
        ),
    }


# ---------------------------------------------------------------------------
# Shared single-part import (used by /import and /update-all)
# ---------------------------------------------------------------------------

def _desc_with_part(desc, lcsc_id):
    """Ensure the LCSC/JLCPCB part number is present in the Description text."""
    desc = (desc or "").strip()
    if lcsc_id and lcsc_id not in desc:
        desc = f"{desc} | {lcsc_id}".lstrip(" |").strip()
    return desc


def _dedupe_lcsc_orphans(lib_path):
    """
    Remove orphaned duplicate symbols that share an LCSC/JLCPCB part number.

    When EasyEDA renames a part's symbol between imports (e.g.
    "XT90PW-F.G.Y" -> "XT90PW-F30.G.Y"), a plain re-import leaves the old symbol
    behind because the name-based dedup can't match it.  Here we group all
    top-level symbols by their part number and keep only the freshest (the one
    that carries a Stock Update Date, else the last one in the file).
    """
    try:
        with open(lib_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return

    entries = []
    for name, line_start, paren, end in _iter_top_symbols(content):
        props = _sym_properties(content[paren:end])
        lcsc  = _lcsc_of(props)
        if lcsc:
            entries.append({"name": name, "start": line_start, "end": end,
                            "lcsc": lcsc, "dated": bool(props.get("Stock Update Date"))})

    groups = {}
    for e in entries:
        groups.setdefault(e["lcsc"], []).append(e)

    remove = []
    for es in groups.values():
        if len(es) <= 1:
            continue
        keep = max(es, key=lambda e: (e["dated"], e["start"]))
        remove.extend(e for e in es if e is not keep)

    if not remove:
        return

    for e in sorted(remove, key=lambda e: e["start"], reverse=True):
        s, en = e["start"], e["end"]
        while en < len(content) and content[en] == "\n":
            en += 1
        content = content[:s] + content[en:]

    with open(lib_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[easyeda2kicad-server] removed {len(remove)} orphaned duplicate symbol(s)", flush=True)


def _import_one(lcsc_id, description="", in_stock=""):
    """Library-locked wrapper around _import_one_unlocked (serialises writes)."""
    with _LIB_LOCK:
        return _import_one_unlocked(lcsc_id, description, in_stock)


def _import_one_unlocked(lcsc_id, description="", in_stock=""):
    """
    Run easyeda2kicad for a single part, deduplicate, and patch the stock/price/
    class/description fields.  Falls back to the LCSC-API symbol generator when
    easyeda2kicad can't find the part.  Returns a result dict.

    The Description field is always sourced from the part's Key Attributes
    (the EasyEDA Pro `device_info` description) when available, falling back to
    whatever the browser scraped.
    """
    if not get_easyeda2kicad():
        return {"success": False, "error": "easyeda2kicad not found in PATH"}

    # Without read access the cleanup after the import can't run, which would
    # leave duplicate symbols behind, so stop before touching the library.
    default_lib = _default_lib_path()
    if os.path.isfile(default_lib):
        try:
            _read_lib(default_lib)
        except PermissionError:
            return {
                "success": False,
                "error": (
                    "No access to your KiCad library. Grant Python Full Disk "
                    "Access (see README.md), then try again."
                ),
            }

    try:
        result = subprocess.run(
            [EASYEDA2KICAD, "--full", "--overwrite", f"--lcsc_id={lcsc_id}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "command timed out"}

    success     = result.returncode == 0
    symbol_name = parse_symbol_name(result.stderr)
    lib_path    = parse_lib_path(result.stderr)

    if success and symbol_name and lib_path:
        # Normal path: easyeda2kicad succeeded.
        # Deduplicate first (--overwrite appends, doesn't remove old entry).
        _dedup_symbol(lib_path, symbol_name)

        price        = ""
        jlcpcb_class = ""
        manufacturer = ""
        mpn          = ""
        part_info = _fetch_lcsc_part_info(lcsc_id)
        if part_info:
            # Key Attributes → Description: always prefer the API's key-attribute
            # string over the browser scrape when we have it.
            if part_info.get("device_desc"):
                description = part_info["device_desc"]
            # API stock is always accurate — browser stock is unreliable.
            if part_info.get("stock"):
                in_stock = str(part_info["stock"])
            price        = part_info.get("price_str", "")
            jlcpcb_class = part_info.get("jlcpcb_class", "")
            manufacturer = part_info.get("manufacturer", "")
            mpn          = part_info.get("mpn", "")

        # Always keep the part number in the Description too.
        description = _desc_with_part(description, lcsc_id)
        add_symbol_fields(lib_path, symbol_name, description, in_stock, price, jlcpcb_class,
                          manufacturer, mpn)
        # Clean up any orphan left behind if EasyEDA renamed this part's symbol.
        _dedupe_lcsc_orphans(lib_path)
        return {
            "success":     True,
            "symbol_name": symbol_name,
            "lib_path":    lib_path,
            "stdout":      result.stdout,
            "stderr":      result.stderr,
        }

    combined = (result.stdout or "") + (result.stderr or "")
    # Only a genuine import/dependency failure means the install itself is broken.
    # A part-specific traceback (unsupported symbol, odd 3D model, etc.) is NOT —
    # it should fall through to the LCSC-API fallback generator below.
    if "ModuleNotFoundError" in combined or "No module named" in combined:
        return {
            "success": False,
            "error": (
                "easyeda2kicad is installed but broken. Reinstall it, "
                "then restart the browser-extension server."
            ),
            "stderr": combined,
        }

    # easyeda2kicad failed — part likely not in EasyEDA's database.
    # Try generating a minimal symbol directly from the LCSC Pro API.
    print(
        f"[easyeda2kicad-server] easyeda2kicad failed for {lcsc_id}, "
        f"trying fallback import…",
        flush=True,
    )
    fb = fallback_import(lcsc_id, description, in_stock)
    if fb.get("success"):
        # Same orphan cleanup as the easyeda2kicad path (was previously missing
        # here, which let update-all accumulate duplicate symbols).
        _dedupe_lcsc_orphans(fb.get("lib_path") or _default_lib_path())
    else:
        fb["stderr"] = result.stderr + "\n\nFallback error: " + fb.get("error", "")
    return fb


# ---------------------------------------------------------------------------
# Bulk update helpers
# ---------------------------------------------------------------------------

# Older imports used the field name "LCSC Part"; newer ones use "JLCPCB Part #".
# Match both so an existing library stays discoverable by Update All.
_PART_FIELD_RE = r'(?:LCSC Part|JLCPCB Part #)'


def _read_lib(lib_path):
    """
    Read the whole library file under the library lock so a reader never
    observes a half-written file mid-import (which parses to 0 symbols).
    Re-raises PermissionError (macOS TCC) so callers can surface it.
    """
    with _LIB_LOCK:
        try:
            with open(lib_path, "r", encoding="utf-8") as f:
                return f.read()
        except PermissionError:
            _restart_for_permission_error()
            raise


def _restart_for_permission_error():
    """
    Exit so launchd starts a fresh server.  macOS can keep denying a process
    that started at login even once its Full Disk Access is in effect; a new
    process gets the correct answer.  Waits a moment so the current request
    can still report the error.
    """
    if time.time() - _START_TIME > 30:
        threading.Timer(1.0, os._exit, args=(1,)).start()


def _find_lcsc_symbols(lib_path):
    """
    Return a list of (symbol_name, lcsc_id) for every top-level symbol in the
    library that carries an LCSC part-number property (either the legacy
    "LCSC Part" field or the current "JLCPCB Part #" field).
    """
    try:
        content = _read_lib(lib_path)
    except Exception:
        return []

    results  = []
    lcsc_pat = re.compile(r'\(property\s+"' + _PART_FIELD_RE + r'"\s+"(C\d+)"')

    for sym_name, line_start, paren, end in _iter_top_symbols(content):
        lcsc_m = lcsc_pat.search(content[paren:end])
        if lcsc_m:
            results.append((sym_name, lcsc_m.group(1)))

    return results


def _get_status(lib_path):
    """Return a dict describing server + library health."""
    status = {
        "success":       True,
        "server":        "ok",
        "library_found": False,
        "lib_path":      lib_path,
        "total_symbols": 0,
        "lcsc_symbols":  0,
        "last_modified": "",
    }
    if not os.path.isfile(lib_path):
        return status

    status["library_found"] = True
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(lib_path))
        status["last_modified"] = (
            f"{mtime.strftime('%b')} {mtime.day}, {mtime.year} "
            f"{mtime.strftime('%H:%M')}"
        )
    except Exception:
        pass

    try:
        content = _read_lib(lib_path)
        status["total_symbols"] = sum(1 for _ in _iter_top_symbols(content))
    except PermissionError:
        # macOS TCC blocks background launchd agents from reading ~/Documents
        # until the agent is granted Full Disk Access.
        status["error"] = "permission_denied"
        status["hint"]  = ("This server can't read your KiCad library. Grant Full Disk "
                           "Access to the Python running it (System Settings › Privacy & "
                           "Security › Full Disk Access), then restart the server.")
    except Exception:
        pass

    status["lcsc_symbols"] = len(_find_lcsc_symbols(lib_path))
    return status


def _update_progress():
    """Snapshot of the background Update All job."""
    with _UPDATE_LOCK:
        s = dict(_UPDATE_STATE)
    s["errors"] = s["errors"][-25:]
    s["success"] = True
    return s


def _refresh_fields_one(lib_path, symbol_name, lcsc_id):
    """Refresh stock, price, class, description, Manufacturer and MPN on one symbol, in place.
    Never touches the symbol drawing, the footprint or the 3D model."""
    info = _fetch_lcsc_part_info(lcsc_id)
    if not info:
        return {"success": False, "error": "no data from LCSC"}
    with _LIB_LOCK:
        add_symbol_fields(lib_path, symbol_name,
                          _desc_with_part(info.get("device_desc", ""), lcsc_id),
                          info.get("stock", ""), info.get("price_str", ""),
                          info.get("jlcpcb_class", ""), info.get("manufacturer", ""),
                          info.get("mpn", ""))
    return {"success": True}


def _run_update_all(lib_path, symbols):
    """Background worker for "Update All", updating progress state.

    By default it only refreshes fields (stock, price, Manufacturer, MPN, ...). A full
    re-import ("update_all": "reimport" in ~/.easyeda2kicad/config.json) regenerates every
    symbol and footprint, which resets hand-tuned 3D model positions and can drop symbols
    whose names changed on EasyEDA, so it's opt-in.
    """
    reimport = _user_config().get("update_all") == "reimport"
    for symbol_name, lcsc_id in symbols:
        with _UPDATE_LOCK:
            _UPDATE_STATE["current"] = f"{lcsc_id} ({symbol_name})"
        try:
            res = _import_one(lcsc_id) if reimport else _refresh_fields_one(lib_path, symbol_name, lcsc_id)
            if not reimport:
                time.sleep(0.5)   # be gentle with LCSC's servers
        except Exception as exc:
            res = {"success": False, "error": str(exc)}
        with _UPDATE_LOCK:
            _UPDATE_STATE["done"] += 1
            if res.get("success"):
                _UPDATE_STATE["updated"] += 1
                verb = "re-imported" if reimport else "refreshed fields of"
                print(f"[easyeda2kicad-server] update-all: {verb} {lcsc_id} ({symbol_name})", flush=True)
            else:
                _UPDATE_STATE["failed"] += 1
                _UPDATE_STATE["errors"].append(f"{lcsc_id}: {res.get('error', 'import failed')}")
                print(f"[easyeda2kicad-server] update-all: FAILED {lcsc_id}: {res.get('error', '')}", flush=True)
    # Final safety sweep after a re-import: remove orphan duplicates left by renamed symbols.
    if reimport:
        try:
            with _LIB_LOCK:
                _dedupe_lcsc_orphans(lib_path)
        except Exception as exc:
            print(f"[easyeda2kicad-server] update-all: final dedup failed: {exc}", flush=True)
    with _UPDATE_LOCK:
        _UPDATE_STATE["running"] = False
        _UPDATE_STATE["current"] = ""
        _UPDATE_STATE["finished_at"] = time.time()
    print("[easyeda2kicad-server] update-all: finished", flush=True)


def _start_update_all(lib_path):
    """
    Kick off a full re-import of every LCSC part as a background thread and return
    immediately.  The dashboard/extension poll /update-progress for live status.
    By default this refreshes each part's fields (stock, price, class, description,
    Manufacturer, MPN) in place; see _run_update_all for the opt-in full re-import.
    """
    if not get_easyeda2kicad():
        return {"success": False, "error": "easyeda2kicad not found in PATH"}
    if not os.path.isfile(lib_path):
        return {"success": False, "error": f"Library not found: {lib_path}"}

    with _UPDATE_LOCK:
        if _UPDATE_STATE["running"]:
            snap = dict(_UPDATE_STATE)
            snap["success"] = True
            snap["already_running"] = True
            return snap

    try:
        symbols = _find_lcsc_symbols(lib_path)
    except PermissionError:
        return {"success": False, "error": "permission_denied",
                "hint": "Grant Full Disk Access to the Python running this server."}

    # Collapse duplicate LCSC ids (orphans) so each part is re-imported once.
    seen, uniq = set(), []
    for name, lcsc in symbols:
        if lcsc not in seen:
            seen.add(lcsc)
            uniq.append((name, lcsc))
    symbols = uniq

    if not symbols:
        return {"success": True, "started": False, "total": 0,
                "message": "No LCSC symbols found."}

    with _UPDATE_LOCK:
        _UPDATE_STATE.update(running=True, total=len(symbols), done=0, updated=0,
                             failed=0, current="", errors=[],
                             started_at=time.time(), finished_at=None)

    threading.Thread(target=_run_update_all, args=(lib_path, symbols), daemon=True).start()
    return {"success": True, "started": True, "total": len(symbols)}


# ---------------------------------------------------------------------------
# Library parts listing (for the dashboard "monitor parts" view)
# ---------------------------------------------------------------------------

def _unescape_sexpr(s):
    """Undo KiCad s-expression string escaping ( \\" -> " , \\\\ -> \\ )."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _sym_properties(block):
    """Return {name: value} for every (property "name" "value" ...) in a block."""
    props = {}
    pat = re.compile(r'\(property\s+"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')
    for m in pat.finditer(block):
        k = _unescape_sexpr(m.group(1))
        v = _unescape_sexpr(m.group(2))
        if k not in props:
            props[k] = v
    return props


def _list_library_parts(lib_path):
    """
    Return a list of dicts describing every top-level symbol in the library
    that carries an LCSC/JLCPCB part number, with its tracked fields.
    """
    try:
        content = _read_lib(lib_path)
    except Exception:
        return []

    parts   = []
    for name, line_start, paren, end in _iter_top_symbols(content):
        props = _sym_properties(content[paren:end])
        lcsc  = _lcsc_of(props)
        if not lcsc:
            continue
        fp = props.get("Footprint", "")
        parts.append({
            "name":        name,
            "lcsc":        lcsc,
            "value":       props.get("Value", name),
            "description": props.get("Description", ""),
            "footprint":   fp.split(":", 1)[1] if ":" in fp else fp,
            "stock":       props.get("In Stock", ""),
            "price":       props.get("Unit Price", ""),
            "jlcpcb_class": props.get("JLCPCB Class", ""),
            "updated":     props.get("Stock Update Date", ""),
            "datasheet":   props.get("Datasheet", ""),
        })

    parts.sort(key=lambda p: p["name"].lower())
    return parts


# ---------------------------------------------------------------------------
# KiCad project → JLCPCB SMT BOM
# ---------------------------------------------------------------------------

_LCSC_ID_RE   = re.compile(r'^C\d{3,}$')
# Property names that may hold an LCSC/JLCPCB part number, in priority order.
_PART_FIELD_NAMES = ["JLCPCB Part #", "LCSC Part #", "LCSC Part", "LCSC"]


def _match_block(text, open_idx):
    """Given the index of a '(', return the index just past its matching ')'."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == '\\':
                    i += 1
                i += 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _strip_lib_symbols(text):
    """Remove the (lib_symbols ...) cache so only placed instances remain."""
    m = re.search(r'\(lib_symbols\b', text)
    if not m:
        return text
    return text[:m.start()] + text[_match_block(text, m.start()):]


def _lcsc_of(props):
    """Pick the LCSC part number out of a symbol's properties."""
    for name in _PART_FIELD_NAMES:
        v = (props.get(name) or "").strip()
        if _LCSC_ID_RE.match(v):
            return v
    for v in props.values():
        v = (v or "").strip()
        if _LCSC_ID_RE.match(v):
            return v
    return ""


def _instance_refs(block, ref_prop):
    """Actual designators from the (instances ...) sub-block, else the Reference property."""
    refs = []
    im = re.search(r'\(instances\b', block)
    if im:
        inst = block[im.start():_match_block(block, im.start())]
        refs = re.findall(r'\(reference\s+"([^"]+)"', inst)
    if not refs and ref_prop:
        refs = [ref_prop]
    seen, out = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _natkey(ref):
    m = re.match(r'([A-Za-z_]+)(\d+)', ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def _parse_project_components(project_path):
    """Parse every placed component out of a KiCad project's schematics."""
    proj_dir = os.path.dirname(project_path) if os.path.isfile(project_path) else project_path
    sch_files = sorted(glob.glob(os.path.join(proj_dir, "*.kicad_sch")))

    comps = []
    for sch in sch_files:
        try:
            with open(sch, encoding="utf-8") as f:
                text = _strip_lib_symbols(f.read())
        except Exception:
            continue
        for m in re.finditer(r'(?m)^[ \t]+(\(symbol)\b', text):
            block = text[m.start(1):_match_block(text, m.start(1))]
            libm  = re.search(r'\(lib_id\s+"([^"]+)"', block)
            lib_id = libm.group(1) if libm else ""
            if lib_id.startswith("power:"):
                continue
            props = _sym_properties(block)
            refs  = [r for r in _instance_refs(block, props.get("Reference", ""))
                     if not r.startswith("#")]
            if not refs:
                continue
            fp = props.get("Footprint", "")
            comps.append({
                "refs":      refs,
                "value":     props.get("Value", ""),
                "footprint": fp.split(":", 1)[1] if ":" in fp else fp,
                "lcsc":      _lcsc_of(props),
                "dnp":       "(dnp yes)" in block,
                "in_bom":    "(in_bom no)" not in block,
            })
    return comps


def _build_bom(project_path, include_dnp=False):
    """Return grouped JLCPCB BOM rows for a KiCad project."""
    comps  = _parse_project_components(project_path)
    groups = {}
    for c in comps:
        if not c["in_bom"] or (c["dnp"] and not include_dnp) or not c["footprint"]:
            continue
        key = c["lcsc"] or f'{c["value"]}|{c["footprint"]}'
        g = groups.setdefault(key, {"comment": c["value"], "footprint": c["footprint"],
                                    "lcsc": c["lcsc"], "refs": []})
        g["refs"].extend(c["refs"])

    rows = []
    for g in groups.values():
        refs = sorted(set(g["refs"]), key=_natkey)
        rows.append({
            "comment":    g["comment"],
            "designator": ",".join(refs),
            "footprint":  g["footprint"],
            "lcsc":       g["lcsc"],
            "qty":        len(refs),
        })
    # LCSC-less rows sink to the bottom; otherwise sort by first designator.
    rows.sort(key=lambda r: (r["lcsc"] == "", _natkey(r["designator"].split(",")[0])))
    return rows


BOM_HEADERS = ["Comment", "Designator", "Footprint", "JLCPCB Part #（optional）"]


def _bom_csv(rows):
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(BOM_HEADERS)
    for r in rows:
        w.writerow([r["comment"], r["designator"], r["footprint"], r["lcsc"]])
    return buf.getvalue()


def _bom_xlsx(rows):
    """Return xlsx bytes matching the JLCPCB SMT BOM template, or None if openpyxl missing."""
    try:
        import openpyxl
        from openpyxl.styles import Font
    except Exception:
        return None
    import io
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(BOM_HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r["comment"], r["designator"], r["footprint"], r["lcsc"]])
    widths = [26, 30, 34, 18]
    for i, wdt in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = wdt
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sanitize_filename(name):
    return re.sub(r'[^A-Za-z0-9._-]+', "_", name).strip("_") or "bom"


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

def _list_projects():
    """Find KiCad projects (.kicad_pro) under the user's common project folders."""
    configured = _user_config().get("projects_dir")
    roots = ([os.path.expanduser(configured)] if configured else []) + [
        os.path.expanduser("~/Documents/KiCad/projects"),
        os.path.expanduser("~/Documents/Kicad/projects"),
        os.path.expanduser("~/Documents/KiCad"),
        os.path.expanduser("~/Documents/Kicad"),
    ]
    found, seen = [], set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for pro in glob.glob(os.path.join(root, "**", "*.kicad_pro"), recursive=True):
            rp = os.path.realpath(pro)
            if rp in seen:
                continue
            seen.add(rp)
            home = os.path.expanduser("~")
            reldir = os.path.dirname(rp)
            try:
                reldir = os.path.relpath(reldir, home)
            except Exception:
                pass
            found.append({
                "name":   os.path.splitext(os.path.basename(pro))[0],
                "folder": reldir,
                "path":   pro,
            })
    found.sort(key=lambda p: (p["name"].lower(), p["folder"].lower()))
    return found


def _safe_under_home(path):
    """Guard against path traversal: only allow reads under the user's home dir."""
    home = os.path.realpath(os.path.expanduser("~"))
    rp   = os.path.realpath(os.path.expanduser(path or ""))
    return rp == home or rp.startswith(home + os.sep), rp


# ---------------------------------------------------------------------------
# Dashboard web page
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>easyeda2kicad — Dashboard</title>
<style>
  :root {
    --green:#2d6a4f; --green-d:#1b4332; --blue:#1a5276; --red:#9b2226;
    --bg:#f4f6f5; --card:#fff; --line:#e3e8e6; --muted:#6b7772; --text:#1a1f1d;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg);
         color:var(--text); font-size:14px; line-height:1.5; }
  header { background:var(--green); color:#fff; padding:18px 24px; display:flex;
           align-items:center; gap:14px; box-shadow:0 1px 4px rgba(0,0,0,.15); }
  header h1 { font-size:18px; font-weight:700; letter-spacing:.01em; }
  header .dot { width:9px; height:9px; border-radius:50%; background:#f1c40f; }
  header .dot.online { background:#2ecc71; } header .dot.offline { background:#e74c3c; }
  header .srv { font-size:12px; opacity:.85; }
  main { max-width:1180px; margin:22px auto; padding:0 20px; display:flex; flex-direction:column; gap:22px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          box-shadow:0 1px 2px rgba(0,0,0,.04); overflow:hidden; }
  .card h2 { font-size:14px; font-weight:700; padding:13px 18px; border-bottom:1px solid var(--line);
             display:flex; align-items:center; gap:10px; }
  .card h2 .count { color:var(--muted); font-weight:600; font-size:12px; }
  .card .body { padding:16px 18px; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px; background:var(--line); }
  .stats .cell { background:var(--card); padding:14px 18px; }
  .stats .cell .k { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
  .stats .cell .v { font-size:19px; font-weight:700; margin-top:3px; }
  .v.online { color:var(--green); } .v.offline { color:var(--red); }
  button { font-family:inherit; font-size:13px; font-weight:600; border:none; border-radius:6px;
           padding:8px 14px; cursor:pointer; transition:background .15s, opacity .15s; }
  button.primary { background:var(--green); color:#fff; } button.primary:hover { background:var(--green-d); }
  button.ghost { background:#eef2f0; color:var(--green-d); } button.ghost:hover { background:#e2e8e5; }
  button.mini { padding:4px 10px; font-size:12px; }
  button:disabled { opacity:.55; cursor:default; }
  input, select { font-family:inherit; font-size:13px; padding:8px 10px; border:1px solid #cfd6d3;
                  border-radius:6px; background:#fff; color:var(--text); outline:none; }
  input:focus, select:focus { border-color:var(--green); }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .grow { flex:1; min-width:120px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); background:#fafbfb;
       position:sticky; top:0; z-index:1; }
  tbody tr:hover { background:#f7faf8; }
  .scroll { max-height:460px; overflow:auto; }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; }
  .pill.warn { background:#fdecea; color:var(--red); } .pill.ok { background:#e6f4ec; color:var(--green-d); }
  .mono { font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:12px; }
  .msg { font-size:12px; color:var(--muted); min-height:16px; }
  .msg.ok { color:var(--green-d); } .msg.err { color:var(--red); }
  a.dl { text-decoration:none; }
  .muted { color:var(--muted); }
  .desc { max-width:340px; color:var(--muted); font-size:12px; }
  .nowrap { white-space:nowrap; }
</style>
</head>
<body>
<header>
  <span class="dot" id="srv-dot"></span>
  <h1>easyeda2kicad</h1>
  <span class="srv" id="srv-text">connecting…</span>
</header>

<main>
  <!-- Library status -->
  <section class="card">
    <h2>Library Status
      <button class="ghost mini" style="margin-left:auto" onclick="loadStatus()">↻ Refresh</button>
    </h2>
    <div class="stats" id="stats"><div class="cell"><div class="k">Loading</div><div class="v">…</div></div></div>
  </section>

  <!-- Parts monitor -->
  <section class="card">
    <h2>Parts <span class="count" id="parts-count"></span>
      <button class="primary mini" style="margin-left:auto" id="btn-update-all" onclick="updateAll()">Update All Parts</button>
    </h2>
    <div class="body">
      <div class="row" style="margin-bottom:12px">
        <input class="grow" id="parts-filter" placeholder="Filter by name, value, LCSC #, description…" oninput="renderParts()">
        <span class="msg" id="update-msg"></span>
      </div>
      <div class="scroll">
        <table>
          <thead><tr>
            <th>Name</th><th>Value</th><th>JLCPCB Part #</th><th>Footprint</th>
            <th class="nowrap">In Stock</th><th>Price</th><th>Class</th><th class="nowrap">Updated</th><th></th>
          </tr></thead>
          <tbody id="parts-body"><tr><td colspan="9" class="muted">Loading…</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- BOM -->
  <section class="card">
    <h2>JLCPCB SMT BOM</h2>
    <div class="body">
      <div class="row" style="margin-bottom:8px">
        <select class="grow" id="bom-project"><option value="">Loading projects…</option></select>
        <button class="ghost mini" onclick="loadProjects()">↻</button>
      </div>
      <div class="row" style="margin-bottom:12px">
        <input class="grow mono" id="bom-path" placeholder="…or paste a .kicad_pro / project folder path">
        <label class="muted" style="font-size:12px"><input type="checkbox" id="bom-dnp"> include DNP</label>
        <button class="primary" onclick="genBom()">Generate BOM</button>
      </div>
      <div class="msg" id="bom-msg"></div>
      <div id="bom-summary" style="margin:8px 0; display:none" class="row">
        <span id="bom-stats" class="muted"></span>
        <button class="ghost mini" onclick="dlBom('xlsx')">⬇ Download .xlsx</button>
        <button class="ghost mini" onclick="dlBom('csv')">⬇ Download .csv</button>
      </div>
      <div class="scroll" id="bom-wrap" style="display:none">
        <table>
          <thead><tr><th>Comment</th><th>Designator</th><th>Footprint</th><th>JLCPCB Part #</th><th class="nowrap">Qty</th></tr></thead>
          <tbody id="bom-body"></tbody>
        </table>
      </div>
    </div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let PARTS = [];
let BOM_QUERY = "";

function dlBom(fmt){
  if (!BOM_QUERY) return;
  const a = document.createElement("a");
  a.href = "/bom." + fmt + "?" + BOM_QUERY;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function esc(s){ return (s==null?"":String(s)).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

// ── status ──────────────────────────────────────────────────────────────
async function loadStatus(){
  try {
    const d = await (await fetch("/status")).json();
    $("srv-dot").className = "dot online"; $("srv-text").textContent = "server online · " + (d.lib_path||"");
    if (d.error === "permission_denied") {
      $("stats").innerHTML = `<div class="cell" style="grid-column:1/-1">
        <div class="k" style="color:var(--red)">⚠ No access to your library</div>
        <div style="margin-top:6px;font-size:13px">${esc(d.hint||"")}</div></div>`;
      return;
    }
    $("stats").innerHTML = [
      ["Server", "Online", "online"],
      ["Library", d.library_found ? "Found":"Not found", d.library_found?"":"offline"],
      ["Total Symbols", d.total_symbols||0, ""],
      ["Tracked Parts", d.lcsc_symbols||0, ""],
      ["Last Modified", d.last_modified||"—", ""],
    ].map(([k,v,c])=>`<div class="cell"><div class="k">${k}</div><div class="v ${c}">${esc(v)}</div></div>`).join("");
  } catch(e){
    $("srv-dot").className="dot offline"; $("srv-text").textContent="server offline";
    $("stats").innerHTML = `<div class="cell"><div class="k">Server</div><div class="v offline">Offline</div></div>`;
  }
}

// ── parts ───────────────────────────────────────────────────────────────
async function loadParts(){
  try {
    const d = await (await fetch("/parts")).json();
    PARTS = d.parts||[]; renderParts();
  } catch(e){ $("parts-body").innerHTML = `<tr><td colspan="9" class="muted">Could not load parts.</td></tr>`; }
}
function renderParts(){
  const q = $("parts-filter").value.trim().toLowerCase();
  const rows = PARTS.filter(p => !q ||
    (p.name+" "+p.value+" "+p.lcsc+" "+p.description).toLowerCase().includes(q));
  $("parts-count").textContent = `(${rows.length}${q?" of "+PARTS.length:""})`;
  if (!rows.length){ $("parts-body").innerHTML = `<tr><td colspan="9" class="muted">No parts.</td></tr>`; return; }
  $("parts-body").innerHTML = rows.map(p => `
    <tr data-lcsc="${esc(p.lcsc)}">
      <td class="mono">${esc(p.name)}<div class="desc">${esc(p.description)}</div></td>
      <td>${esc(p.value)}</td>
      <td class="mono">${esc(p.lcsc)}</td>
      <td class="mono muted">${esc(p.footprint)}</td>
      <td class="nowrap">${esc(p.stock||"—")}</td>
      <td class="nowrap">${esc(p.price||"—")}</td>
      <td>${esc(p.jlcpcb_class||"—")}</td>
      <td class="nowrap muted">${esc(p.updated||"—")}</td>
      <td><button class="ghost mini" onclick="updateOne(this,'${esc(p.lcsc)}')">Update</button></td>
    </tr>`).join("");
}
async function updateOne(btn, lcsc){
  btn.disabled = true; btn.textContent = "…";
  try {
    const d = await (await fetch("/import?lcsc_id="+encodeURIComponent(lcsc))).json();
    btn.textContent = d.success ? "✓" : "✗";
    if (d.success){ await loadParts(); await loadStatus(); }
  } catch(e){ btn.textContent = "✗"; }
  setTimeout(()=>{ btn.disabled=false; btn.textContent="Update"; }, 1500);
}
let UPDATE_POLL = null;

async function updateAll(){
  const btn = $("btn-update-all"), msg = $("update-msg");
  btn.disabled = true; btn.textContent = "Updating…";
  msg.className = "msg"; msg.textContent = "Starting…";
  try {
    const d = await (await fetch("/update-all")).json();
    if (!d.success){ msg.className="msg err"; msg.textContent = "✗ "+(d.error||"failed"); btn.disabled=false; btn.textContent="Update All Parts"; return; }
    if (d.started === false){ msg.className="msg ok"; msg.textContent = d.message||"Nothing to update"; btn.disabled=false; btn.textContent="Update All Parts"; return; }
    pollUpdate();  // begin polling progress
  } catch(e){
    msg.className="msg err"; msg.textContent="✗ server offline";
    btn.disabled=false; btn.textContent="Update All Parts";
  }
}

function pollUpdate(){
  const btn = $("btn-update-all"), msg = $("update-msg");
  if (UPDATE_POLL) clearInterval(UPDATE_POLL);
  const tick = async () => {
    let p;
    try { p = await (await fetch("/update-progress")).json(); }
    catch(e){ return; }
    const pct = p.total ? Math.round(100*p.done/p.total) : 0;
    if (p.running){
      btn.disabled = true; btn.textContent = `Updating ${p.done}/${p.total}`;
      msg.className = "msg";
      msg.textContent = `${pct}% · ${p.done}/${p.total} done` +
        (p.failed?` · ${p.failed} failed`:"") + (p.current?` · now: ${p.current}`:"");
    } else {
      clearInterval(UPDATE_POLL); UPDATE_POLL = null;
      btn.disabled = false; btn.textContent = "Update All Parts";
      if (p.total){
        msg.className = "msg ok";
        msg.textContent = `✓ Updated ${p.updated}/${p.total}` + (p.failed?` (${p.failed} failed)`:"");
      } else { msg.className="msg"; msg.textContent=""; }
      await loadParts(); await loadStatus();
    }
  };
  tick();
  UPDATE_POLL = setInterval(tick, 2000);
}

// If a job is already running when the page loads, resume showing progress.
async function resumeUpdateIfRunning(){
  try {
    const p = await (await fetch("/update-progress")).json();
    if (p.running) pollUpdate();
  } catch(e){}
}

// ── projects + BOM ──────────────────────────────────────────────────────
async function loadProjects(){
  try {
    const d = await (await fetch("/projects")).json();
    const sel = $("bom-project");
    sel.innerHTML = `<option value="">— select a KiCad project —</option>` +
      (d.projects||[]).map(p=>{
        const label = p.folder && p.folder!==p.name ? `${p.name} — ${p.folder}` : p.name;
        return `<option value="${esc(p.path)}">${esc(label)}</option>`;
      }).join("");
  } catch(e){ $("bom-project").innerHTML = `<option value="">Could not load projects</option>`; }
}
$("bom-project") && $("bom-project").addEventListener("change", e => { if (e.target.value) $("bom-path").value = e.target.value; });

async function genBom(){
  const project = ($("bom-path").value.trim() || $("bom-project").value);
  const dnp = $("bom-dnp").checked ? "1":"0";
  const msg = $("bom-msg");
  if (!project){ msg.className="msg err"; msg.textContent="Pick a project or paste a path first."; return; }
  msg.className="msg"; msg.textContent="Parsing schematics…";
  $("bom-summary").style.display="none"; $("bom-wrap").style.display="none";
  try {
    const url = "/bom?project="+encodeURIComponent(project)+"&include_dnp="+dnp;
    const d = await (await fetch(url)).json();
    if (!d.success){ msg.className="msg err"; msg.textContent="✗ "+(d.error||"failed"); return; }
    msg.className="msg ok"; msg.textContent = `✓ ${d.name}`;
    $("bom-stats").textContent = `${d.lines} lines · ${d.parts} parts` + (d.missing?` · ${d.missing} without LCSC #`:"");
    BOM_QUERY = "project="+encodeURIComponent(project)+"&include_dnp="+dnp;
    $("bom-body").innerHTML = d.rows.map(r=>`
      <tr>
        <td>${esc(r.comment)}</td>
        <td class="mono" style="max-width:300px">${esc(r.designator)}</td>
        <td class="mono muted">${esc(r.footprint)}</td>
        <td class="mono">${r.lcsc ? esc(r.lcsc) : '<span class="pill warn">missing</span>'}</td>
        <td>${r.qty}</td>
      </tr>`).join("");
    $("bom-summary").style.display="flex"; $("bom-wrap").style.display="block";
  } catch(e){ msg.className="msg err"; msg.textContent="✗ "+e.message; }
}

// ── init ────────────────────────────────────────────────────────────────
loadStatus(); loadParts(); loadProjects(); resumeUpdateIfRunning();
setInterval(loadStatus, 30000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/dashboard"):
            self._html(200, DASHBOARD_HTML)
            return

        if parsed.path == "/status":
            self._json(200, _get_status(_default_lib_path()))
            return

        if parsed.path == "/parts":
            self._json(200, {
                "success":  True,
                "lib_path": _default_lib_path(),
                "parts":    _list_library_parts(_default_lib_path()),
            })
            return

        if parsed.path == "/projects":
            self._json(200, {"success": True, "projects": _list_projects()})
            return

        if parsed.path == "/update-all":
            self._json(200, _start_update_all(_default_lib_path()))
            return

        if parsed.path == "/update-progress":
            self._json(200, _update_progress())
            return

        if parsed.path in ("/bom", "/bom.csv", "/bom.xlsx"):
            self._handle_bom(parsed.path, params)
            return

        if parsed.path != "/import":
            self.send_response(404)
            self.end_headers()
            return

        lcsc_id     = params.get("lcsc_id",     [None])[0]
        description = params.get("description", [""])[0]
        in_stock    = params.get("in_stock",    [""])[0]

        if not lcsc_id:
            self._json(400, {"success": False, "error": "missing lcsc_id"})
            return

        try:
            result = _import_one(lcsc_id, description, in_stock)
            # Surface the "broken install" case as a 500 like before.
            if not result.get("success") and "installed but broken" in result.get("error", ""):
                self._json(500, result)
            else:
                self._json(200, result)
        except Exception as exc:
            self._json(500, {"success": False, "error": str(exc)})

    def _handle_bom(self, path, params):
        project = params.get("project", [""])[0]
        include_dnp = params.get("include_dnp", ["0"])[0] in ("1", "true", "yes")

        if not project:
            self._json(400, {"success": False, "error": "missing project path"})
            return

        ok, real = _safe_under_home(project)
        if not ok:
            self._json(403, {"success": False, "error": "project path must be inside your home directory"})
            return
        if not os.path.exists(real):
            self._json(404, {"success": False, "error": f"project not found: {project}"})
            return

        try:
            rows = _build_bom(real, include_dnp=include_dnp)
        except Exception as exc:
            self._json(500, {"success": False, "error": f"BOM parse failed: {exc}"})
            return

        base = os.path.splitext(os.path.basename(real))[0] or "project"
        if os.path.isdir(real):
            base = os.path.basename(os.path.realpath(real)) or "project"
        fname = _sanitize_filename(f"BOM_{base}_JLCSMT")

        if path == "/bom.csv":
            self._download(_bom_csv(rows).encode("utf-8-sig"), fname + ".csv", "text/csv")
            return
        if path == "/bom.xlsx":
            data = _bom_xlsx(rows)
            if data is None:
                self._download(_bom_csv(rows).encode("utf-8-sig"), fname + ".csv", "text/csv")
                return
            self._download(data, fname + ".xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            return

        missing = sum(1 for r in rows if not r["lcsc"])
        self._json(200, {
            "success":   True,
            "project":   real,
            "name":      base,
            "rows":      rows,
            "lines":     len(rows),
            "parts":     sum(r["qty"] for r in rows),
            "missing":   missing,
            "xlsx":      _bom_xlsx([]) is not None,
        })

    def _json(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, code, body):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _download(self, data, filename, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"[easyeda2kicad-server] {fmt % args}", flush=True)


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # Threaded so a long "Update All" runs in its own thread while /status,
    # /parts and the dashboard stay responsive.
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"[easyeda2kicad-server] starting on port 7777", flush=True)
    print(f"[easyeda2kicad-server] easyeda2kicad = {EASYEDA2KICAD}", flush=True)
    with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        httpd.serve_forever()

#!/usr/bin/env python3
"""
Tiny local HTTP server that the Chrome extension calls to trigger easyeda2kicad.
Runs on http://localhost:7777 — started automatically by launchd at login.

When easyeda2kicad can't find a part in EasyEDA's database (many through-hole or
niche SMD parts), the server falls back to querying the LCSC Pro API directly and
generating a minimal-but-complete KiCad symbol from scratch.
"""

import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime

PORT = 7777

# ---------------------------------------------------------------------------
# easyeda2kicad location
# ---------------------------------------------------------------------------

def find_easyeda2kicad():
    found = shutil.which("easyeda2kicad")
    if found:
        return found
    for c in [
        os.path.expanduser("~/miniconda3/bin/easyeda2kicad"),
        os.path.expanduser("~/anaconda3/bin/easyeda2kicad"),
        os.path.expanduser("~/.local/bin/easyeda2kicad"),
        "/usr/local/bin/easyeda2kicad",
        "/opt/homebrew/bin/easyeda2kicad",
    ]:
        if os.path.isfile(c):
            return c
    return None


EASYEDA2KICAD = find_easyeda2kicad()


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
            depth, i = 0, idx
            while i < len(content):
                if content[i] == "(":
                    depth += 1
                elif content[i] == ")":
                    depth -= 1
                    if depth == 0:
                        starts.append(idx)
                        ends.append(i + 1)
                        pos = i + 1
                        break
                i += 1
            else:
                break

        if len(starts) <= 1:
            return

        # Remove all but the last (newest) entry, in reverse order.
        for start, end in zip(reversed(starts[:-1]), reversed(ends[:-1])):
            block_start = start
            while block_start > 0 and content[block_start - 1] in (" ", "\t"):
                block_start -= 1
            if block_start > 0 and content[block_start - 1] == "\n":
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


def add_symbol_fields(sym_path, symbol_name, description, in_stock, price="", jlcpcb_class=""):
    """
    Add/update In Stock and Stock Update Date on an easyeda2kicad-generated symbol.
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
        headers={"Accept": "application/json", "User-Agent": "easyeda2kicad-server/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"[easyeda2kicad-server] LCSC API fetch error: {exc}", flush=True)
        return None

    if not data.get("success"):
        return None

    products = data.get("result", {}).get("productList", [])
    # Exact match on LCSC number
    part = next((p for p in products if p.get("number") == lcsc_id), None)
    if not part:
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


def _default_lib_path():
    return os.path.join(
        os.path.expanduser("~"),
        "Documents", "Kicad", "easyeda2kicad", "easyeda2kicad.kicad_sym",
    )


def _remove_existing_symbol(content, symbol_name):
    """Remove a top-level symbol block matching (symbol "symbol_name" ...) from content."""
    pat = re.compile(r'  \(symbol "' + re.escape(symbol_name) + r'"')
    m = pat.search(content)
    if not m:
        return content
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
                return content[:start] + content[end:]
        i += 1
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

    # Description priority:
    #   1. Scraped from browser (JLCPCB row / LCSC page key attributes)
    #   2. EasyEDA Pro device_info description / attributes
    #   3. Manufacturer + MPN as last resort
    desc  = description or device_desc or f"{manufacturer} {mpn}".strip()
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
        f'      "LCSC Part"\n'
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
# Bulk update helpers
# ---------------------------------------------------------------------------

def _find_lcsc_symbols(lib_path):
    """
    Return a list of (symbol_name, lcsc_id) for every top-level symbol in the
    library that has an 'LCSC Part' property.
    """
    try:
        with open(lib_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return []

    results  = []
    sym_pat  = re.compile(r'^  \(symbol "([^"]+)"', re.MULTILINE)
    lcsc_pat = re.compile(r'\(property\s+"LCSC Part"\s+"(C\d+)"')

    for m in sym_pat.finditer(content):
        sym_name = m.group(1)
        # Sub-symbols end with _N_N — skip them
        if re.search(r'_\d+_\d+$', sym_name):
            continue

        # Walk to the end of this symbol block
        start = m.start()
        depth = 0
        i     = start
        end   = len(content)
        while i < len(content):
            if content[i] == "(":
                depth += 1
            elif content[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1

        lcsc_m = lcsc_pat.search(content[start:end])
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
        with open(lib_path, "r", encoding="utf-8") as f:
            content = f.read()
        sym_pat = re.compile(r'^  \(symbol "([^"]+)"', re.MULTILINE)
        status["total_symbols"] = sum(
            1 for m in sym_pat.finditer(content)
            if not re.search(r'_\d+_\d+$', m.group(1))
        )
    except Exception:
        pass

    status["lcsc_symbols"] = len(_find_lcsc_symbols(lib_path))
    return status


def _update_all_symbols(lib_path):
    """Batch-update In Stock, Unit Price, JLCPCB Class for all LCSC symbols."""
    if not os.path.isfile(lib_path):
        return {"success": False, "error": f"Library not found: {lib_path}"}

    symbols = _find_lcsc_symbols(lib_path)
    if not symbols:
        return {"success": True, "updated": 0, "failed": 0, "total": 0,
                "message": "No LCSC symbols found."}

    updated = 0
    failed  = 0
    errors  = []

    for symbol_name, lcsc_id in symbols:
        part_info = _fetch_lcsc_part_info(lcsc_id)
        if not part_info:
            failed += 1
            errors.append(f"{lcsc_id}: not found in LCSC API")
            continue

        add_symbol_fields(
            lib_path,
            symbol_name,
            description="",  # don't overwrite user-set descriptions
            in_stock=str(part_info.get("stock", "")),
            price=part_info.get("price_str", ""),
            jlcpcb_class=part_info.get("jlcpcb_class", ""),
        )
        updated += 1
        print(f"[easyeda2kicad-server] update-all: {lcsc_id} ({symbol_name})", flush=True)

    return {
        "success": True,
        "updated": updated,
        "failed":  failed,
        "total":   len(symbols),
        "errors":  errors,
    }


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

        if parsed.path == "/status":
            self._json(200, _get_status(_default_lib_path()))
            return

        if parsed.path == "/update-all":
            self._json(200, _update_all_symbols(_default_lib_path()))
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

        if not EASYEDA2KICAD:
            self._json(500, {"success": False, "error": "easyeda2kicad not found in PATH"})
            return

        try:
            result = subprocess.run(
                [EASYEDA2KICAD, "--full", "--overwrite", f"--lcsc_id={lcsc_id}"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            success     = result.returncode == 0
            symbol_name = parse_symbol_name(result.stderr)
            lib_path    = parse_lib_path(result.stderr)

            if success and symbol_name and lib_path:
                # Normal path: easyeda2kicad succeeded.
                # Deduplicate first (--overwrite appends, doesn't remove old entry).
                _dedup_symbol(lib_path, symbol_name)

                # Always fetch from LCSC API: stock from the browser is unreliable
                # (it often picks up package codes like "0603" or MOQ values).
                # Also use API description when the browser didn't scrape a real one.
                price        = ""
                jlcpcb_class = ""
                part_info = _fetch_lcsc_part_info(lcsc_id)
                if part_info:
                    is_trivial = (
                        not description or
                        bool(re.match(r'^LCSC:\s*C\d+\s*$', description.strip())) or
                        len(description.strip()) <= 5
                    )
                    if is_trivial and part_info.get("device_desc"):
                        description = part_info["device_desc"]
                    # Use API stock — it's always accurate
                    if part_info.get("stock"):
                        in_stock = str(part_info["stock"])
                    price        = part_info.get("price_str", "")
                    jlcpcb_class = part_info.get("jlcpcb_class", "")

                add_symbol_fields(lib_path, symbol_name, description, in_stock, price, jlcpcb_class)
                self._json(200, {
                    "success":     True,
                    "symbol_name": symbol_name,
                    "lib_path":    lib_path,
                    "stdout":      result.stdout,
                    "stderr":      result.stderr,
                })
            else:
                # easyeda2kicad failed — part likely not in EasyEDA's database.
                # Try generating a minimal symbol directly from the LCSC Pro API.
                print(
                    f"[easyeda2kicad-server] easyeda2kicad failed for {lcsc_id}, "
                    f"trying fallback import…",
                    flush=True,
                )
                fb = fallback_import(lcsc_id, description, in_stock)
                if not fb["success"]:
                    # Surface the original easyeda2kicad error alongside the fallback error.
                    fb["stderr"] = result.stderr + "\n\nFallback error: " + fb.get("error", "")
                self._json(200, fb)

        except subprocess.TimeoutExpired:
            self._json(500, {"success": False, "error": "command timed out"})
        except Exception as exc:
            self._json(500, {"success": False, "error": str(exc)})

    def _json(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(f"[easyeda2kicad-server] {fmt % args}", flush=True)


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"[easyeda2kicad-server] starting on port 7777", flush=True)
    print(f"[easyeda2kicad-server] easyeda2kicad = {EASYEDA2KICAD}", flush=True)
    with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        httpd.serve_forever()

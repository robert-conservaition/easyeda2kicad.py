#!/usr/bin/env python3
"""
Tiny local HTTP server that the Chrome extension calls to trigger easyeda2kicad.
Runs on http://localhost:7777 — started automatically by launchd at login.
"""

import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import urllib.parse

PORT = 7777


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


def parse_symbol_name(stderr):
    """Extract 'Symbol name : FOO' from easyeda2kicad stderr."""
    m = re.search(r"Symbol name\s*:\s*(\S+)", stderr)
    return m.group(1) if m else None


def parse_lib_path(stderr):
    """Extract the .kicad_sym path from easyeda2kicad stderr output."""
    m = re.search(r"Library path\s*:\s*(.+\.kicad_sym)", stderr)
    return m.group(1).strip() if m else None


def add_ki_description(sym_path, symbol_name, description):
    """
    Insert (or replace) the ki_description property in a .kicad_sym file
    for the given symbol_name.
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
        block = content[sym_start:insert_pos]

        # Remove existing ki_description if present
        existing = re.search(
            r'\(property\s*\n?\s*"ki_description"[^)]*\([^)]*\)[^)]*\)',
            block,
            re.DOTALL,
        )
        if existing:
            content = (
                content[:sym_start + existing.start()]
                + content[sym_start + existing.end():]
            )
            nested_m = nested_pat.search(content, sym_start)
            if not nested_m:
                return
            insert_pos = nested_m.start()

        all_ids = [int(m.group(1)) for m in re.finditer(r"\(id (\d+)\)", content)]
        next_id = max(all_ids, default=10) + 1

        safe_desc = description.replace("\\", "\\\\").replace('"', '\\"')
        prop = (
            f'    (property\n'
            f'      "ki_description"\n'
            f'      "{safe_desc}"\n'
            f'      (id {next_id})\n'
            f'      (at 0 0 0)\n'
            f'      (effects (font (size 1.27 1.27) ) hide)\n'
            f'    )\n'
        )
        content = content[:insert_pos] + prop + content[insert_pos:]

        with open(sym_path, "w", encoding="utf-8") as f:
            f.write(content)

    except Exception as e:
        print(f"[easyeda2kicad-server] warning: could not write ki_description: {e}", flush=True)


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

        if parsed.path != "/import":
            self.send_response(404)
            self.end_headers()
            return

        lcsc_id     = params.get("lcsc_id",     [None])[0]
        description = params.get("description", [""])[0]

        if not lcsc_id:
            self._json(400, {"success": False, "error": "missing lcsc_id"})
            return

        if not EASYEDA2KICAD:
            self._json(500, {"success": False, "error": "easyeda2kicad not found in PATH"})
            return

        try:
            # No --output: use easyeda2kicad's default path, which is what KiCad
            # has configured (~/Documents/Kicad/easyeda2kicad/easyeda2kicad.kicad_sym)
            result = subprocess.run(
                [EASYEDA2KICAD, "--full", "--overwrite", f"--lcsc_id={lcsc_id}"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            success     = result.returncode == 0
            symbol_name = parse_symbol_name(result.stderr)
            lib_path    = parse_lib_path(result.stderr)

            if success and description and symbol_name and lib_path:
                add_ki_description(lib_path, symbol_name, description)

            self._json(200, {
                "success":     success,
                "symbol_name": symbol_name or "",
                "lib_path":    lib_path or "",
                "stdout":      result.stdout,
                "stderr":      result.stderr,
            })
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

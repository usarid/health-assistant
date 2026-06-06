#!/usr/bin/env python3
"""Tiny localhost receiver for v3 dev — writes POST'd JSON to disk.

Listens on 127.0.0.1:8765. POST any path; the body is written to
tools/v3/out/<X-Filename header or 'upload.json'>. Returns CORS headers
permissive enough that an HTTPS page on any origin can post here from
the browser (Chrome treats localhost as a secure context, so HTTPS →
http://127.0.0.1 is allowed without mixed-content block).

This is a dev convenience — the structural analog of the mobile app's
"POST scraped JSON to BinaHealth backend" step, useful while we're
iterating on v3 in a browser tab. Not for prod.

Usage:
    python3 tools/v3/dev/upload_receiver.py    # foreground
    # or background:
    python3 tools/v3/dev/upload_receiver.py &
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / 'out'
OUT_DIR.mkdir(parents=True, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Filename')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n)
        # Sanitize filename
        raw_name = self.headers.get('X-Filename', 'upload.json')
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(raw_name))
        path = OUT_DIR / safe
        with open(path, 'wb') as f:
            f.write(body)
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True, 'wrote': str(path), 'bytes': n}).encode())
        sys.stderr.write(f'[upload_receiver] wrote {n} bytes → {path}\n')

    def log_message(self, *args, **kwargs):
        pass  # silence the default per-request log line


def main():
    port = int(os.environ.get('PORT', '8765'))
    server = HTTPServer(('127.0.0.1', port), Handler)
    sys.stderr.write(f'[upload_receiver] listening on http://127.0.0.1:{port}/  → {OUT_DIR}\n')
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

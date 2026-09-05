from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from computeruse.tools.web import _is_fetchable_url, fetch_page


class AuditHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body: bytes = ('<html><body>' + 'AUDIT_LOOPBACK_ONLY ' * 30 + '</body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server: HTTPServer = HTTPServer(('127.0.0.1', 0), AuditHandler)
    thread: threading.Thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port: int = server.server_port
        ordinary: str = f'http://127.0.0.1:{port}/'
        alias: str = f'http://127.1:{port}/'
        text: str = fetch_page(alias)
        print(json.dumps({'probe': 'loopback_fetch_bypass', 'ordinary_allowed': _is_fetchable_url(ordinary),
                          'alias_allowed': _is_fetchable_url(alias),
                          'read_own_loopback_server': text.startswith('AUDIT_LOOPBACK_ONLY'), 'chars': len(text)}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Hermes Ollama Proxy — token-gated reverse proxy.
Sits on port 11435. ngrok tunnels 11435.
Only forwards requests that carry X-Hermes-Token header.
All other requests → 403 Forbidden.
"""
import http.server
import os
import socketserver
import urllib.request
import urllib.error

PROXY_PORT  = int(os.getenv("PROXY_PORT",  "11435"))
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
SECRET      = os.getenv("HERMES_TUNNEL_TOKEN", "")
OLLAMA_BASE = f"http://127.0.0.1:{OLLAMA_PORT}"


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def _check_token(self) -> bool:
        if not SECRET:
            return True  # no token set — allow all (shouldn't happen)
        incoming = self.headers.get("X-Hermes-Token", "")
        return incoming == SECRET

    def _proxy(self):
        if not self._check_token():
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"403 Forbidden — invalid token\n")
            return

        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length) if length else None
        target  = OLLAMA_BASE + self.path

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "x-hermes-token")}
        if body:
            headers["Content-Length"] = str(len(body))

        req = urllib.request.Request(target, data=body,
                                     headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() != "transfer-encoding":
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as ex:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Proxy error: {ex}\n".encode())

    def do_GET(self):    self._proxy()
    def do_POST(self):   self._proxy()
    def do_DELETE(self): self._proxy()
    def do_HEAD(self):   self._proxy()

    def log_message(self, fmt, *args):
        token_ok = self._check_token()
        status = args[1] if len(args) > 1 else "?"
        src = self.client_address[0]
        tag = "✅" if token_ok else "🚫 BLOCKED"
        print(f"  {tag}  {src}  {self.command} {self.path}  → {status}", flush=True)


class ThreadedProxy(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if not SECRET:
        print("❌  HERMES_TUNNEL_TOKEN not set. Refusing to start without a token.")
        raise SystemExit(1)

    print(f"\n  Hermes Ollama Proxy")
    print(f"  Listening : http://127.0.0.1:{PROXY_PORT}")
    print(f"  Forwarding: {OLLAMA_BASE}")
    print(f"  Token     : {SECRET[:6]}{'*' * (len(SECRET)-6)}")
    print(f"  All requests without X-Hermes-Token → 403\n")

    server = ThreadedProxy(("127.0.0.1", PROXY_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Proxy stopped.")

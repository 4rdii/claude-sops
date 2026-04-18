#!/usr/bin/env python3
"""
One-shot web form for secret input.
Serves a secure form, accepts secret submission, encrypts with SOPS, shuts down.
No secret values are logged.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

SOPS_DIR = Path(os.environ.get("CLAUDE_SOPS_DIR", Path.home() / ".claude-sops"))
SERVE_DIR = Path(__file__).parent
DEFAULT_PORT = 8888


class SecretHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SERVE_DIR), **kwargs)

    def do_POST(self):
        if self.path == "/api/secret":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                name = data.get("name", "").strip()
                value = data.get("value", "").strip()
                tier = int(data.get("tier", 1))

                if not name or not value:
                    self._json(400, {"ok": False, "error": "Name and value required"})
                    return

                if tier not in (1, 2):
                    self._json(400, {"ok": False, "error": "Tier must be 1 or 2"})
                    return

                # Use the add-secret script
                script = SOPS_DIR / "bin" / "add-secret"
                if not script.exists():
                    # Fallback: use the repo's bin
                    script = Path(__file__).parent.parent / "bin" / "add-secret"

                result = subprocess.run(
                    [str(script), "--tier", str(tier), name, value],
                    capture_output=True, text=True, timeout=10
                )

                if result.returncode == 0:
                    self._json(200, {
                        "ok": True,
                        "tier": tier,
                        "message": f"Secret '{name}' saved to Tier {tier}."
                    })
                    print(f"\n  Secret saved: {name} (Tier {tier})")

                    # Check if auto-shutdown is desired
                    if data.get("shutdown", False):
                        print("  Shutting down...")
                        threading.Thread(target=self.server.shutdown).start()
                else:
                    self._json(500, {"ok": False, "error": result.stderr.strip()})

            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "Invalid JSON"})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        else:
            self._json(404, {"ok": False, "error": "Not found"})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args):
        # Suppress POST logs to avoid leaking secrets
        if "POST" not in str(args):
            super().log_message(fmt, *args)


def main():
    port = DEFAULT_PORT
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])

    # Detect server IP
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "0.0.0.0"

    print(f"""
  ╔══════════════════════════════════════╗
  ║   claude-sops: Secret Input Form    ║
  ╚══════════════════════════════════════╝

  Open in your browser:
    http://{ip}:{port}

  - Enter your secret key name and value
  - Choose Tier 1 (Claude can decrypt) or Tier 2 (proxy only)
  - Secret is encrypted with SOPS/age immediately
  - Server stays running until you close it (Ctrl+C)
  - Secret values are NEVER logged
""")

    server = HTTPServer(("0.0.0.0", port), SecretHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\nServer stopped.")


if __name__ == "__main__":
    main()

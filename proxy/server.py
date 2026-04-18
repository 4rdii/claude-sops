#!/usr/bin/env python3
"""
Tier 2 Zero-Knowledge Proxy Server.

Claude calls localhost:<port>/<route-name> and the proxy:
1. Loads the real secret from SOPS-encrypted Tier 2 store
2. Injects it into the outgoing request (as header, query param, or body field)
3. Forwards to the real API
4. Returns the response to Claude

Claude never sees the secret value — only the route name and the API response.

Configuration: ~/.claude-sops/proxy-routes.json
"""

import json
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, parse_qs

SOPS_DIR = Path(os.environ.get("CLAUDE_SOPS_DIR", Path.home() / ".claude-sops"))
ROUTES_FILE = SOPS_DIR / "proxy-routes.json"
DEFAULT_PORT = 9999

# Cache decrypted tier2 secrets in memory (only while proxy runs)
_secrets_cache = None


def load_secrets():
    """Decrypt Tier 2 secrets (cached in memory for proxy lifetime)."""
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    tier2_file = SOPS_DIR / "secrets" / "tier2.env.sops"
    if not tier2_file.exists():
        _secrets_cache = {}
        return _secrets_cache

    result = subprocess.run(
        ["sops", "--input-type", "dotenv", "--output-type", "dotenv", "-d", str(tier2_file)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [error] Failed to decrypt Tier 2 secrets: {result.stderr}")
        _secrets_cache = {}
        return _secrets_cache

    secrets = {}
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip()

    _secrets_cache = secrets
    return secrets


def load_routes():
    """Load proxy route configuration."""
    if not ROUTES_FILE.exists():
        return {}
    with open(ROUTES_FILE) as f:
        return json.load(f)


def create_default_routes():
    """Create example proxy-routes.json if it doesn't exist."""
    if ROUTES_FILE.exists():
        return

    example = {
        "_comment": "Proxy route configuration. Each key is a route name, called as localhost:9999/<route-name>/...",
        "_docs": {
            "target": "Base URL to forward to",
            "secret_key": "Name of the Tier 2 secret to inject",
            "inject_as": "How to inject: 'header', 'query', 'bearer'",
            "inject_name": "Header name or query param name (not needed for 'bearer')",
        },
        "example-api": {
            "target": "https://api.example.com",
            "secret_key": "EXAMPLE_API_KEY",
            "inject_as": "bearer",
        },
        "github": {
            "target": "https://api.github.com",
            "secret_key": "GITHUB_TOKEN",
            "inject_as": "header",
            "inject_name": "Authorization",
            "inject_prefix": "token ",
        },
    }
    ROUTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ROUTES_FILE, "w") as f:
        json.dump(example, f, indent=2)
    print(f"  Created example routes config: {ROUTES_FILE}")
    print(f"  Edit it to add your API routes.")


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PATCH(self):
        self._proxy("PATCH")

    def _proxy(self, method):
        # Parse route from path: /<route-name>/rest/of/path
        parts = self.path.strip("/").split("/", 1)
        route_name = parts[0]
        rest_path = parts[1] if len(parts) > 1 else ""

        # Special endpoints
        if route_name == "health":
            self._json(200, {"ok": True, "routes": list(load_routes().keys())})
            return

        if route_name == "reload":
            global _secrets_cache
            _secrets_cache = None
            self._json(200, {"ok": True, "message": "Secrets cache cleared"})
            return

        routes = load_routes()
        route = routes.get(route_name)
        if not route or route_name.startswith("_"):
            self._json(404, {
                "ok": False,
                "error": f"Route '{route_name}' not found",
                "available": [k for k in routes.keys() if not k.startswith("_")]
            })
            return

        secrets = load_secrets()
        secret_key = route.get("secret_key", "")
        secret_value = secrets.get(secret_key, "")

        if not secret_value:
            self._json(500, {
                "ok": False,
                "error": f"Secret '{secret_key}' not found in Tier 2 store"
            })
            return

        # Build target URL
        target_url = route["target"].rstrip("/")
        if rest_path:
            target_url += "/" + rest_path

        # Read request body if present
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Build headers (forward most, inject secret)
        headers = {}
        for key in self.headers:
            if key.lower() not in ("host", "connection", "transfer-encoding"):
                headers[key] = self.headers[key]

        # Inject secret
        inject_as = route.get("inject_as", "bearer")
        if inject_as == "bearer":
            headers["Authorization"] = f"Bearer {secret_value}"
        elif inject_as == "header":
            inject_name = route.get("inject_name", "Authorization")
            prefix = route.get("inject_prefix", "")
            headers[inject_name] = f"{prefix}{secret_value}"
        elif inject_as == "query":
            inject_name = route.get("inject_name", "api_key")
            separator = "&" if "?" in target_url else "?"
            target_url += f"{separator}{inject_name}={secret_value}"

        # Forward request
        try:
            req = Request(
                target_url,
                data=body,
                headers=headers,
                method=method
            )
            with urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, val in resp.headers.items():
                    if key.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(key, val)
                self.end_headers()
                self.wfile.write(resp_body)
        except HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self._json(502, {"ok": False, "error": f"Proxy error: {str(e)}"})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args):
        # Log route but not full URL (which might contain query secrets)
        parts = str(args[0]).split("/")
        route = parts[1] if len(parts) > 1 else "?"
        status = args[1] if len(args) > 1 else "?"
        print(f"  [{status}] {route}")


def main():
    port = DEFAULT_PORT
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])

    create_default_routes()

    routes = load_routes()
    route_names = [k for k in routes.keys() if not k.startswith("_")]

    print(f"""
  ╔══════════════════════════════════════════╗
  ║   claude-sops: Zero-Knowledge Proxy     ║
  ╚══════════════════════════════════════════╝

  Listening on: http://localhost:{port}
  Routes configured: {len(route_names)}
""")
    for name in route_names:
        r = routes[name]
        print(f"    /{name}/*  ->  {r.get('target', '?')}  (secret: {r.get('secret_key', '?')})")
    print(f"""
  Usage: Claude calls http://localhost:{port}/<route>/path
  The proxy injects the secret and forwards the request.
  Claude never sees the secret value.

  Special endpoints:
    /health   — list available routes
    /reload   — clear secrets cache (re-decrypt from SOPS)
""")

    secrets = load_secrets()
    print(f"  Tier 2 secrets loaded: {len(secrets)} keys")
    print()

    server = HTTPServer(("127.0.0.1", port), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\nProxy stopped.")


if __name__ == "__main__":
    main()

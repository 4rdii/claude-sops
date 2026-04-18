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

# Import signer module (optional — only needed if signing routes exist)
try:
    from signer import handle_sign_request, get_wallet_address, check_dependencies as check_signer
except ImportError:
    try:
        from proxy.signer import handle_sign_request, get_wallet_address, check_signer
    except ImportError:
        handle_sign_request = None
        get_wallet_address = None

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
            routes = load_routes()
            route_list = []
            for k, v in routes.items():
                if k.startswith("_"):
                    continue
                route_type = v.get("type", "proxy")
                route_list.append({"name": k, "type": route_type})
            self._json(200, {"ok": True, "routes": route_list})
            return

        if route_name == "reload":
            global _secrets_cache
            _secrets_cache = None
            self._json(200, {"ok": True, "message": "Secrets cache cleared"})
            return

        # Handle /sign/<wallet>/<action> endpoints
        if route_name == "sign":
            self._handle_signing(rest_path, method)
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

        # Check if this is a signer route accessed directly (redirect to /sign/)
        if route.get("type") == "signer":
            self._json(400, {
                "ok": False,
                "error": f"'{route_name}' is a signer wallet. Use /sign/{route_name}/send or /sign/{route_name}/address",
                "endpoints": [
                    f"/sign/{route_name}/address — get wallet address & balance",
                    f"/sign/{route_name}/balance — get balance",
                    f"/sign/{route_name}/nonce — get current nonce",
                    f"/sign/{route_name}/send — sign & broadcast transaction (POST)",
                    f"/sign/{route_name}/sign — sign without broadcasting (POST)",
                ]
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

    def _handle_signing(self, rest_path, method):
        """Handle /sign/<wallet>/<action> requests."""
        if handle_sign_request is None:
            self._json(500, {
                "ok": False,
                "error": "Signing module not available. Install web3: pip install web3"
            })
            return

        # Parse: <wallet_name>/<action>
        parts = rest_path.strip("/").split("/", 1)
        if len(parts) < 1 or not parts[0]:
            # List available signer wallets
            routes = load_routes()
            signers = [
                {"name": k, "chain_id": v.get("chain_id", 1)}
                for k, v in routes.items()
                if v.get("type") == "signer" and not k.startswith("_")
            ]
            self._json(200, {
                "ok": True,
                "wallets": signers,
                "usage": "/sign/<wallet>/address | balance | nonce | send | sign"
            })
            return

        wallet_name = parts[0]
        action = parts[1] if len(parts) > 1 else "address"

        # Load route config
        routes = load_routes()
        route = routes.get(wallet_name)
        if not route or route.get("type") != "signer":
            self._json(404, {
                "ok": False,
                "error": f"Signer wallet '{wallet_name}' not found",
                "available": [k for k in routes if routes[k].get("type") == "signer"]
            })
            return

        # Load secret (private key)
        secrets = load_secrets()
        secret_key = route.get("secret_key", "")
        secret_value = secrets.get(secret_key, "")

        if not secret_value:
            self._json(500, {
                "ok": False,
                "error": f"Private key '{secret_key}' not found in Tier 2 store"
            })
            return

        # Parse request body for send/sign actions
        tx_params = {}
        if method == "POST":
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                try:
                    tx_params = json.loads(body)
                except json.JSONDecodeError:
                    self._json(400, {"ok": False, "error": "Invalid JSON body"})
                    return

        # Delegate to signer module
        result = handle_sign_request(action, wallet_name, route, secret_value, tx_params)
        status = 200 if result.get("ok", False) else 400
        self._json(status, result)

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
        if r.get("type") == "signer":
            chain = r.get("chain_id", 1)
            print(f"    /sign/{name}/*  ->  EVM signer (chain: {chain}, key: {r.get('secret_key', '?')})")
        else:
            print(f"    /{name}/*  ->  {r.get('target', '?')}  (secret: {r.get('secret_key', '?')})")
    print(f"""
  Usage:
    API proxy:  http://localhost:{port}/<route>/path
    Signer:     http://localhost:{port}/sign/<wallet>/send  (POST)
                http://localhost:{port}/sign/<wallet>/address

  The proxy injects secrets / signs transactions.
  Claude never sees secret values or private keys.

  Special endpoints:
    /health   — list available routes
    /reload   — clear secrets cache (re-decrypt from SOPS)
    /sign     — list signer wallets
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

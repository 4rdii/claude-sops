"""
claude-sops Python SDK

Helper library for scripts that need to access secrets or sign transactions
through the claude-sops proxy. Import this instead of handling secrets directly.

Usage:
    from claude_sops import get_secret, api, sign_tx, wallet_address

    # Tier 1: get a decryptable secret
    token = get_secret("VERCEL_TOKEN")

    # Tier 2: make an API call through the proxy (zero-knowledge)
    response = api("helius", "/v0/tokens/metadata", params={"mint": "..."})

    # Tier 2: sign and send a transaction (zero-knowledge)
    result = sign_tx("my-wallet", to="0xABC...", value=0.01)
    print(result["tx_hash"])

    # Tier 2: get wallet address without exposing key
    addr = wallet_address("my-wallet")
"""

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

PROXY_URL = os.environ.get("CLAUDE_SOPS_PROXY", "http://localhost:9999")
SOPS_DIR = Path(os.environ.get("CLAUDE_SOPS_DIR", Path.home() / ".claude-sops"))


# ─── Tier 1: Direct secret access ────────────────────────────────────────────

def get_secret(key: str) -> str:
    """
    Get a Tier 1 secret by decrypting from SOPS store.

    Returns the secret value as a string.
    Raises RuntimeError if key not found or decryption fails.
    """
    bin_path = SOPS_DIR / "bin" / "get-secret"
    if bin_path.exists():
        result = subprocess.run(
            [str(bin_path), key],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        raise RuntimeError(f"Failed to get secret '{key}': {result.stderr.strip()}")

    # Fallback: decrypt directly
    tier1_file = SOPS_DIR / "secrets" / "tier1.env.sops"
    if not tier1_file.exists():
        raise RuntimeError(f"No Tier 1 store found at {tier1_file}")

    result = subprocess.run(
        ["sops", "--input-type", "dotenv", "--output-type", "dotenv", "-d", str(tier1_file)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"SOPS decrypt failed: {result.stderr.strip()}")

    for line in result.stdout.strip().split("\n"):
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()

    raise RuntimeError(f"Secret '{key}' not found in Tier 1 store")


def list_secrets() -> dict:
    """
    List all secret key names (no values) organized by tier.

    Returns: {"tier1": ["KEY1", "KEY2"], "tier2": ["KEY3"]}
    """
    secrets = {"tier1": [], "tier2": []}

    for tier in (1, 2):
        store = SOPS_DIR / "secrets" / f"tier{tier}.env.sops"
        if not store.exists():
            continue
        result = subprocess.run(
            ["sops", "--input-type", "dotenv", "--output-type", "dotenv", "-d", str(store)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line and not line.startswith("#") and "=" in line:
                    key = line.split("=", 1)[0].strip()
                    secrets[f"tier{tier}"].append(key)

    return secrets


# ─── Tier 2: API calls through proxy ─────────────────────────────────────────

def api(route: str, path: str = "", method: str = "GET",
        params: dict = None, json_body: dict = None,
        headers: dict = None, raw: bool = False):
    """
    Make an API call through the Tier 2 zero-knowledge proxy.

    The proxy injects the real secret (API key, token, etc.) into the request.
    Your script never sees the secret.

    Args:
        route: Proxy route name (from proxy-routes.json)
        path: API path after the route (e.g. "/v1/users")
        method: HTTP method (GET, POST, PUT, DELETE)
        params: Query parameters dict
        json_body: JSON body for POST/PUT requests
        headers: Additional headers
        raw: If True, return raw bytes instead of parsed JSON

    Returns: Parsed JSON response (dict/list) or raw bytes if raw=True
    """
    url = f"{PROXY_URL}/{route}"
    if path:
        url += "/" + path.lstrip("/")
    if params:
        url += "?" + urlencode(params)

    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()

    req = Request(url, data=body, headers=req_headers, method=method)

    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
            if raw:
                return data
            return json.loads(data)
    except HTTPError as e:
        error_body = e.read()
        try:
            return json.loads(error_body)
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError(f"API call failed ({e.code}): {error_body.decode()}")


def api_get(route: str, path: str = "", params: dict = None):
    """Shorthand for api() with GET method."""
    return api(route, path, method="GET", params=params)


def api_post(route: str, path: str = "", json_body: dict = None):
    """Shorthand for api() with POST method."""
    return api(route, path, method="POST", json_body=json_body)


# ─── Tier 2: Transaction signing ─────────────────────────────────────────────

def wallet_address(wallet: str) -> dict:
    """
    Get wallet address and balance without exposing the private key.

    Returns: {"ok": True, "address": "0x...", "balance_eth": "1.5", ...}
    """
    return api("sign", f"{wallet}/address")


def wallet_balance(wallet: str) -> str:
    """Get wallet balance in ETH as a string."""
    result = api("sign", f"{wallet}/balance")
    return result.get("balance_eth", "0")


def sign_tx(wallet: str, to: str, value: float = 0,
            data: str = None, gas: int = None,
            broadcast: bool = True) -> dict:
    """
    Sign (and optionally broadcast) an EVM transaction.

    The proxy loads the private key from Tier 2 SOPS store, signs locally,
    and broadcasts to the configured RPC. Your script never sees the key.

    Args:
        wallet: Wallet name from proxy-routes.json
        to: Destination address
        value: ETH value to send (as float, e.g. 0.1 for 0.1 ETH)
        data: Hex-encoded calldata for contract calls
        gas: Gas limit (auto-estimated if omitted)
        broadcast: If True, sign and send. If False, sign only.

    Returns: {"ok": True, "tx_hash": "0x...", "explorer": "https://..."} on success
    """
    action = "send" if broadcast else "sign"

    tx_params = {"to": to}

    if value > 0:
        # Convert ETH float to wei
        tx_params["value"] = int(Decimal(str(value)) * Decimal(10**18))

    if data:
        tx_params["data"] = data

    if gas:
        tx_params["gas"] = gas

    return api("sign", f"{wallet}/{action}", method="POST", json_body=tx_params)


def encode_erc20_transfer(to: str, amount_wei: int) -> str:
    """
    Encode an ERC20 transfer(address,uint256) call.

    Use with sign_tx:
        sign_tx("wallet", to=TOKEN_ADDRESS, data=encode_erc20_transfer(recipient, amount))
    """
    # transfer(address,uint256) selector = 0xa9059cbb
    to_padded = to.lower().replace("0x", "").zfill(64)
    amount_padded = hex(amount_wei)[2:].zfill(64)
    return f"0xa9059cbb{to_padded}{amount_padded}"


def encode_erc20_approve(spender: str, amount_wei: int) -> str:
    """
    Encode an ERC20 approve(address,uint256) call.

    Use with sign_tx:
        sign_tx("wallet", to=TOKEN_ADDRESS, data=encode_erc20_approve(spender, amount))
    """
    # approve(address,uint256) selector = 0x095ea7b3
    spender_padded = spender.lower().replace("0x", "").zfill(64)
    amount_padded = hex(amount_wei)[2:].zfill(64)
    return f"0x095ea7b3{spender_padded}{amount_padded}"


# ─── Proxy health ────────────────────────────────────────────────────────────

def proxy_health() -> dict:
    """Check if the proxy is running and list available routes."""
    try:
        return api("health")
    except Exception as e:
        return {"ok": False, "error": str(e)}


def is_proxy_running() -> bool:
    """Quick check if the proxy is reachable."""
    try:
        result = proxy_health()
        return result.get("ok", False)
    except Exception:
        return False

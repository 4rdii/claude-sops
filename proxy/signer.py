#!/usr/bin/env python3
"""
Tier 2 Transaction Signing Proxy.

Claude sends transaction parameters, this module signs with a private key
from the SOPS Tier 2 store and broadcasts to the RPC endpoint.
Claude NEVER sees the private key.

Flow:
  Claude → POST /sign/<wallet>/send {to, value, data, ...}
       → signer loads private key from Tier 2 SOPS
       → builds & signs transaction
       → sends to RPC
       → returns tx hash to Claude

Wallet configuration in proxy-routes.json:
{
  "my-wallet": {
    "type": "signer",
    "secret_key": "MY_PRIVATE_KEY",
    "rpc_url": "https://eth-mainnet.g.alchemy.com/v2/...",
    "chain_id": 1,
    "max_value": "0.1",
    "allowed_contracts": ["0x..."],
    "require_confirmation": false
  }
}

Safety features:
  - max_value: reject transactions above this ETH value (default: 0.1 ETH)
  - allowed_contracts: whitelist of contract addresses (empty = any)
  - require_confirmation: if true, returns unsigned tx for review first
  - All signing happens in-memory, key is never written to disk unencrypted
  - Transaction log saved (without private key) for audit trail
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

try:
    from web3 import Web3
    from eth_account import Account
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False

SOPS_DIR = Path.home() / ".claude-sops"
TX_LOG = SOPS_DIR / "tx-log.jsonl"


def check_dependencies():
    """Check if web3.py is installed."""
    if not HAS_WEB3:
        return {
            "ok": False,
            "error": "web3 not installed. Run: pip install web3"
        }
    return {"ok": True}


def get_wallet_address(secret_value: str) -> str:
    """Derive wallet address from private key without exposing key."""
    if not HAS_WEB3:
        return "unknown (web3 not installed)"
    try:
        account = Account.from_key(secret_value)
        return account.address
    except Exception:
        return "invalid key"


def validate_tx_request(tx_params: dict, route_config: dict) -> dict | None:
    """
    Validate transaction against safety constraints.
    Returns error dict if invalid, None if OK.
    """
    # Check max_value
    max_value_eth = Decimal(str(route_config.get("max_value", "0.1")))
    tx_value_wei = int(tx_params.get("value", 0))
    tx_value_eth = Decimal(tx_value_wei) / Decimal(10**18)

    if tx_value_eth > max_value_eth:
        return {
            "ok": False,
            "error": f"Transaction value {tx_value_eth} ETH exceeds max_value limit of {max_value_eth} ETH. "
                     f"Update max_value in proxy-routes.json to allow larger transactions."
        }

    # Check allowed_contracts
    allowed = route_config.get("allowed_contracts", [])
    if allowed:
        to_addr = (tx_params.get("to") or "").lower()
        allowed_lower = [a.lower() for a in allowed]
        if to_addr not in allowed_lower:
            return {
                "ok": False,
                "error": f"Contract {tx_params.get('to')} not in allowed_contracts whitelist. "
                         f"Allowed: {allowed}"
            }

    # Check required fields
    if not tx_params.get("to"):
        return {
            "ok": False,
            "error": "Missing 'to' address in transaction"
        }

    return None


def log_transaction(wallet_name: str, tx_hash: str, tx_params: dict, chain_id: int):
    """Log transaction for audit trail (no secrets logged)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "wallet": wallet_name,
        "chain_id": chain_id,
        "tx_hash": tx_hash,
        "to": tx_params.get("to"),
        "value_wei": str(tx_params.get("value", 0)),
        "data_length": len(tx_params.get("data", "")) if tx_params.get("data") else 0,
    }
    TX_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(TX_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def build_transaction(w3: Web3, from_address: str, tx_params: dict, chain_id: int) -> dict:
    """Build a complete transaction dict ready for signing."""
    nonce = w3.eth.get_transaction_count(from_address)

    tx = {
        "chainId": chain_id,
        "nonce": nonce,
        "to": Web3.to_checksum_address(tx_params["to"]),
        "value": int(tx_params.get("value", 0)),
    }

    # Add data if present
    if tx_params.get("data"):
        tx["data"] = tx_params["data"]

    # Gas: use provided or estimate
    if tx_params.get("gas"):
        tx["gas"] = int(tx_params["gas"])
    else:
        try:
            tx["gas"] = w3.eth.estimate_gas({
                "from": from_address,
                "to": tx["to"],
                "value": tx["value"],
                "data": tx.get("data", b""),
            })
            # Add 20% buffer
            tx["gas"] = int(tx["gas"] * 1.2)
        except Exception as e:
            return {"error": f"Gas estimation failed: {str(e)}"}

    # Gas price: use EIP-1559 if available
    if tx_params.get("maxFeePerGas"):
        tx["maxFeePerGas"] = int(tx_params["maxFeePerGas"])
        tx["maxPriorityFeePerGas"] = int(tx_params.get("maxPriorityFeePerGas", 1_000_000_000))
    elif tx_params.get("gasPrice"):
        tx["gasPrice"] = int(tx_params["gasPrice"])
    else:
        # Auto-detect EIP-1559 support
        try:
            latest = w3.eth.get_block("latest")
            if hasattr(latest, "baseFeePerGas") and latest.baseFeePerGas:
                base_fee = latest.baseFeePerGas
                tx["maxFeePerGas"] = int(base_fee * 2)
                tx["maxPriorityFeePerGas"] = w3.eth.max_priority_fee
            else:
                tx["gasPrice"] = w3.eth.gas_price
        except Exception:
            tx["gasPrice"] = w3.eth.gas_price

    return tx


def handle_sign_request(action: str, wallet_name: str, route_config: dict,
                        secret_value: str, tx_params: dict) -> dict:
    """
    Handle a signing request.

    Actions:
      - "send": sign and broadcast transaction
      - "sign": sign and return raw signed tx (don't broadcast)
      - "address": return wallet address
      - "balance": return wallet balance
      - "nonce": return current nonce
    """
    dep_check = check_dependencies()
    if not dep_check["ok"]:
        return dep_check

    chain_id = int(route_config.get("chain_id", 1))
    rpc_url = route_config.get("rpc_url", "")

    if not rpc_url:
        return {"ok": False, "error": "No rpc_url configured for this wallet"}

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        return {"ok": False, "error": f"Cannot connect to RPC: {rpc_url}"}

    # Derive address from private key
    try:
        account = Account.from_key(secret_value)
        from_address = account.address
    except Exception as e:
        return {"ok": False, "error": f"Invalid private key: {str(e)}"}

    # --- Action: address ---
    if action == "address":
        balance = w3.eth.get_balance(from_address)
        return {
            "ok": True,
            "address": from_address,
            "balance_wei": str(balance),
            "balance_eth": str(Decimal(balance) / Decimal(10**18)),
            "chain_id": chain_id,
        }

    # --- Action: balance ---
    if action == "balance":
        balance = w3.eth.get_balance(from_address)
        return {
            "ok": True,
            "address": from_address,
            "balance_wei": str(balance),
            "balance_eth": str(Decimal(balance) / Decimal(10**18)),
        }

    # --- Action: nonce ---
    if action == "nonce":
        nonce = w3.eth.get_transaction_count(from_address)
        return {"ok": True, "address": from_address, "nonce": nonce}

    # --- Action: send or sign ---
    if action in ("send", "sign"):
        # Validate safety constraints
        validation_error = validate_tx_request(tx_params, route_config)
        if validation_error:
            return validation_error

        # Check require_confirmation for send
        if action == "send" and route_config.get("require_confirmation", False):
            # Build tx but don't send — return for review
            tx = build_transaction(w3, from_address, tx_params, chain_id)
            if "error" in tx:
                return {"ok": False, "error": tx["error"]}
            return {
                "ok": True,
                "action": "review",
                "message": "require_confirmation is enabled. Review this transaction, then call /sign/<wallet>/confirm with the same params.",
                "transaction": {
                    "from": from_address,
                    "to": tx["to"],
                    "value_wei": str(tx["value"]),
                    "value_eth": str(Decimal(tx["value"]) / Decimal(10**18)),
                    "gas": tx.get("gas"),
                    "chain_id": chain_id,
                    "data": tx_params.get("data", "none"),
                }
            }

        # Build transaction
        tx = build_transaction(w3, from_address, tx_params, chain_id)
        if "error" in tx:
            return {"ok": False, "error": tx["error"]}

        # Sign
        signed = account.sign_transaction(tx)

        if action == "sign":
            return {
                "ok": True,
                "action": "signed",
                "raw_transaction": signed.raw_transaction.hex(),
                "tx_hash": signed.hash.hex(),
                "from": from_address,
                "to": tx["to"],
                "value_eth": str(Decimal(tx["value"]) / Decimal(10**18)),
                "gas": tx.get("gas"),
            }

        # Broadcast
        try:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            # Log for audit
            log_transaction(wallet_name, tx_hash_hex, tx_params, chain_id)

            return {
                "ok": True,
                "action": "sent",
                "tx_hash": tx_hash_hex,
                "from": from_address,
                "to": tx["to"],
                "value_eth": str(Decimal(tx["value"]) / Decimal(10**18)),
                "gas": tx.get("gas"),
                "chain_id": chain_id,
                "explorer": get_explorer_url(chain_id, tx_hash_hex),
            }
        except Exception as e:
            return {"ok": False, "error": f"Broadcast failed: {str(e)}"}

    return {"ok": False, "error": f"Unknown action: {action}"}


def get_explorer_url(chain_id: int, tx_hash: str) -> str:
    """Get block explorer URL for a transaction."""
    explorers = {
        1: f"https://etherscan.io/tx/{tx_hash}",
        10: f"https://optimistic.etherscan.io/tx/{tx_hash}",
        56: f"https://bscscan.com/tx/{tx_hash}",
        100: f"https://gnosisscan.io/tx/{tx_hash}",
        137: f"https://polygonscan.com/tx/{tx_hash}",
        250: f"https://ftmscan.com/tx/{tx_hash}",
        8453: f"https://basescan.org/tx/{tx_hash}",
        42161: f"https://arbiscan.io/tx/{tx_hash}",
        43114: f"https://snowtrace.io/tx/{tx_hash}",
        11155111: f"https://sepolia.etherscan.io/tx/{tx_hash}",
    }
    return explorers.get(chain_id, f"chain:{chain_id}/tx/{tx_hash}")

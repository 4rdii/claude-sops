#!/usr/bin/env bash
# claude-sops shell helpers
#
# Source this in your scripts:
#   source /path/to/claude-sops/sdk/bash/claude-sops-helpers.sh
#
# Then use:
#   TOKEN=$(cs_get VERCEL_TOKEN)                     # Tier 1 secret
#   RESPONSE=$(cs_api helius /v0/tokens/metadata)    # Tier 2 API call
#   TX_HASH=$(cs_send my-wallet 0xABC 0.01)          # Tier 2 sign+send
#   ADDRESS=$(cs_address my-wallet)                   # Tier 2 wallet address

CLAUDE_SOPS_PROXY="${CLAUDE_SOPS_PROXY:-http://localhost:9999}"
CLAUDE_SOPS_DIR="${CLAUDE_SOPS_DIR:-$HOME/.claude-sops}"

# ─── Tier 1 ──────────────────────────────────────────────────────────────────

cs_get() {
    # Get a Tier 1 secret
    # Usage: cs_get MY_API_KEY
    local key="$1"
    if [[ -x "$CLAUDE_SOPS_DIR/bin/get-secret" ]]; then
        "$CLAUDE_SOPS_DIR/bin/get-secret" "$key"
    else
        sops --input-type dotenv --output-type dotenv -d "$CLAUDE_SOPS_DIR/secrets/tier1.env.sops" 2>/dev/null \
            | grep "^${key}=" | cut -d= -f2-
    fi
}

# ─── Tier 2: API proxy ──────────────────────────────────────────────────────

cs_api() {
    # Make a GET request through the Tier 2 proxy
    # Usage: cs_api <route> [path] [extra curl args...]
    local route="$1"
    local path="${2:-}"
    shift 2 2>/dev/null || shift 1
    local url="${CLAUDE_SOPS_PROXY}/${route}"
    [[ -n "$path" ]] && url="${url}/${path#/}"
    curl -s "$url" "$@"
}

cs_api_post() {
    # Make a POST request through the Tier 2 proxy
    # Usage: cs_api_post <route> [path] <json_body>
    local route="$1"
    local path="$2"
    local body="$3"
    local url="${CLAUDE_SOPS_PROXY}/${route}"
    [[ -n "$path" ]] && url="${url}/${path#/}"
    curl -s -X POST -H "Content-Type: application/json" -d "$body" "$url"
}

# ─── Tier 2: Signing ────────────────────────────────────────────────────────

cs_address() {
    # Get wallet address (zero-knowledge)
    # Usage: cs_address <wallet_name>
    local wallet="$1"
    curl -s "${CLAUDE_SOPS_PROXY}/sign/${wallet}/address" | python3 -c "import sys,json; print(json.load(sys.stdin).get('address','error'))" 2>/dev/null
}

cs_balance() {
    # Get wallet balance in ETH (zero-knowledge)
    # Usage: cs_balance <wallet_name>
    local wallet="$1"
    curl -s "${CLAUDE_SOPS_PROXY}/sign/${wallet}/balance" | python3 -c "import sys,json; print(json.load(sys.stdin).get('balance_eth','error'))" 2>/dev/null
}

cs_send() {
    # Sign and send a transaction (zero-knowledge)
    # Usage: cs_send <wallet> <to_address> [value_eth] [data_hex]
    # Returns: tx hash
    local wallet="$1"
    local to="$2"
    local value_eth="${3:-0}"
    local data="${4:-}"

    # Convert ETH to wei
    local value_wei
    value_wei=$(python3 -c "from decimal import Decimal; print(int(Decimal('$value_eth') * Decimal(10**18)))")

    local body="{\"to\": \"$to\", \"value\": $value_wei"
    [[ -n "$data" ]] && body="$body, \"data\": \"$data\""
    body="$body}"

    curl -s -X POST \
        -H "Content-Type: application/json" \
        -d "$body" \
        "${CLAUDE_SOPS_PROXY}/sign/${wallet}/send"
}

# ─── Health ──────────────────────────────────────────────────────────────────

cs_proxy_health() {
    # Check proxy status
    curl -s "${CLAUDE_SOPS_PROXY}/health" 2>/dev/null || echo '{"ok": false, "error": "proxy not reachable"}'
}

cs_proxy_running() {
    # Returns 0 if proxy is running, 1 if not
    curl -sf "${CLAUDE_SOPS_PROXY}/health" >/dev/null 2>&1
}

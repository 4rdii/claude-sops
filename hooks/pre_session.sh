#!/usr/bin/env bash
# claude-sops pre-session hook
# Runs at the start of each Claude Code session.
# - Auto-starts Tier 2 proxy if configured secrets exist
# - Outputs available secret names (not values) for context

SOPS_DIR="${CLAUDE_SOPS_DIR:-$HOME/.claude-sops}"

# Check if claude-sops is set up
if [[ ! -d "$SOPS_DIR" ]]; then
    exit 0
fi

# Auto-start proxy if Tier 2 secrets exist and proxy isn't running
TIER2_FILE="$SOPS_DIR/secrets/tier2.env.sops"
if [[ -f "$TIER2_FILE" ]]; then
    if ! curl -s http://localhost:9999/health &>/dev/null; then
        REPO_DIR="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
        nohup python3 "$REPO_DIR/proxy/server.py" > /tmp/claude-sops-proxy.log 2>&1 &
        echo "[claude-sops] Tier 2 proxy started on localhost:9999"
    fi
fi

# Output available secrets for context (keys only, never values)
echo "[claude-sops] Available secrets:"
if [[ -f "$SOPS_DIR/secrets/tier1.env.sops" ]]; then
    echo "  Tier 1 (decryptable):"
    sops --input-type dotenv --output-type dotenv -d "$SOPS_DIR/secrets/tier1.env.sops" 2>/dev/null | \
        grep -v '^#' | grep -v '^$' | cut -d= -f1 | while read -r key; do
        echo "    - $key"
    done
fi
if [[ -f "$TIER2_FILE" ]]; then
    echo "  Tier 2 (proxy only):"
    sops --input-type dotenv --output-type dotenv -d "$TIER2_FILE" 2>/dev/null | \
        grep -v '^#' | grep -v '^$' | cut -d= -f1 | while read -r key; do
        echo "    - $key"
    done
fi

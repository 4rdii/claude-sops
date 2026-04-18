#!/usr/bin/env bash
set -euo pipefail

# claude-sops: Secret management for Claude Code
# This script sets up SOPS + age encryption on a fresh VPS.

SOPS_DIR="${CLAUDE_SOPS_DIR:-$HOME/.claude-sops}"
KEY_FILE="$SOPS_DIR/age-key.txt"
SOPS_CONFIG="$HOME/.sops.yaml"
PROXY_PORT="${PROXY_PORT:-9999}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[info]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
err()   { echo -e "${RED}[error]${NC} $*" >&2; }

# ── Step 1: Install dependencies ──────────────────────────────────────────────

install_age() {
    if command -v age &>/dev/null; then
        ok "age already installed ($(age --version 2>/dev/null || echo 'unknown'))"
        return
    fi
    info "Installing age..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq age
    elif command -v brew &>/dev/null; then
        brew install age
    elif command -v pacman &>/dev/null; then
        pacman -S --noconfirm age
    else
        # Fallback: install from GitHub release
        local version="1.2.0"
        local arch
        arch=$(uname -m)
        case "$arch" in
            x86_64)  arch="amd64" ;;
            aarch64) arch="arm64" ;;
        esac
        curl -sL "https://github.com/FiloSottile/age/releases/download/v${version}/age-v${version}-linux-${arch}.tar.gz" | \
            tar xz -C /usr/local/bin --strip-components=1 age/age age/age-keygen
    fi
    ok "age installed"
}

install_sops() {
    if command -v sops &>/dev/null; then
        ok "sops already installed ($(sops --version 2>/dev/null | head -1))"
        return
    fi
    info "Installing sops..."
    local version="3.9.4"
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64)  arch="amd64" ;;
        aarch64) arch="arm64" ;;
    esac
    local os
    os=$(uname -s | tr '[:upper:]' '[:lower:]')
    curl -sL "https://github.com/getsops/sops/releases/download/v${version}/sops-v${version}.${os}.${arch}" \
        -o /usr/local/bin/sops
    chmod +x /usr/local/bin/sops
    ok "sops installed"
}

# ── Step 2: Generate age keypair ──────────────────────────────────────────────

setup_keys() {
    mkdir -p "$SOPS_DIR"
    chmod 700 "$SOPS_DIR"

    if [[ -f "$KEY_FILE" ]]; then
        ok "Age key already exists at $KEY_FILE"
    else
        info "Generating age keypair..."
        age-keygen -o "$KEY_FILE" 2>/dev/null
        chmod 600 "$KEY_FILE"
        ok "Age key generated at $KEY_FILE"
    fi

    # Extract public key
    local pubkey
    pubkey=$(grep -o 'age1[a-z0-9]*' "$KEY_FILE" | head -1)
    echo "$pubkey" > "$SOPS_DIR/age-public-key.txt"
    ok "Public key: $pubkey"

    # Set SOPS_AGE_KEY_FILE for the current session and shell profile
    export SOPS_AGE_KEY_FILE="$KEY_FILE"

    # Add to shell profile if not already there
    local shell_rc="$HOME/.bashrc"
    [[ -f "$HOME/.zshrc" ]] && shell_rc="$HOME/.zshrc"
    if ! grep -q 'SOPS_AGE_KEY_FILE' "$shell_rc" 2>/dev/null; then
        echo "" >> "$shell_rc"
        echo "# claude-sops: age key for SOPS decryption" >> "$shell_rc"
        echo "export SOPS_AGE_KEY_FILE=\"$KEY_FILE\"" >> "$shell_rc"
        ok "Added SOPS_AGE_KEY_FILE to $shell_rc"
    fi
}

# ── Step 3: Create .sops.yaml ─────────────────────────────────────────────────

setup_sops_config() {
    local pubkey
    pubkey=$(cat "$SOPS_DIR/age-public-key.txt")

    if [[ -f "$SOPS_CONFIG" ]]; then
        warn ".sops.yaml already exists at $SOPS_CONFIG — skipping"
        return
    fi

    cat > "$SOPS_CONFIG" <<EOF
creation_rules:
  - path_regex: '\.env.*'
    age: '$pubkey'
  - path_regex: '\.sops\.'
    age: '$pubkey'
  - path_regex: 'secrets\.ya?ml'
    age: '$pubkey'
EOF
    ok "Created $SOPS_CONFIG"
}

# ── Step 4: Create secrets directory ──────────────────────────────────────────

setup_secrets_dir() {
    local secrets_dir="$SOPS_DIR/secrets"
    mkdir -p "$secrets_dir"
    chmod 700 "$secrets_dir"

    # Create empty secrets env file if it doesn't exist
    if [[ ! -f "$secrets_dir/.env.sops" ]]; then
        echo "# claude-sops managed secrets" > "$secrets_dir/.env"
        cd "$secrets_dir"
        sops -e -i .env
        mv .env .env.sops
        ok "Created encrypted secrets store at $secrets_dir/.env.sops"
    else
        ok "Secrets store already exists"
    fi
}

# ── Step 5: Install proxy & scripts ───────────────────────────────────────────

install_scripts() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    # Create bin directory
    mkdir -p "$SOPS_DIR/bin"

    # Symlink scripts
    for script in add-secret list-secrets web-input proxy; do
        if [[ -f "$script_dir/bin/$script" ]]; then
            ln -sf "$script_dir/bin/$script" "$SOPS_DIR/bin/$script"
        fi
    done

    # Add to PATH if not already there
    local shell_rc="$HOME/.bashrc"
    [[ -f "$HOME/.zshrc" ]] && shell_rc="$HOME/.zshrc"
    if ! grep -q 'claude-sops/bin' "$shell_rc" 2>/dev/null; then
        echo "export PATH=\"$SOPS_DIR/bin:\$PATH\"" >> "$shell_rc"
        ok "Added claude-sops/bin to PATH in $shell_rc"
    fi

    ok "Scripts installed to $SOPS_DIR/bin/"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║   claude-sops: Secret Management Setup   ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
    echo ""

    install_age
    install_sops
    setup_keys
    setup_sops_config
    setup_secrets_dir
    install_scripts

    echo ""
    echo -e "${GREEN}━━━ Setup complete! ━━━${NC}"
    echo ""
    echo "  Secrets dir:  $SOPS_DIR/secrets/"
    echo "  Age key:      $KEY_FILE"
    echo "  Public key:   $(cat "$SOPS_DIR/age-public-key.txt")"
    echo ""
    echo "  Quick start:"
    echo "    Add a secret:     claude-sops add <KEY> <VALUE>"
    echo "    Web input:        claude-sops web-input"
    echo "    Start proxy:      claude-sops proxy"
    echo "    List secrets:     claude-sops list"
    echo ""
    echo "  Reload your shell:  source ~/.bashrc"
    echo ""
}

main "$@"

# claude-sops

Secret management for [Claude Code](https://claude.ai/code) running on a VPS. Two-tier encryption with SOPS/age — from API keys to private keys.

## The Problem

Claude Code on a VPS needs access to secrets (API keys, tokens, credentials). Storing them in plaintext `.env` files is insecure. Hardcoding them in conversations exposes them to the LLM provider.

## The Solution

**claude-sops** provides two tiers of secret management:

| | Tier 1 — Standard | Tier 2 — Zero-Knowledge |
|---|---|---|
| **Storage** | SOPS/age encrypted at rest | SOPS/age encrypted at rest |
| **Access** | Claude decrypts at runtime | Local proxy injects secret |
| **LLM sees secret?** | Yes (in API call) | **No, never** |
| **Use for** | API keys, tokens | Private keys, signing keys |
| **How Claude uses it** | `claude-sops get KEY` | `curl localhost:9999/route/...` |

## Quick Start

```bash
# Clone
git clone https://github.com/4rdiii/claude-sops.git
cd claude-sops

# Setup (installs age + sops, generates keys, creates config)
chmod +x setup.sh
./setup.sh

# Reload shell
source ~/.bashrc

# Add a Tier 1 secret (Claude can decrypt)
claude-sops add VERCEL_TOKEN "vcp_abc123..."

# Add a Tier 2 secret (Claude never sees it)
claude-sops add-private PRIVATE_KEY "0xdeadbeef..."

# Or use the web form (nice for mobile/browser input)
claude-sops web-input
# Opens http://your-ip:8888 — fill in the form, done.

# List secrets (keys only, no values)
claude-sops list

# Start the zero-knowledge proxy for Tier 2
claude-sops proxy
```

## How It Works

### Setup
```
setup.sh
├── Installs age (encryption) + sops (secret management)
├── Generates an age keypair (~/.claude-sops/age-key.txt)
├── Creates .sops.yaml config pointing to your key
└── Creates encrypted secret stores (tier1.env.sops, tier2.env.sops)
```

### Adding Secrets

**Option A: CLI**
```bash
claude-sops add API_KEY "sk-abc123"         # Tier 1
claude-sops add-private SIGNING_KEY "0x..."  # Tier 2
```

**Option B: Web Form**
```bash
claude-sops web-input [--port 8888]
```
Opens a web form at `http://your-ip:8888`. Enter key name, value, and tier. Secret is encrypted immediately. No values are logged.

### Using Secrets

**Tier 1** — Claude decrypts directly:
```bash
# In Claude's bash tool:
export API_KEY=$(claude-sops get API_KEY)
curl -H "Authorization: Bearer $API_KEY" https://api.example.com/data
```

**Tier 2** — Through the proxy:
```bash
# Start proxy (runs in background)
claude-sops proxy &

# Claude calls the proxy instead of the real API:
curl http://localhost:9999/example-api/v1/data
# Proxy injects the real auth header and forwards to https://api.example.com/v1/data
# Claude only sees the response, never the secret
```

### Proxy Configuration

Edit `~/.claude-sops/proxy-routes.json`:

```json
{
  "github": {
    "target": "https://api.github.com",
    "secret_key": "GITHUB_TOKEN",
    "inject_as": "header",
    "inject_name": "Authorization",
    "inject_prefix": "token "
  },
  "openai": {
    "target": "https://api.openai.com",
    "secret_key": "OPENAI_API_KEY",
    "inject_as": "bearer"
  },
  "moralis": {
    "target": "https://deep-index.moralis.io/api/v2.2",
    "secret_key": "MORALIS_API_KEY",
    "inject_as": "header",
    "inject_name": "X-API-Key"
  }
}
```

Injection modes:
- `bearer` — adds `Authorization: Bearer <secret>` header
- `header` — adds `<inject_name>: <inject_prefix><secret>` header
- `query` — appends `?<inject_name>=<secret>` to URL

### Transaction Signing (Private Keys)

For blockchain private keys, the proxy can **sign and send transactions** without Claude ever seeing the key:

```json
{
  "my-wallet": {
    "type": "signer",
    "secret_key": "MY_PRIVATE_KEY",
    "rpc_url": "https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY",
    "chain_id": 1,
    "max_value": "0.1",
    "allowed_contracts": ["0x..."],
    "require_confirmation": false
  }
}
```

```bash
# Check wallet address & balance (Claude never sees the private key)
curl http://localhost:9999/sign/my-wallet/address

# Sign and send a transaction
curl -X POST http://localhost:9999/sign/my-wallet/send \
  -H "Content-Type: application/json" \
  -d '{"to": "0xRecipient...", "value": 100000000000000000}'
# → Returns tx hash + explorer link. Private key never leaves the proxy.
```

**Safety features:**
- `max_value` — caps ETH per transaction (default: 0.1 ETH). Prevents catastrophic sends.
- `allowed_contracts` — whitelist of destination addresses. Empty = any address allowed.
- `require_confirmation` — if `true`, returns unsigned tx for human review before signing.
- **Audit log** — all transactions logged to `~/.claude-sops/tx-log.jsonl` (without private keys).
- EIP-1559 auto-detection — uses `maxFeePerGas` when available, falls back to legacy gas price.
- Gas estimation with 20% buffer.

**Signing endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sign/<wallet>/address` | GET | Wallet address + balance |
| `/sign/<wallet>/balance` | GET | Current balance |
| `/sign/<wallet>/nonce` | GET | Current nonce |
| `/sign/<wallet>/send` | POST | Sign + broadcast tx |
| `/sign/<wallet>/sign` | POST | Sign only (return raw signed tx) |

**Transaction parameters (POST body):**
```json
{
  "to": "0xRecipientAddress",
  "value": 100000000000000000,
  "data": "0xcalldata...",
  "gas": 21000,
  "maxFeePerGas": 30000000000,
  "maxPriorityFeePerGas": 1000000000
}
```
Only `to` is required. Gas and fees are auto-estimated if omitted.

## Security Model

### What's protected

| Threat | Protected? | How |
|--------|-----------|-----|
| Secrets in git repos | Yes | SOPS encryption, `.gitignore` |
| Secrets readable on disk | Yes | age encryption at rest |
| Unauthorized VPS access | Yes | age key required to decrypt |
| LLM provider sees Tier 1 secrets | No | Decrypted value passes through API |
| LLM provider sees Tier 2 secrets | **Yes** | Proxy handles secret, LLM only sees response |
| Compromised Claude session | Partial | Tier 2 secrets still protected via proxy |

### Trust boundaries

```
┌──────────────────────────────────────────────┐
│ Your VPS                                      │
│                                               │
│  ┌─────────────┐    ┌──────────────────────┐ │
│  │ Claude Code  │    │ claude-sops proxy    │ │
│  │             │───>│ (localhost:9999)      │ │
│  │ Sees: route │    │                      │ │
│  │ name + API  │<───│ Injects real secret  │ │
│  │ response    │    │ from SOPS store      │ │
│  └──────┬──────┘    └──────────┬───────────┘ │
│         │                      │              │
│         │ Tier 1: decrypted    │ Tier 2: only │
│         │ secret in API call   │ proxy touches│
│         ▼                      ▼              │
│  ┌─────────────────────────────────────────┐ │
│  │ SOPS-encrypted store (~/.claude-sops/)   │ │
│  │ tier1.env.sops  |  tier2.env.sops        │ │
│  └─────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
         │
         │ Tier 1 secrets visible
         │ Tier 2 secrets NOT visible
         ▼
┌──────────────────┐
│ Anthropic API    │
│ (LLM server)    │
└──────────────────┘
```

### Recommendations

- **API keys for external services** (Vercel, Moralis, etc.) → Tier 1 is fine
- **Private keys, wallet seeds** → Always Tier 2
- **OAuth tokens** → Tier 1 for read-only, Tier 2 for write access
- **Signing keys** → Always Tier 2

## File Structure

```
claude-sops/
├── setup.sh              # One-command setup
├── CLAUDE.md             # Instructions for Claude Code
├── README.md             # This file
├── bin/
│   ├── claude-sops       # Main CLI entry point
│   ├── add-secret        # Add/update secrets
│   ├── get-secret        # Decrypt Tier 1 secret
│   ├── list-secrets      # List key names
│   ├── remove-secret     # Remove a secret
│   ├── rotate-keys       # Rotate age keypair
│   ├── export-secrets    # Export Tier 1 as plaintext
│   └── status            # Health check
├── web-input/
│   ├── server.py         # One-shot web form server
│   └── index.html        # Secret input form
├── proxy/
│   ├── server.py         # Zero-knowledge proxy server
│   └── signer.py         # Transaction signing module
└── hooks/
    ├── secret_leak_check.py  # Stop hook: detects leaked Tier 2 values
    ├── tier2_guard.py        # PreToolUse hook: blocks Tier 2 access attempts
    └── pre_session.sh        # Auto-starts proxy, lists available secrets
```

After setup, secrets are stored in:
```
~/.claude-sops/
├── age-key.txt           # Private key (NEVER share)
├── age-public-key.txt    # Public key
├── bin/                  # Symlinked scripts
├── secrets/
│   ├── tier1.env.sops    # Tier 1 encrypted secrets
│   └── tier2.env.sops    # Tier 2 encrypted secrets
└── proxy-routes.json     # Proxy route configuration
```

## Integration with Claude Code

Add to your project's `CLAUDE.md`:

```markdown
## Secret Management

This project uses claude-sops for secret management.

### Available Tier 1 secrets:
- VERCEL_TOKEN — Vercel deployment token
- MORALIS_API_KEY — Moralis API access

### Tier 2 proxy routes (localhost:9999):
- /github/* — GitHub API (injects token)
- /openai/* — OpenAI API (injects key)

To use a Tier 1 secret:
\`\`\`bash
export TOKEN=$(claude-sops get VERCEL_TOKEN)
\`\`\`

To use a Tier 2 secret (via proxy):
\`\`\`bash
curl http://localhost:9999/github/repos/user/repo
\`\`\`
```

## License

MIT

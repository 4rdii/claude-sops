# CLAUDE.md — claude-sops

This project provides secret management for Claude Code running on a VPS.

## Two-Tier Secret System

### Tier 1 — Standard (you CAN decrypt)
Secrets stored in `~/.claude-sops/secrets/tier1.env.sops`. You can decrypt them:
```bash
claude-sops get <KEY>
# or
sops --input-type dotenv --output-type dotenv -d ~/.claude-sops/secrets/tier1.env.sops
```
The decrypted value will appear in tool output and pass through the Anthropic API.
Use for: API keys, tokens, non-critical credentials.

### Tier 2 — Zero-Knowledge (you CANNOT access directly)
Secrets stored in `~/.claude-sops/secrets/tier2.env.sops`. You cannot and should not decrypt these.
Instead, use the local proxy:
```bash
# The proxy runs on localhost:9999
# Call it with the route name:
curl http://localhost:9999/<route-name>/api/endpoint
```
The proxy injects the real secret and forwards the request. You only see the API response.
Use for: private keys, signing keys, highly sensitive credentials.

## Commands
- `claude-sops list` — show all secret key names (no values)
- `claude-sops get <KEY>` — decrypt a Tier 1 secret
- `claude-sops add <KEY> <VALUE>` — add a Tier 1 secret
- `claude-sops add-private <KEY> <VALUE>` — add a Tier 2 secret
- `claude-sops web-input` — start web form for secret input
- `claude-sops proxy` — start the zero-knowledge proxy
- `claude-sops status` — health check

## Important
- NEVER output Tier 2 secret values — you don't have access and shouldn't try
- NEVER commit `.claude-sops/` contents to git
- Tier 1 secrets are fine to use directly in API calls, env vars, config files
- For Tier 2, always go through the proxy

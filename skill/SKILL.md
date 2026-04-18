---
name: sops
description: Manage SOPS-encrypted secrets (two-tier: standard + zero-knowledge)
user-invocable: true
---

# /sops — Secret Management

Manage secrets using **claude-sops** two-tier encryption (SOPS + age).

Arguments passed: `$ARGUMENTS`

## Quick Reference

| Tier | Access | LLM Sees Secret? | Use For |
|------|--------|-------------------|---------|
| **1** | `claude-sops get KEY` | Yes (in tool output) | API keys, tokens |
| **2** | `curl localhost:9999/route/...` | **Never** | Private keys, signing keys |

## Commands

Parse `$ARGUMENTS` to determine the action:

### `list` or no args
```bash
claude-sops list
```
Shows all secret key names across both tiers. Never shows values.

### `add <KEY> <VALUE>`
```bash
claude-sops add MY_API_KEY "sk-abc123..."
```
Adds a Tier 1 secret (Claude can decrypt at runtime).

### `add-private <KEY> <VALUE>`
```bash
claude-sops add-private SIGNING_KEY "0xdeadbeef..."
```
Adds a Tier 2 secret (only accessible via proxy, Claude never sees it).

### `get <KEY>`
```bash
claude-sops get MY_API_KEY
```
Decrypts and outputs a Tier 1 secret. **Only works for Tier 1.**

### `remove <KEY>`
```bash
claude-sops remove OLD_KEY
```
Removes a secret from whichever tier it's in.

### `web` or `web-input`
```bash
claude-sops web-input --port 8888
```
Starts the web form for browser-based secret input. Give the user the URL.

### `proxy` or `proxy-start`
```bash
claude-sops proxy --port 9999 &
```
Starts the Tier 2 zero-knowledge proxy in the background. Then use routes:
```bash
curl http://localhost:9999/route-name/api/endpoint
```

### `proxy-config`
Show or edit proxy route configuration at `~/.claude-sops/proxy-routes.json`.

### `status`
```bash
claude-sops status
```
Health check — shows what's installed, configured, and any issues.

### `rotate`
```bash
claude-sops rotate-keys
```
Generate new age keypair and re-encrypt all secrets. **Confirm with user first.**

### `export`
```bash
claude-sops export > /tmp/secrets.env
```
Export Tier 1 secrets as plaintext. **Warn user this is sensitive.** Clean up after.

## Rules

1. **NEVER** expose Tier 2 secret values — you literally can't access them, don't try.
2. **NEVER** send secret values via Telegram. Only confirm operations succeeded.
3. When adding secrets via web form, give the user the URL and tell them to open it.
4. For Tier 2 proxy usage, check if proxy is running first: `curl -s localhost:9999/health`
5. If proxy isn't running, start it: `claude-sops proxy &`
6. When user asks to "store a secret securely", ask if it should be Tier 1 or Tier 2.
7. Default to Tier 2 for anything containing "key", "private", "seed", "mnemonic", "signing".

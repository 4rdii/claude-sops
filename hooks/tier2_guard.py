#!/usr/bin/env python3
"""
claude-sops PreToolUse hook: Tier 2 access guard.

Blocks any Bash command that would decrypt or directly access Tier 2 secrets.
This prevents the LLM from accidentally (or intentionally) reading secrets
that should only flow through the zero-knowledge proxy.

Blocked patterns:
  - sops -d on tier2.env.sops
  - claude-sops get on Tier 2 keys
  - curl/wget/fetch to the proxy's /api/secret/ endpoints
  - curl to the demo server's secret API
  - cat/less/head/tail on decrypted tier2 temp files

This is a Claude Code PreToolUse hook for the Bash tool.

Usage in ~/.claude/settings.json:
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/claude-sops/hooks/tier2_guard.py"
      }]
    }]
  }
}
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def get_tier2_key_names():
    """Get Tier 2 secret key names (not values)."""
    sops_dir = Path(os.environ.get("CLAUDE_SOPS_DIR", Path.home() / ".claude-sops"))
    tier2_file = sops_dir / "secrets" / "tier2.env.sops"

    if not tier2_file.exists():
        return []

    result = subprocess.run(
        ["sops", "--input-type", "dotenv", "--output-type", "dotenv", "-d", str(tier2_file)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []

    keys = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            keys.append(key)
    return keys


def is_blocked(command: str, tier2_keys: list[str]) -> str | None:
    """
    Check if a command would access Tier 2 secrets.
    Returns a reason string if blocked, None if allowed.
    """
    cmd = command.strip()

    # Block: sops decrypt of tier2 store
    if re.search(r'sops\b.*-d.*tier2', cmd) or re.search(r'sops\b.*--decrypt.*tier2', cmd):
        return "Direct decryption of Tier 2 secret store is blocked. Use the proxy for Tier 2 secrets."

    # Block: claude-sops get on Tier 2 keys
    for key in tier2_keys:
        if re.search(rf'claude-sops\s+get\s+{re.escape(key)}', cmd):
            return f"Direct access to Tier 2 secret '{key}' is blocked. Use the proxy instead."
        if re.search(rf'get-secret\s+{re.escape(key)}', cmd):
            return f"Direct access to Tier 2 secret '{key}' is blocked. Use the proxy instead."

    # Block: curl/wget to proxy secret endpoints or demo secret API
    if re.search(r'(curl|wget|http)\b.*(/api/secret/)', cmd):
        return "Direct HTTP access to secret API endpoints is blocked. Tier 2 secrets must flow through the proxy to external services, not be fetched by the LLM."

    # Block: reading the raw decrypted tier2 file
    if re.search(r'(cat|less|more|head|tail|bat)\b.*tier2', cmd):
        return "Reading Tier 2 secret files directly is blocked."

    # Block: export-secrets for tier 2
    if re.search(r'export-secrets?\b.*tier2', cmd) or re.search(r'export-secrets?\b.*--tier\s*2', cmd):
        return "Exporting Tier 2 secrets is blocked. They should never leave the encrypted store except through the proxy."

    return None


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    # Get Tier 2 key names
    tier2_keys = get_tier2_key_names()

    reason = is_blocked(command, tier2_keys)
    if reason:
        result = {
            "decision": "block",
            "reason": f"[claude-sops] BLOCKED: {reason}"
        }
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()

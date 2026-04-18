#!/usr/bin/env python3
"""
claude-sops Stop hook: Secret leak detection.

Scans the conversation transcript for accidentally exposed Tier 2 secrets.
If a Tier 2 secret value appears in any tool output, blocks the stop and warns.

This is a Claude Code Stop hook. It reads the transcript from stdin (JSON)
and outputs a decision.

Usage in ~/.claude/settings.json:
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/claude-sops/hooks/secret_leak_check.py"
      }]
    }]
  }
}
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def get_tier2_secrets():
    """Decrypt Tier 2 secrets to check for leaks."""
    sops_dir = Path(os.environ.get("CLAUDE_SOPS_DIR", Path.home() / ".claude-sops"))
    tier2_file = sops_dir / "secrets" / "tier2.env.sops"

    if not tier2_file.exists():
        return {}

    result = subprocess.run(
        ["sops", "--input-type", "dotenv", "--output-type", "dotenv", "-d", str(tier2_file)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {}

    secrets = {}
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip()
            # Only check secrets longer than 8 chars to avoid false positives
            if len(value) > 8:
                secrets[key.strip()] = value
    return secrets


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        # Can't parse input, allow stop
        sys.exit(0)

    transcript_path = input_data.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        sys.exit(0)

    # Load Tier 2 secrets
    secrets = get_tier2_secrets()
    if not secrets:
        # No Tier 2 secrets to check
        sys.exit(0)

    # Scan last 20 messages in transcript for leaked values
    leaked = []
    try:
        with open(transcript_path) as f:
            lines = f.readlines()

        # Only check recent messages (last 20)
        recent = lines[-20:] if len(lines) > 20 else lines

        transcript_text = "".join(recent)

        for key, value in secrets.items():
            if value in transcript_text:
                leaked.append(key)
    except Exception:
        sys.exit(0)

    if leaked:
        result = {
            "decision": "block",
            "reason": (
                f"[claude-sops] SECURITY WARNING: Tier 2 secret(s) detected in conversation: "
                f"{', '.join(leaked)}. These should NEVER appear in tool output. "
                f"Do NOT repeat the values. Acknowledge the leak and advise the user to rotate these secrets."
            )
        }
        print(json.dumps(result))
        sys.exit(0)

    # No leaks detected
    sys.exit(0)


if __name__ == "__main__":
    main()

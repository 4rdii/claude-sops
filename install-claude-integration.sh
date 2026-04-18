#!/usr/bin/env bash
set -euo pipefail

# Install claude-sops skill and hooks into Claude Code.
# Run after setup.sh to wire up Claude Code integration.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SKILLS_DIR="$CLAUDE_DIR/skills"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[info]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC} $*"; }

# ── Install Skill ────────────────────────────────────────────────────────────

install_skill() {
    mkdir -p "$SKILLS_DIR/sops"
    ln -sf "$REPO_DIR/skill/SKILL.md" "$SKILLS_DIR/sops/SKILL.md"
    ok "Installed /sops skill -> $SKILLS_DIR/sops/"
}

# ── Install Hooks ────────────────────────────────────────────────────────────

install_hooks() {
    chmod +x "$REPO_DIR/hooks/pre_session.sh"
    chmod +x "$REPO_DIR/hooks/secret_leak_check.py"

    if [[ ! -f "$SETTINGS_FILE" ]]; then
        echo '{}' > "$SETTINGS_FILE"
    fi

    # Use Python to safely merge hooks into settings.json
    python3 -c "
import json
from pathlib import Path

settings_file = Path('$SETTINGS_FILE')
settings = json.loads(settings_file.read_text())

hooks = settings.setdefault('hooks', {})

# Add Stop hook for leak detection (if not already present)
stop_hooks = hooks.setdefault('Stop', [])
leak_cmd = 'python3 $REPO_DIR/hooks/secret_leak_check.py'

# Check if already installed
already_installed = any(
    any(h.get('command', '') == leak_cmd for h in entry.get('hooks', []))
    for entry in stop_hooks
)

if not already_installed:
    stop_hooks.append({
        'matcher': '',
        'hooks': [{
            'type': 'command',
            'command': leak_cmd
        }]
    })

settings_file.write_text(json.dumps(settings, indent=2) + '\n')
print('Hooks updated in settings.json')
"
    ok "Installed Stop hook (secret leak detection)"
    info "Pre-session hook available at: $REPO_DIR/hooks/pre_session.sh"
    info "  To use: add to your shell profile or Claude Code PreToolUse hook"
}

# ── Main ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}Installing Claude Code integration...${NC}"
echo ""

install_skill
install_hooks

echo ""
echo -e "${GREEN}Done!${NC}"
echo ""
echo "  Skill:  /sops — manage secrets from any Claude Code session"
echo "  Hook:   Stop hook checks for Tier 2 secret leaks in conversation"
echo ""
echo "  Try it: type '/sops list' in Claude Code"
echo ""

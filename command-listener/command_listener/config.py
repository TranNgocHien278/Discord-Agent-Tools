"""Configuration: token loading and command routing."""

from __future__ import annotations

import re
from pathlib import Path

ENV_FILE = Path.home() / ".hermes" / ".env"

GUILD_ID = "1509937779176640662"

INTENTS = (1 | 512 | 32768)  # GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT = 33281

COMMAND_PREFIXES = ("/", "!")

MAX_MESSAGE_LENGTH = 1900

COMMAND_ROUTES: dict[str, dict[str, str]] = {
    "1509957355549495431": {  # #system-status
        "status": "command_listener.commands.status:handle",
    },
}


def load_token() -> str:
    if not ENV_FILE.exists():
        raise SystemExit(f"Env file not found: {ENV_FILE}")
    text = ENV_FILE.read_text()
    m = re.search(r"^DISCORD_BOT_TOKEN\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    if not m:
        raise SystemExit("DISCORD_BOT_TOKEN not found in .env")
    return m.group(1).strip().strip('"').strip("'")

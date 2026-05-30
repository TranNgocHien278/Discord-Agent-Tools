"""/status command: run system_watchdog.py --force and return its stdout."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIPT = Path.home() / ".hermes" / "scripts" / "system_watchdog.py"
TIMEOUT = 10


async def handle(channel_id: str, content: str, author: dict) -> str:
    """Run system_watchdog.py --force, return stdout (or error message)."""
    if not SCRIPT.exists():
        return f"⚠️ Script not found: {SCRIPT}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "python3",
            str(SCRIPT),
            "--force",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "⚠️ python3 not found on PATH"

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        return f"⚠️ Status check timed out (>{TIMEOUT}s)"

    if proc.returncode != 0:
        err_text = err.decode(errors="replace").strip()[:500] or "(no stderr)"
        return f"⚠️ Script error (rc={proc.returncode}):\n```\n{err_text}\n```"

    output = out.decode(errors="replace").strip()
    return output or "(no output)"

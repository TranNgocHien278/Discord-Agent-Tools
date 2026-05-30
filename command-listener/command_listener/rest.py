"""Discord REST helpers: posting messages with rate-limit handling."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import MAX_MESSAGE_LENGTH

logger = logging.getLogger(__name__)

# Trailing slash matters: httpx merges relative paths against base_url; an
# absolute path like "/foo" would strip "/api/v10" off the base.
API_BASE = "https://discord.com/api/v10/"


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[:MAX_MESSAGE_LENGTH] + "\n...(truncated)"


class DiscordREST:
    def __init__(self, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "command-listener (https://github.com/TranNgocHien278/Discord-Agent-Tools, 0.1.0)",
            },
            timeout=15.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_message(self, channel_id: str, content: str) -> None:
        body = {"content": _truncate(content)}
        path = f"channels/{channel_id}/messages"
        for attempt in range(5):
            try:
                r = await self._client.post(path, json=body)
            except httpx.HTTPError as e:
                logger.warning("POST %s network error: %s (attempt %d)", path, e, attempt + 1)
                await asyncio.sleep(min(2 ** attempt, 30))
                continue

            if r.status_code == 429:
                try:
                    retry_after = float(r.json().get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                logger.warning("Rate limited on POST %s, retry in %.2fs", path, retry_after)
                await asyncio.sleep(retry_after)
                continue

            if 500 <= r.status_code < 600:
                logger.warning(
                    "Server error %d on POST %s (attempt %d)",
                    r.status_code, path, attempt + 1,
                )
                await asyncio.sleep(min(2 ** attempt, 30))
                continue

            if r.status_code >= 400:
                logger.error("POST %s -> %d: %s", path, r.status_code, r.text[:500])
                return

            return
        logger.error("POST %s gave up after retries", path)

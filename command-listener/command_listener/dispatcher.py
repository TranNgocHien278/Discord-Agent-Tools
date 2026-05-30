"""Route MESSAGE_CREATE events to command handlers."""

from __future__ import annotations

import importlib
import logging
from typing import Awaitable, Callable

from .config import COMMAND_PREFIXES, COMMAND_ROUTES

logger = logging.getLogger(__name__)

Handler = Callable[[str, str, dict], Awaitable[str]]
Poster = Callable[[str, str], Awaitable[None]]


class Dispatcher:
    def __init__(
        self,
        routes: dict[str, dict[str, str]],
        post_response: Poster,
    ) -> None:
        self.routes = routes
        self.post_response = post_response
        self._handler_cache: dict[str, Handler] = {}

    def _resolve(self, dotted: str) -> Handler:
        if dotted in self._handler_cache:
            return self._handler_cache[dotted]
        module_name, _, attr = dotted.partition(":")
        if not attr:
            module_name, _, attr = dotted.rpartition(".")
        module = importlib.import_module(module_name)
        handler = getattr(module, attr)
        self._handler_cache[dotted] = handler
        return handler

    @staticmethod
    def _parse_command(content: str) -> str | None:
        if not content:
            return None
        if content[0] not in COMMAND_PREFIXES:
            return None
        rest = content[1:].strip()
        if not rest:
            return None
        return rest.split(maxsplit=1)[0].lower()

    async def on_message_create(self, event: dict) -> None:
        try:
            author = event.get("author") or {}
            if author.get("bot"):
                return

            channel_id = str(event.get("channel_id") or "")
            channel_routes = self.routes.get(channel_id)
            if not channel_routes:
                return

            content = event.get("content") or ""
            command = self._parse_command(content)
            if not command:
                return

            dotted = channel_routes.get(command)
            if not dotted:
                return

            logger.info(
                "Running command '%s' (channel=%s, user=%s)",
                command,
                channel_id,
                author.get("username") or author.get("id"),
            )

            handler = self._resolve(dotted)
            try:
                response = await handler(channel_id, content, author)
            except Exception:
                logger.exception("Handler '%s' raised", dotted)
                response = f"⚠️ Command '{command}' crashed (see service logs)"

            if response:
                await self.post_response(channel_id, response)
        except Exception:
            logger.exception("Dispatcher error on MESSAGE_CREATE")

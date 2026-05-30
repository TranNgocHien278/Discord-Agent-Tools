"""Discord Gateway WebSocket client: connect, identify, heartbeat, resume."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

GATEWAY_VERSION = 10
GATEWAY_QUERY = f"?v={GATEWAY_VERSION}&encoding=json"

# Opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Close codes that mean: do NOT resume, re-identify fresh
NON_RESUMABLE_CLOSE_CODES = {
    4004,  # auth failed (token bad — fatal)
    4010,  # invalid shard
    4011,  # sharding required
    4012,  # invalid api version
    4013,  # invalid intents
    4014,  # disallowed intents
}
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}

EventHandler = Callable[[dict], Awaitable[None]]


class GatewayClient:
    def __init__(
        self,
        token: str,
        intents: int,
        rest_client: httpx.AsyncClient,
        on_message_create: EventHandler,
    ) -> None:
        self.token = token
        self.intents = intents
        self._rest = rest_client
        self._on_message_create = on_message_create

        self._gateway_url: str | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._heartbeat_interval: float | None = None
        self._sequence: int | None = None
        self._session_id: str | None = None
        self._resume_gateway_url: str | None = None
        self._last_ack: bool = True
        self._heartbeat_task: asyncio.Task | None = None

        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _fetch_gateway_url(self) -> str:
        r = await self._rest.get("gateway/bot")
        r.raise_for_status()
        data = r.json()
        return data["url"]

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload))

    async def _heartbeat_loop(self, interval_ms: float) -> None:
        # First beat is jittered as per Discord docs.
        jitter = random.random()
        await asyncio.sleep((interval_ms / 1000.0) * jitter)
        try:
            while True:
                if not self._last_ack:
                    logger.warning("Missed heartbeat ACK, forcing reconnect")
                    if self._ws is not None:
                        await self._ws.close(code=4000)
                    return
                self._last_ack = False
                await self._send({"op": OP_HEARTBEAT, "d": self._sequence})
                await asyncio.sleep(interval_ms / 1000.0)
        except asyncio.CancelledError:
            return
        except ConnectionClosed:
            return

    async def _identify(self) -> None:
        await self._send(
            {
                "op": OP_IDENTIFY,
                "d": {
                    "token": self.token,
                    "intents": self.intents,
                    "properties": {
                        "os": "linux",
                        "browser": "command-listener",
                        "device": "command-listener",
                    },
                },
            }
        )

    async def _resume(self) -> None:
        await self._send(
            {
                "op": OP_RESUME,
                "d": {
                    "token": self.token,
                    "session_id": self._session_id,
                    "seq": self._sequence,
                },
            }
        )

    async def _handle_dispatch(self, t: str, d: dict) -> None:
        if t == "READY":
            self._session_id = d.get("session_id")
            self._resume_gateway_url = d.get("resume_gateway_url")
            user = d.get("user", {})
            logger.info(
                "READY: logged in as %s#%s (id=%s)",
                user.get("username"),
                user.get("discriminator"),
                user.get("id"),
            )
        elif t == "RESUMED":
            logger.info("Session resumed (seq=%s)", self._sequence)
        elif t == "MESSAGE_CREATE":
            await self._on_message_create(d)

    async def _connect_once(self, resume: bool) -> bool:
        """One connect attempt. Returns True if we should attempt to resume next."""
        url = (
            self._resume_gateway_url
            if (resume and self._resume_gateway_url)
            else self._gateway_url
        )
        if url is None:
            url = await self._fetch_gateway_url()
            self._gateway_url = url

        full_url = url + GATEWAY_QUERY
        logger.info("Connecting to gateway %s (resume=%s)", url, resume)

        self._last_ack = True
        try:
            async with websockets.connect(full_url, max_size=2**20) as ws:
                self._ws = ws

                # Expect HELLO
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("op") != OP_HELLO:
                    logger.error("Expected HELLO, got %s", msg)
                    return False
                self._heartbeat_interval = msg["d"]["heartbeat_interval"]
                logger.info("HELLO heartbeat_interval=%sms", self._heartbeat_interval)

                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(self._heartbeat_interval)
                )

                if resume and self._session_id and self._sequence is not None:
                    await self._resume()
                else:
                    await self._identify()

                async for raw in ws:
                    msg = json.loads(raw)
                    op = msg.get("op")
                    s = msg.get("s")
                    t = msg.get("t")
                    d = msg.get("d")

                    if s is not None:
                        self._sequence = s

                    if op == OP_DISPATCH:
                        await self._handle_dispatch(t, d or {})
                    elif op == OP_HEARTBEAT:
                        await self._send({"op": OP_HEARTBEAT, "d": self._sequence})
                    elif op == OP_HEARTBEAT_ACK:
                        self._last_ack = True
                    elif op == OP_RECONNECT:
                        logger.info("Server requested RECONNECT")
                        await ws.close(code=4000)
                        return True  # attempt resume
                    elif op == OP_INVALID_SESSION:
                        resumable = bool(d)
                        logger.warning("INVALID_SESSION (resumable=%s)", resumable)
                        # Wait 1-5s before re-identifying per Discord spec
                        await asyncio.sleep(random.uniform(1.0, 5.0))
                        if not resumable:
                            self._session_id = None
                            self._sequence = None
                        await ws.close(code=4000)
                        return resumable
                    else:
                        logger.debug("Unhandled op=%s", op)
        except ConnectionClosed as e:
            code = e.code
            logger.warning("WebSocket closed: code=%s reason=%s", code, e.reason)
            if code in FATAL_CLOSE_CODES:
                logger.error("Fatal close code %s — stopping", code)
                self._stop.set()
                return False
            if code in NON_RESUMABLE_CLOSE_CODES:
                self._session_id = None
                self._sequence = None
                return False
            return True  # try resume
        except Exception:
            logger.exception("Gateway connection error")
            return True
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._heartbeat_task = None
            self._ws = None

        return True

    async def run(self) -> None:
        backoff = 1.0
        resume = False
        while not self._stop.is_set():
            try:
                want_resume = await self._connect_once(resume=resume)
            except Exception:
                logger.exception("connect_once raised unexpectedly")
                want_resume = False

            if self._stop.is_set():
                break

            # Successful disconnect (clean) resets backoff
            await asyncio.sleep(min(backoff, 60.0) * (0.8 + 0.4 * random.random()))
            backoff = min(backoff * 2, 60.0) if not want_resume else 1.0
            resume = want_resume

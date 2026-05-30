"""Entry point: wire config + REST + gateway + dispatcher, handle signals."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

import httpx

from .config import COMMAND_ROUTES, INTENTS, load_token
from .dispatcher import Dispatcher
from .gateway import GatewayClient
from .rest import API_BASE, DiscordREST

LOG_FILE = Path(__file__).resolve().parent.parent / "command_listener.log"


def setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Don't double-add handlers if reloaded
    root.handlers.clear()

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(fmt)
    root.addHandler(stderr_h)

    try:
        file_h = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_h.setFormatter(fmt)
        root.addHandler(file_h)
    except OSError as e:
        logging.warning("Could not open log file %s: %s", LOG_FILE, e)

    # Quiet noisy libs
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def amain() -> None:
    setup_logging()
    logger = logging.getLogger("command_listener")
    logger.info("Starting command-listener")

    token = load_token()

    rest = DiscordREST(token)
    gateway_rest = httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bot {token}"},
        timeout=15.0,
    )

    dispatcher = Dispatcher(routes=COMMAND_ROUTES, post_response=rest.post_message)

    gateway = GatewayClient(
        token=token,
        intents=INTENTS,
        rest_client=gateway_rest,
        on_message_create=dispatcher.on_message_create,
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal")
        stop.set()
        gateway.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    gateway_task = asyncio.create_task(gateway.run(), name="gateway")
    stop_task = asyncio.create_task(stop.wait(), name="stop")

    try:
        done, pending = await asyncio.wait(
            {gateway_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in done:
            if t is gateway_task and t.exception():
                raise t.exception()  # type: ignore[misc]
    finally:
        gateway.stop()
        gateway_task.cancel()
        for t in (gateway_task, stop_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await rest.aclose()
        await gateway_rest.aclose()
        logger.info("Shutdown complete")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

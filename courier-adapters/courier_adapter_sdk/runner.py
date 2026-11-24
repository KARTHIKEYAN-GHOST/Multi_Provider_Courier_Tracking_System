"""Entry point shared by every adapter: ``run(MyAdapter())``."""

from __future__ import annotations

import asyncio

from .base import Adapter
from .config import WorkerSettings
from .logging import configure_logging
from .worker import WorkerProcess, install_signal_handlers


async def _main(adapter: Adapter) -> None:
    settings = WorkerSettings()  # values come from the environment
    configure_logging(settings.log_format)
    process = WorkerProcess(adapter, settings)
    install_signal_handlers(process)
    await process.run()


def run(adapter: Adapter) -> None:
    asyncio.run(_main(adapter))

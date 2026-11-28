import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ParamSpec, TypeVar

import aiohttp
from ecdsa import VerifyingKey
from fastapi import Request

from app.database.core import Database
from app.settings import Config, Constants

if TYPE_CHECKING:
    from app.services.conversion_runner import ConversionChildren
    from app.services.crypto import TokenCipher
    from app.services.discord_bot import DiscordBot
    from app.services.storage import Storage

ParamsT = ParamSpec("ParamsT")
ResultT = TypeVar("ResultT")


@dataclass
class AppContext:
    """Everything a request handler needs, created once per worker.

    Attributes:
        config: Validated config.yml.
        constants: Validated constants.yml.
        database: Database pool wrapper.
        storage: Object storage client.
        http: Shared outgoing HTTP session.
        discord: Discord bot client.
        cipher: Encrypter for stored provider tokens.
        sonolus_key: Sonolus's ECDSA verifying key.
        executor: Thread pool for blocking work.
        conversion_children: Running chart conversion child processes of this worker.
        audio_jobs: Limits concurrent ffmpeg audio work.
        worker_task: Background worker task, if running.
    """

    config: Config
    constants: Constants
    database: Database
    storage: "Storage"
    http: aiohttp.ClientSession
    discord: "DiscordBot"
    cipher: "TokenCipher"
    sonolus_key: VerifyingKey
    executor: ThreadPoolExecutor
    conversion_children: "ConversionChildren"
    audio_jobs: asyncio.Semaphore
    worker_task: asyncio.Task | None = field(default=None)

    async def run_blocking(
        self, function: Callable[ParamsT, ResultT], *args: ParamsT.args, **kwargs: ParamsT.kwargs
    ) -> ResultT:
        """Run blocking work on the thread pool.

        Args:
            function: Callable to run.
            *args: Positional arguments for it.
            **kwargs: Keyword arguments for it.

        Returns:
            ResultT: What the callable returned.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.executor, functools.partial(function, *args, **kwargs)
        )


def get_context(request: Request) -> AppContext:
    """Return the worker's AppContext.

    Args:
        request: Current request.

    Returns:
        AppContext: The context.
    """
    return request.app.state.context

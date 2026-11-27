import asyncio
import os
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth.sonolus import load_sonolus_key
from app.context import AppContext
from app.database.core import Database
from app.errors import install_error_handlers
from app.routers import (
    accounts,
    chart_social,
    charts,
    global_routes,
    internal,
    me,
    oauth,
    sessions,
)
from app.services.conversion_runner import ConversionChildren
from app.services.crypto import TokenCipher
from app.services.discord_bot import DiscordBot
from app.services.storage import Storage
from app.services.worker import worker_loop
from app.settings import Config, Constants, load_config, load_constants

CONFIG_ENV = "UC_BACKEND_CONFIG"
CONSTANTS_ENV = "UC_BACKEND_CONSTANTS"


def create_app(config: Config, constants: Constants) -> FastAPI:
    """Build the FastAPI application.

    Args:
        config: Validated config.yml.
        constants: Validated constants.yml.

    Returns:
        FastAPI: The application.
    """
    Image.MAX_IMAGE_PIXELS = constants.images.max_pixels

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Open and close per-worker resources.

        Args:
            app: The application.

        Returns:
            AsyncGenerator[None, None]: Lifespan context.
        """
        database = Database()
        await database.open(config.database)
        storage = Storage(config.storage)
        await storage.open()
        http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        discord = DiscordBot(
            config.discord, http, config.sonolus.server_url, config.sonolus.chart_prefix
        )
        context = AppContext(
            config=config,
            constants=constants,
            database=database,
            storage=storage,
            http=http,
            discord=discord,
            cipher=TokenCipher(config.keys.encryption_key),
            sonolus_key=load_sonolus_key(),
            executor=ThreadPoolExecutor(max_workers=16),
            conversion_children=ConversionChildren(),
            audio_jobs=asyncio.Semaphore(config.charts.max_concurrent_audio),
        )
        context.worker_task = asyncio.create_task(worker_loop(context))
        app.state.context = context
        try:
            yield
        finally:
            context.conversion_children.kill_all()
            context.worker_task.cancel()
            await asyncio.gather(context.worker_task, return_exceptions=True)
            await discord.drain()
            await http.close()
            await storage.close()
            await database.close()
            context.executor.shutdown(wait=False)

    debug = config.server.debug
    app = FastAPI(
        title="UntitledCharts API",
        lifespan=lifespan,
        redirect_slashes=False,
        docs_url="/docs" if debug else None,
        redoc_url="/redoc" if debug else None,
        openapi_url="/openapi.json" if debug else None,
    )
    install_error_handlers(app, debug)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["WWW-Authenticate"],
    )
    if not debug:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.server.allowed_hosts)
    for module in (
        sessions,
        me,
        accounts,
        charts,
        chart_social,
        global_routes,
        oauth,
        internal,
    ):
        app.include_router(module.router)
    return app


def create_app_from_env() -> FastAPI:
    """Build the application from the config paths main.py exported.

    Returns:
        FastAPI: The application.
    """
    config = load_config(Path(os.environ.get(CONFIG_ENV, "config.yml")))
    constants = load_constants(Path(os.environ.get(CONSTANTS_ENV, "constants.yml")))
    return create_app(config, constants)

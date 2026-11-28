import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.context import AppContext

STORAGE_PREFIX = "og/"
CONTENT_TYPE = "image/png"

logger = logging.getLogger("uc.backend.og")

_memory: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_renders: asyncio.Semaphore | None = None
_uploads: set[asyncio.Task[None]] = set()


def storage_key(kind: str, inputs: dict[str, Any]) -> str:
    """Build the storage key of an image from everything that changes how it looks.

    Args:
        kind: Image design.
        inputs: Values the image is drawn from.

    Returns:
        str: Key under the link preview image prefix.
    """
    digest = hashlib.sha1(
        json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{STORAGE_PREFIX}{kind}/{digest}.png"


def _recall(context: AppContext, key: str) -> bytes | None:
    """Return an image from this worker's memory while it is still fresh.

    Args:
        context: App context.
        key: Storage key.

    Returns:
        bytes | None: The image, or None when it isn't remembered or is too old.
    """
    remembered = _memory.get(key)
    if remembered is None:
        return None
    if time.time() - remembered[0] >= context.constants.og_images.max_age_seconds:
        return None
    _memory.move_to_end(key)
    return remembered[1]


def _remember(context: AppContext, key: str, png: bytes, drawn_at: float) -> None:
    """Keep an image in this worker's memory, dropping the oldest beyond the limit.

    Args:
        context: App context.
        key: Storage key.
        png: Image.
        drawn_at: Unix time the image was drawn.
    """
    size = context.constants.og_images.memory_cache_size
    if size <= 0:
        return
    _memory[key] = (drawn_at, png)
    _memory.move_to_end(key)
    while len(_memory) > size:
        _memory.popitem(last=False)


async def _upload(context: AppContext, key: str, png: bytes) -> None:
    """Store a drawn image, logging failures since the caller already has it.

    Args:
        context: App context.
        key: Storage key.
        png: Image.
    """
    try:
        await context.storage.put(key, png, CONTENT_TYPE)
    except Exception:
        logger.warning("couldn't store link preview image %s", key, exc_info=True)


async def get_png(
    context: AppContext,
    kind: str,
    inputs: dict[str, Any],
    draw: Callable[[], Awaitable[Callable[[], bytes]]],
) -> bytes:
    """Return a link preview image, drawing it in a limited slot and storing it when no fresh copy exists.

    Args:
        context: App context.
        kind: Image design.
        inputs: Values the image is drawn from; any change gives a new image.
        draw: Coroutine function fetching what the image needs and returning a blocking renderer
            that returns PNG bytes.

    Returns:
        bytes: The PNG.
    """
    global _renders
    constants = context.constants.og_images
    key = storage_key(kind, inputs)
    remembered = _recall(context, key)
    if remembered is not None:
        return remembered
    try:
        stored = await context.storage.get(key)
    except Exception:
        logger.warning("couldn't read link preview image %s", key, exc_info=True)
        stored = None
    if stored is not None:
        data, modified = stored
        age = (datetime.now(timezone.utc) - modified).total_seconds()
        if age < constants.max_age_seconds:
            _remember(context, key, data, time.time() - age)
            return data
    if _renders is None:
        _renders = asyncio.Semaphore(constants.max_concurrent_renders)
    async with _renders:
        remembered = _recall(context, key)
        if remembered is not None:
            return remembered
        render = await draw()
        png = await context.run_blocking(render)
    _remember(context, key, png, time.time())
    if len(_uploads) >= constants.max_pending_uploads:
        logger.warning("too many link preview uploads pending, not storing %s", key)
        return png
    task = asyncio.create_task(_upload(context, key, png))
    _uploads.add(task)
    task.add_done_callback(_uploads.discard)
    return png

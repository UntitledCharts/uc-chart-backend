import asyncio
import logging
from collections.abc import Iterable
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, cast

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from app.settings import StorageConfig

DELETE_BATCH_SIZE = 1000

logger = logging.getLogger("uc.backend.storage")


class StorageError(Exception):
    """Raised when storage refuses part of a request."""


class Storage:
    """S3-compatible storage client shared by a worker.

    Attributes:
        config: Storage settings.
    """

    def __init__(self, config: StorageConfig) -> None:
        """Create an unopened storage client.

        Args:
            config: Storage settings.
        """
        self.config = config
        self._stack = AsyncExitStack()
        self._client: Any = None

    async def open(self) -> None:
        """Open the S3 client."""
        session = aioboto3.Session(
            aws_access_key_id=self.config.access_key_id,
            aws_secret_access_key=self.config.secret_access_key,
            region_name=self.config.region,
        )
        client_context = cast(Any, session.client("s3", endpoint_url=self.config.endpoint))
        self._client = await self._stack.enter_async_context(client_context)

    async def close(self) -> None:
        """Close the S3 client."""
        await self._stack.aclose()
        self._client = None

    def public_url(self, key: str) -> str:
        """Build the public URL of a stored file.

        Args:
            key: Object key.

        Returns:
            str: The URL.
        """
        return f"{self.config.public_url.rstrip('/')}/{key}"

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """Upload a file.

        Args:
            key: Object key.
            data: File contents.
            content_type: MIME type.
        """
        await self._client.put_object(
            Bucket=self.config.bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get(self, key: str) -> tuple[bytes, datetime] | None:
        """Download a file.

        Args:
            key: Object key.

        Returns:
            tuple[bytes, datetime] | None: Contents and last modified time, or None when the key
                doesn't exist.
        """
        try:
            response = await self._client.get_object(Bucket=self.config.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        async with response["Body"] as body:
            data = await body.read()
        return data, response["LastModified"]

    async def put_many(self, files: Iterable[tuple[str, bytes, str]]) -> None:
        """Upload several files concurrently, raising the first failure once every upload stopped.

        Args:
            files: Tuples of key, contents and MIME type.
        """
        results = await asyncio.gather(
            *(self.put(key, data, content_type) for key, data, content_type in files),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def delete_keys(self, keys: Iterable[str]) -> None:
        """Delete files by key, failing when storage reports any key it couldn't delete.

        Args:
            keys: Object keys.
        """
        unique = list(dict.fromkeys(keys))
        failed: list[str] = []
        for start in range(0, len(unique), DELETE_BATCH_SIZE):
            batch = unique[start : start + DELETE_BATCH_SIZE]
            response = await self._client.delete_objects(
                Bucket=self.config.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            failed.extend(
                f"{error.get('Key', '?')} ({error.get('Code', 'unknown error')})"
                for error in response.get("Errors", [])
            )
        if failed:
            raise StorageError(
                f"{len(failed)} file(s) couldn't be deleted: {', '.join(failed[:10])}"
            )

    async def list_keys(self, prefix: str = "") -> list[str]:
        """List every key under a prefix.

        Args:
            prefix: Key prefix.

        Returns:
            list[str]: The keys.
        """
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    async def list_objects(self, prefix: str = "") -> list[tuple[str, int]]:
        """List every key and size under a prefix.

        Args:
            prefix: Key prefix.

        Returns:
            list[tuple[str, int]]: Keys and sizes in bytes.
        """
        objects: list[tuple[str, int]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            objects.extend((item["Key"], int(item["Size"])) for item in page.get("Contents", []))
        return objects

    async def list_keys_older_than(self, prefix: str, cutoff: datetime) -> list[str]:
        """List the keys under a prefix last modified before a time.

        Args:
            prefix: Key prefix.
            cutoff: Aware time; keys modified before it are listed.

        Returns:
            list[str]: The keys.
        """
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            keys.extend(
                item["Key"] for item in page.get("Contents", []) if item["LastModified"] < cutoff
            )
        return keys

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every file under a prefix.

        Args:
            prefix: Key prefix; must not be empty.

        Returns:
            int: Number of files deleted.
        """
        if not prefix:
            raise ValueError("refusing to delete an empty prefix")
        keys = await self.list_keys(prefix)
        await self.delete_keys(keys)
        return len(keys)

    async def discard_keys(self, keys: Iterable[str]) -> None:
        """Delete files by key, logging failures instead of raising them.

        Args:
            keys: Object keys.
        """
        try:
            await self.delete_keys(keys)
        except (StorageError, BotoCoreError, ClientError) as error:
            logger.warning("leftover files after cleanup: %s", error)

    async def discard_prefix(self, prefix: str) -> None:
        """Delete every file under a prefix, logging failures instead of raising them.

        Args:
            prefix: Key prefix; must not be empty.
        """
        try:
            await self.delete_prefix(prefix)
        except (StorageError, BotoCoreError, ClientError) as error:
            logger.warning("leftover files under %s after cleanup: %s", prefix, error)

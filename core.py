import asyncio, hashlib, base64, hmac
from fastapi import FastAPI, Request
from fastapi import status, HTTPException
from fastapi.responses import JSONResponse
from helpers.config_loader import ConfigType
from helpers.models import SessionKeyData, ExternalLoginKeyData
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from database import DBConnWrapper
import aioboto3
import asyncpg
from typing import Union

from authlib.integrations.starlette_client import OAuth


class ChartFastAPI(FastAPI):
    def __init__(self, config: ConfigType, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config: ConfigType = config
        self.debug: bool = config["server"].get("debug", False)

        self.executor: ThreadPoolExecutor | None = None
        self.s3_session: aioboto3.Session | None = None
        self.s3_session_getter: callable | None = None
        self.s3_bucket: str | None = None
        self.s3_asset_base_url: str | None = None
        self.auth: str | None = None
        self.auth_header: str | None = None
        self.token_secret_key: str | None = None
        self.db: asyncpg.Pool | None = None

        self.oauth: OAuth | None = None

        self.exception_handlers.setdefault(HTTPException, self.http_exception_handler)

    async def init(self) -> None:
        """Initialize all resources after worker process starts."""
        self.executor = ThreadPoolExecutor(max_workers=16)

        self.s3_session = aioboto3.Session(
            aws_access_key_id=self.config["s3"]["access-key-id"],
            aws_secret_access_key=self.config["s3"]["secret-access-key"],
            region_name=self.config["s3"]["location"],
        )
        self.s3_session_getter = lambda: self.s3_session.resource(
            service_name="s3",
            endpoint_url=self.config["s3"]["endpoint"],
        )
        self.s3_bucket = self.config["s3"]["bucket-name"]
        self.s3_asset_base_url = self.config["s3"]["base-url"]

        self.auth = self.config["server"]["auth"]
        self.auth_header = self.config["server"]["auth-header"]
        self.token_secret_key = self.config["server"]["token-secret-key"]

        psql_config = self.config["psql"]
        self.db = await asyncpg.create_pool(
            host=psql_config["host"],
            user=psql_config["user"],
            database=psql_config["database"],
            password=psql_config["password"],
            port=psql_config["port"],
            min_size=psql_config["pool-min-size"],
            max_size=psql_config["pool-max-size"],
            ssl="disable",  # XXX: todo, lazy for now
        )

    @asynccontextmanager
    async def db_acquire(self):
        async with self.db.acquire() as conn:
            yield DBConnWrapper(conn)

    def decode_key(
        self, session_key: str
    ) -> Union[SessionKeyData, ExternalLoginKeyData]:
        try:
            encoded_data, signature = session_key.rsplit(".", 1)
            recalculated_signature = hmac.new(
                self.token_secret_key.encode(), encoded_data.encode(), hashlib.sha256
            ).hexdigest()
            if recalculated_signature == signature:
                decoded_data = base64.urlsafe_b64decode(encoded_data).decode()
                try:
                    return SessionKeyData.model_validate_json(decoded_data)
                except:
                    return ExternalLoginKeyData.model_validate_json(decoded_data)
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session token."
        )

    async def run_blocking(self, func, *args, **kwargs):
        if not self.executor:
            raise RuntimeError("Executor not initialized. Call init() first.")
        return await asyncio.get_event_loop().run_in_executor(
            self.executor, lambda: func(*args, **kwargs)
        )

    async def http_exception_handler(self, request: Request, exc: HTTPException):
        if exc.status_code < 500 and exc.status_code != 422:
            return JSONResponse(
                content={"message": exc.detail}, status_code=exc.status_code
            )
        elif exc.status_code == 422 and not self.debug:
            return JSONResponse(
                content={"message": "Bad request. This is probably not your fault."},
                status_code=400,
            )
        else:
            if self.debug:
                raise exc
            return JSONResponse(content={}, status_code=exc.status_code)

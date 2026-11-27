import logging
import traceback
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("uc.backend")


class ApiError(Exception):
    """Error returned to the client as an error code.

    Attributes:
        status_code: HTTP status of the response.
        code: Machine-readable error code such as ``error.chart.not_found``.
        params: Values the client may need to render the error.
        headers: Extra response headers.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Create an API error.

        Args:
            status_code: HTTP status of the response.
            code: Machine-readable error code.
            params: Values the client may need to render the error.
            headers: Extra response headers.
        """
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.params = params or {}
        self.headers = headers or {}


def bad_request(code: str, **params: Any) -> ApiError:
    """Build a 400 error.

    Args:
        code: Error code.
        **params: Error parameters.

    Returns:
        ApiError: The error.
    """
    return ApiError(400, code, params)


def unauthorized(code: str = "error.auth.required", bearer_error: str | None = None) -> ApiError:
    """Build a 401 error with an RFC 6750 challenge.

    Args:
        code: Error code.
        bearer_error: RFC 6750 error value for the WWW-Authenticate header.

    Returns:
        ApiError: The error.
    """
    challenge = "Bearer" if bearer_error is None else f'Bearer error="{bearer_error}"'
    return ApiError(401, code, headers={"WWW-Authenticate": challenge})


def forbidden(code: str, **params: Any) -> ApiError:
    """Build a 403 error.

    Args:
        code: Error code.
        **params: Error parameters.

    Returns:
        ApiError: The error.
    """
    return ApiError(403, code, params)


def not_found(code: str, **params: Any) -> ApiError:
    """Build a 404 error.

    Args:
        code: Error code.
        **params: Error parameters.

    Returns:
        ApiError: The error.
    """
    return ApiError(404, code, params)


def conflict(code: str, **params: Any) -> ApiError:
    """Build a 409 error.

    Args:
        code: Error code.
        **params: Error parameters.

    Returns:
        ApiError: The error.
    """
    return ApiError(409, code, params)


def too_large(code: str, **params: Any) -> ApiError:
    """Build a 413 error.

    Args:
        code: Error code.
        **params: Error parameters.

    Returns:
        ApiError: The error.
    """
    return ApiError(413, code, params)


def error_body(code: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an error response body.

    Args:
        code: Error code.
        params: Error parameters, left out when empty.

    Returns:
        dict[str, Any]: The response body.
    """
    body: dict[str, Any] = {"error": code}
    if params:
        body["params"] = params
    return body


def install_error_handlers(app: FastAPI, debug: bool) -> None:
    """Register handlers that turn every failure into an error-code response.

    Args:
        app: Application to register the handlers on.
        debug: Whether to include exception details in 500 responses.
    """

    async def handle_api_error(request: Request, exc: Exception) -> JSONResponse:
        """Render an ApiError.

        Args:
            request: The failed request.
            exc: The raised error.

        Returns:
            JSONResponse: The error response.
        """
        if not isinstance(exc, ApiError):
            return await handle_unexpected(request, exc)
        return JSONResponse(
            error_body(exc.code, exc.params),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        """Render a request validation failure.

        Args:
            request: The failed request.
            exc: The validation error.

        Returns:
            JSONResponse: The error response.
        """
        if not isinstance(exc, RequestValidationError):
            return await handle_unexpected(request, exc)
        fields = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "type": error.get("type"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            error_body("error.request.invalid", {"fields": fields}), status_code=422
        )

    async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
        """Render routing errors raised by Starlette.

        Args:
            request: The failed request.
            exc: The HTTP error.

        Returns:
            JSONResponse: The error response.
        """
        if not isinstance(exc, StarletteHTTPException):
            return await handle_unexpected(request, exc)
        codes = {404: "error.route.not_found", 405: "error.route.method_not_allowed"}
        code = codes.get(exc.status_code, "error.request.failed")
        return JSONResponse(
            error_body(code),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Render an unexpected exception without leaking details.

        Args:
            request: The failed request.
            exc: The exception.

        Returns:
            JSONResponse: The error response.
        """
        logger.error(
            "unhandled error on %s %s\n%s",
            request.method,
            request.url.path,
            "".join(traceback.format_exception(exc)),
        )
        params = {"detail": repr(exc)} if debug else None
        return JSONResponse(error_body("error.internal", params), status_code=500)

    app.add_exception_handler(ApiError, handle_api_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected)

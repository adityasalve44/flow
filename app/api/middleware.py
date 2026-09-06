"""
app/api/middleware.py — FastAPI middleware for request correlation.

Assigns a UUID ``X-Request-ID`` to every request (or echoes the caller's),
threads it into the logging context for the duration of the request, and
echoes it in the response.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.logging import LogContext, get_logger

logger = get_logger(__name__)


class RequestCorrelationMiddleware(BaseHTTPMiddleware):
    """Assign and thread a request ID through every log line."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        with LogContext(request_id=request_id):
            logger.info(
                "request_received",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request_completed",
                extra={
                    "status_code": response.status_code,
                },
            )

        return response

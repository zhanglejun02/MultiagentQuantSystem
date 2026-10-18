"""Global exception handlers mapping domain errors to HTTP responses.

Only unambiguous mappings live here. QuantSystemError subclasses whose HTTP
status depends on route context are still handled locally in routers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from quant_system.api.backtest_files import WrongArtifactTypeError
from quant_system.domain.exceptions import ArtifactNotFoundError, InvalidTransitionError


def _handler(status_code: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status_code)

    return handle


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ArtifactNotFoundError, _handler(404))
    app.add_exception_handler(InvalidTransitionError, _handler(409))
    app.add_exception_handler(WrongArtifactTypeError, _handler(400))

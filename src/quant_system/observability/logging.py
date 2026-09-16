"""Structured logging with trace context. OpenTelemetry/Prometheus can be added
later behind these interfaces without touching domain code."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


def bind_context(**kwargs: Any) -> None:
    ctx = dict(_context.get() or {})
    ctx.update({k: v for k, v in kwargs.items() if v is not None})
    _context.set(ctx)


def clear_context() -> None:
    _context.set({})


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_context.get() or {})
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, StructuredFormatter)
               for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(StructuredFormatter())
        root.addHandler(handler)


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    logger.info(message, extra={"fields": fields})

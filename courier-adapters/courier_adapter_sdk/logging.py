"""Structured logging: one JSON object per line, on stderr."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(log_format: str = "json", level: int = logging.INFO) -> None:
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer() if log_format == "pretty" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.EventRenamer("message"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(**initial: object) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger().bind(**initial)
    return logger

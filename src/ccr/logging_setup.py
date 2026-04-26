"""Structlog configuration helper.

`configure_logging` is invoked once from :func:`ccr.cli.main` before any
subcommand dispatch, so every component sees the same renderer.
"""

from __future__ import annotations

import logging

import structlog

_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog and the stdlib logging module for console output.

    Idempotent: callers may invoke it multiple times (e.g. tests, CLI re-entry)
    without compounding handlers.
    """
    upper = level.upper()
    if upper not in _VALID_LEVELS:
        upper = "INFO"
    log_level = getattr(logging, upper, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


__all__ = ["configure_logging"]

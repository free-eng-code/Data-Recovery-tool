"""Logging setup for the application."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_file: str | None = None, debug: bool = False) -> None:
    """Configure application logging.

    Args:
        log_file: Optional path to a log file
        debug: If True, set DEBUG level; otherwise INFO
    """
    level = logging.DEBUG if debug else logging.INFO

    fmt = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
    ]

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=date_fmt,
        handlers=handlers,
    )

    # Quiet down noisy libraries
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

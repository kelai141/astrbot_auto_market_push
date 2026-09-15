"""Logging helpers shared by the CLI and the AstrBot plugin adapter."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

RESET = "\x1b[0m"
LEVEL_COLORS = {
    logging.DEBUG: "\x1b[38;5;244m",
    logging.INFO: "\x1b[38;5;39m",
    logging.WARNING: "\x1b[38;5;214m",
    logging.ERROR: "\x1b[38;5;196m",
    logging.CRITICAL: "\x1b[38;5;196;1m",
}


class ColorFormatter(logging.Formatter):
    """Compact console formatter that colors the level name."""

    def __init__(self, *, color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.color:
            return text
        color = LEVEL_COLORS.get(record.levelno)
        if not color:
            return text
        return text.replace(record.levelname, f"{color}{record.levelname}{RESET}", 1)


def setup_logging(
    level: str = "INFO", *, log_file: str | Path | None = None, color: bool | None = None
) -> None:
    """Configure the ``astrbot_auto_market_push`` logger tree."""
    root = logging.getLogger("astrbot_auto_market_push")
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.propagate = False

    if color is None:
        color = sys.stderr.isatty()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    console.setFormatter(ColorFormatter(color=color))
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
        root.addHandler(file_handler)

"""Configure application-only console and rotating file logs."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from app.exceptions import ConfigurationError


def configure_logging(level: str, log_file: Path) -> logging.Logger:
    """Keep third-party SSH debug output out of the application log."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError as exc:
        raise ConfigurationError("Unable to create the application log file.") from exc

    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    console_handler = RichHandler(
        console=Console(stderr=True), markup=False, rich_tracebacks=False
    )
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("app")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

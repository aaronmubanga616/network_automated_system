"""CLI entry point for the interactive menu, validation, and batch backups."""

import argparse
import logging
from pathlib import Path

from rich.console import Console

from app.backup import DEFAULT_BACKUP_DIR, backup_devices
from app.cli import InteractiveCLI
from app.config_manager import (
    DEFAULT_ENV_FILE,
    DEFAULT_INVENTORY,
    DEFAULT_LOG_FILE,
    load_settings,
)
from app.exceptions import NetworkAutomationError
from app.inventory import load_inventory
from app.logging_config import configure_logging
from app.report import DEFAULT_REPORT_DIR, display_backup_summary, display_inventory


def main(argv: list[str] | None = None) -> int:
    """Return 0 on success, 1 for any backup/input failure, or 130 on interruption."""
    parser = argparse.ArgumentParser(
        description="Open the interactive network automation menu, validate settings, or run batch backups."
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--backup", action="store_true", help="Back up running configurations for all inventory devices.")
    modes.add_argument("--validate", action="store_true", help="Validate local settings/inventory and exit without SSH.")
    modes.add_argument("--interactive", action="store_true", help="Open the interactive menu (default).")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help="Backup destination for the menu or --backup.")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help="Destination for generated JSON reports.")
    args = parser.parse_args(argv)
    logger: logging.Logger | None = None
    try:
        if not args.validate and not args.backup:
            logger = configure_logging("INFO", DEFAULT_LOG_FILE)
            devices = load_inventory(args.inventory)
            return InteractiveCLI(
                devices, env_file=args.env_file, backup_dir=args.backup_dir, report_dir=args.report_dir,
            ).run()
        settings = load_settings(args.env_file)
        logger = configure_logging(settings.log_level, DEFAULT_LOG_FILE)
        devices = load_inventory(args.inventory)
        logger.info("Settings and inventory validated: %d devices.", len(devices))
        if args.backup:
            summary = backup_devices(devices, settings, backup_dir=args.backup_dir)
            display_backup_summary(summary, Console())
            return 1 if summary.failed else 0
        display_inventory(devices, Console())
        Console().print("Validation complete. No SSH connections were opened.")
        return 0
    except NetworkAutomationError as exc:
        if logger is not None:
            logger.error("%s", exc)
        else:
            Console(stderr=True).print(f"Error: {exc}", style="red", markup=False)
        return 1
    except KeyboardInterrupt:
        Console(stderr=True).print("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

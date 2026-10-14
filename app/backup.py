"""Save Cisco IOS running configurations without overwriting previous backups."""

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app import connection
from app.config_manager import PROJECT_ROOT, Settings
from app.inventory import Device, load_inventory

logger = logging.getLogger(__name__)
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups"
BACKUP_COMMAND = "show running-config"
SAFE_DEVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


@dataclass(frozen=True)
class BackupIssue:
    """A safe failure code and message, without configuration or credentials."""

    code: str
    message: str


@dataclass(frozen=True)
class BackupResult:
    """Outcome for one device; path is the saved or attempted destination.

    On failure, path may identify an existing collision or an incomplete file
    whose cleanup failed. Always check success before treating it as a new backup.
    """

    device: Device
    path: Path | None = None
    error: BackupIssue | None = None

    @property
    def success(self) -> bool:
        """Whether the configuration was written and the file closed successfully."""
        return self.path is not None and self.error is None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-serializable metadata; configuration content is never stored here."""
        return {
            "device": asdict(self.device),
            "path": str(self.path) if self.path is not None else None,
            "success": self.success,
            "error": asdict(self.error) if self.error is not None else None,
        }


@dataclass
class BackupSummary:
    """All per-device outcomes, with successful and failed backup groups."""

    results: list[BackupResult] = field(default_factory=list)

    @property
    def successful(self) -> list[BackupResult]:
        """Return the newly saved backups."""
        return [result for result in self.results if result.success]

    @property
    def failed(self) -> list[BackupResult]:
        """Return failed attempts, including filename collisions."""
        return [result for result in self.results if not result.success]

    def to_dict(self) -> dict[str, object]:
        """Return summary counts and grouped, JSON-serializable outcome metadata."""
        successful, failed = self.successful, self.failed
        return {
            "total": len(self.results),
            "success_count": len(successful),
            "failure_count": len(failed),
            "successful": [result.to_dict() for result in successful],
            "failed": [result.to_dict() for result in failed],
        }


def _failure(device: Device, code: str, message: str, path: Path | None = None) -> BackupResult:
    """Record a safe per-device failure and allow the batch to continue."""
    logger.warning("Backup failed for %s [%s]: %s", device.name, code, message)
    return BackupResult(device=device, path=path, error=BackupIssue(code, message))


def _write_exclusive(path: Path, configuration: str) -> None:
    """Create a new UTF-8 file exclusively and remove our partial file on failure.

    Exclusive creation prevents check-then-write races between concurrent runs.
    Opening failures (including collisions) never trigger deletion. Cleanup is
    also attempted for interruptions; raw filesystem errors are not logged.
    """
    data = configuration.encode("utf-8")
    stream = path.open("xb")
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Only remove a file this call successfully created; never an old backup.
        try:
            path.unlink()
        except OSError:
            logger.error("Could not remove incomplete backup file: %s", path)
        raise


def backup_device(
    device: Device,
    settings: Settings | None = None,
    *,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    connect_timeout: float = 10.0,
    read_timeout: float = 60.0,
) -> BackupResult:
    """Fetch running-config through the connection layer and save a new backup.

    Uses inventory name and UTC time: DEVICE_YYYY-MM-DD_HH-MM-SS.cfg. Unsafe
    filename names are rejected instead of rewritten. A same-second collision
    is a failed attempt; old files are never overwritten or renamed. Enable
    mode and environment credential handling belong to the connection layer.
    Expected network and filesystem failures return a result; interrupts propagate.
    """
    if not SAFE_DEVICE_NAME.fullmatch(device.name):
        return _failure(
            device, "invalid_name",
            "Backup names require 1-100 ASCII letters, digits, dots, underscores, or hyphens, starting with a letter or digit.",
        )
    try:
        directory = backup_dir.resolve()
        directory.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError):
        return _failure(device, "directory", "Unable to create or access the backups directory.")

    logger.info("Starting running-configuration backup for %s.", device.name)
    commands = connection.execute_show_commands(
        device, [BACKUP_COMMAND], settings, enable=True,
        connect_timeout=connect_timeout, read_timeout=read_timeout,
    )
    if commands.error is not None:
        return _failure(device, commands.error.code, str(commands.error))
    configuration = commands.outputs.get(BACKUP_COMMAND, "")
    lines = [line.strip() for line in configuration.splitlines() if line.strip()]
    if len(lines) < 2 or lines[-1] != "end":
        return _failure(device, "invalid_output", "Running configuration is empty or missing the final IOS 'end' marker.")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    path = directory / f"{device.name}_{timestamp}.cfg"
    try:
        _write_exclusive(path, configuration)
    except FileExistsError:
        return _failure(device, "collision", "A backup with this device name and timestamp already exists; it was preserved.", path)
    except (OSError, UnicodeError):
        return _failure(device, "write", "Unable to write the backup file. Check storage permissions and available space.", path)
    logger.info("Backup saved for %s: %s", device.name, path)
    return BackupResult(device=device, path=path)


def backup_devices(
    devices: Iterable[Device] | None = None,
    settings: Settings | None = None,
    *,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    connect_timeout: float = 10.0,
    read_timeout: float = 60.0,
) -> BackupSummary:
    """Back up every inventory device and summarize successes and failures.

    Omit devices to load inventory/devices.yaml. Per-device failures do not stop
    the batch. Invalid inventory raises InventoryError before connecting. Pass
    settings from load_settings() to select a custom environment file.
    """
    inventory = load_inventory() if devices is None else devices
    summary = BackupSummary([
        backup_device(
            device, settings, backup_dir=backup_dir,
            connect_timeout=connect_timeout, read_timeout=read_timeout,
        )
        for device in inventory
    ])
    logger.info("Backup batch finished: %d successful, %d failed.", len(summary.successful), len(summary.failed))
    return summary

"""Load settings and prepare explicitly confirmed IOS configuration changes."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from app.exceptions import ConfigurationError, NetworkOperationError

if TYPE_CHECKING:
    from app.inventory import Device

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = PROJECT_ROOT / "inventory" / "devices.yaml"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "network_automation.log"
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True)
class Settings:
    """Credentials are excluded from repr to avoid accidental log disclosure."""

    username: str = field(repr=False)
    password: str = field(repr=False)
    secret: str | None = field(default=None, repr=False)
    log_level: str = "INFO"


def load_settings(env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    """Load optional .env defaults, giving existing environment variables priority."""
    try:
        # An absent .env is valid when credentials come from the environment.
        if env_file.exists():
            if not env_file.is_file():
                raise ConfigurationError("The environment file must be a regular file.")
            load_dotenv(dotenv_path=env_file, override=False, interpolate=False)
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("Unable to read the environment file.") from exc

    required = ("NETMIKO_USERNAME", "NETMIKO_PASSWORD")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigurationError(
            "Set the required environment variables: " + ", ".join(missing)
        )

    log_level = os.environ.get("APP_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigurationError(
            "APP_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL."
        )

    return Settings(
        username=os.environ["NETMIKO_USERNAME"],
        password=os.environ["NETMIKO_PASSWORD"],
        secret=os.environ.get("NETMIKO_SECRET") or None,
        log_level=log_level,
    )


@dataclass(frozen=True)
class ConfigurationPlan:
    """Immutable, preloaded snippet so approval cannot be changed by editing its file."""

    source: Path
    commands: tuple[str, ...] = field(repr=False)
    digest: str


@dataclass(frozen=True)
class ConfigurationResult:
    """Application outcome; failures after sending may have left partial changes."""

    device: Device
    success: bool
    may_have_changed: bool = False
    error: NetworkOperationError | None = None


def load_configuration_plan(path: Path) -> ConfigurationPlan:
    """Read a UTF-8 snippet with one non-interactive IOS config command per line.

    Blank lines and !/# comment lines are ignored. Full backups, multiline banners,
    and exec commands are outside this initial snippet workflow. Commands are
    never logged. The returned command tuple is applied as loaded, without rereading.
    """
    try:
        source = path.expanduser().resolve()
        content = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, RuntimeError):
        raise ConfigurationError("Unable to read the configuration snippet as UTF-8.") from None
    commands = tuple(line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith(("!", "#")))
    if not commands:
        raise ConfigurationError("The configuration file contains no commands.")
    for command in commands:
        verb = command.split()[0].casefold()
        if not command.isprintable() or verb in {
            "banner", "configure", "conf", "end", "do", "write", "copy", "reload", "erase",
        }:
            raise ConfigurationError("Use single-line configuration commands; omit banners, config-mode wrappers, and exec/save commands.")
    digest = hashlib.sha256("\n".join(commands).encode("utf-8")).hexdigest()
    return ConfigurationPlan(source, commands, digest)


def apply_configuration(
    device: Device,
    plan: ConfigurationPlan,
    settings: Settings | None = None,
    *,
    confirmed: bool = False,
) -> ConfigurationResult:
    """Apply a confirmed snippet using the existing connection layer.

    Without confirmed=True, no connection is opened. Only running configuration
    is changed; no automatic save, retry, or rollback is performed. A failed
    application after sending starts may have partially changed the device.
    """
    # Local import avoids the settings/inventory/connection dependency cycle.
    from app import connection

    if confirmed is not True:
        return ConfigurationResult(device, False, error=NetworkOperationError("configuration", "Configuration application was not confirmed."))
    may_have_changed = False
    try:
        with connection.open_connection(device, settings) as session:
            may_have_changed = True
            connection.send_config_commands(session, plan.commands, confirmed=True)
        logger.info("Configuration commands applied to %s; no automatic save requested.", device.name)
        return ConfigurationResult(device, True, True)
    except ConfigurationError:
        error = NetworkOperationError("configuration", "Environment settings are missing or invalid.")
    except NetworkOperationError as exc:
        error = exc
    logger.warning("Configuration application failed for %s [%s]; partial changes possible=%s.", device.name, error.code, may_have_changed)
    return ConfigurationResult(device, False, may_have_changed, error)

"""Collect structured Cisco IOS information using the shared connection layer."""

import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import cast

from app import connection
from app.config_manager import Settings
from app.exceptions import CollectionParseError, ConfigurationError, NetworkOperationError
from app.inventory import Device, load_inventory
from app.parsers import Record, normalize_interface_status, parse_ios_output

logger = logging.getLogger(__name__)
COLLECTION_COMMANDS = (
    ("version", "show version"),
    ("ip_interface_brief", "show ip interface brief"),
    ("ip_route", "show ip route"),
    ("interface_status", "show interfaces status"),
)


@dataclass(frozen=True)
class CollectionIssue:
    """Safe error metadata for a failed section or connection."""

    section: str
    command: str | None
    code: str
    message: str


@dataclass
class DeviceInformation:
    """One device snapshot with structured records and any collection issues.

    None means unavailable or not collected; [] means a successfully parsed empty
    table. Version fields and table records follow NTC's lowercase schema with
    string values (including numbers) and lists for multi-valued fields. Data is
    excluded from repr and logs. to_dict() returns a JSON-serializable snapshot.
    """

    device: Device
    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    hostname: str | None = field(default=None, repr=False)
    uptime: str | None = field(default=None, repr=False)
    version: Record | None = field(default=None, repr=False)
    ip_interface_brief: list[Record] | None = field(default=None, repr=False)
    ip_route: list[Record] | None = field(default=None, repr=False)
    interface_status: list[Record] | None = field(default=None, repr=False)
    interface_status_source: str | None = None
    errors: list[CollectionIssue] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Whether all required sections were collected and parsed."""
        return not self.errors and all(
            value is not None for value in (
                self.hostname, self.uptime, self.version, self.ip_interface_brief,
                self.ip_route, self.interface_status,
            )
        )

    def to_dict(self) -> dict[str, object]:
        """Return plain nested Python data suitable for reports or JSON storage."""
        return {**asdict(self), "success": self.success}


def _record_issue(
    result: DeviceInformation, section: str, command: str | None, code: str, message: str
) -> None:
    """Record a sanitized issue without logging device output."""
    result.errors.append(CollectionIssue(section, command, code, message))
    logger.warning("Collection failed for %s section %s [%s]: %s", result.device.name, section, code, message)


def collect_device(
    device: Device,
    settings: Settings | None = None,
    *,
    enable: bool = True,
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
) -> DeviceInformation:
    """Collect hostname, uptime, version, interfaces, routes, and interface status.

    One session is owned entirely by connection.open_connection(); all commands
    use connection.send_show_command(). Router images that reject the switch
    status command fall back to show interfaces. Parsing failures also allow this
    fallback. Command/parse errors preserve other sections; transport failures
    stop the current device to avoid reusing a broken or desynchronized session.
    Settings may be preloaded or obtained by the existing connection layer.
    Expected failures return issues, never raw exceptions. Interrupts propagate.
    """
    result = DeviceInformation(device)
    logger.info("Collecting device information for %s.", device.name)
    try:
        with connection.open_connection(
            device, settings, enable=enable, connect_timeout=connect_timeout
        ) as session:
            for section, preferred_command in COLLECTION_COMMANDS:
                command = preferred_command
                try:
                    try:
                        output = connection.send_show_command(session, command, read_timeout=read_timeout)
                        records = parse_ios_output(command, output)
                    except (NetworkOperationError, CollectionParseError) as exc:
                        if section != "interface_status" or (
                            isinstance(exc, NetworkOperationError) and exc.code != "command"
                        ):
                            raise
                        logger.info("Using detailed interface status for %s.", device.name)
                        command = "show interfaces"
                        output = connection.send_show_command(session, command, read_timeout=read_timeout)
                        records = parse_ios_output(command, output)

                    if section == "version":
                        result.version = records[0]
                        result.hostname = cast(str, records[0]["hostname"])
                        result.uptime = cast(str, records[0]["uptime"])
                    elif section == "ip_interface_brief":
                        result.ip_interface_brief = records
                    elif section == "ip_route":
                        result.ip_route = records
                    else:
                        result.interface_status = normalize_interface_status(command, records)
                        result.interface_status_source = command
                    logger.debug("Collected %s for %s: %d records.", section, device.name, len(records))
                except CollectionParseError as exc:
                    _record_issue(result, section, command, "parse", str(exc))
                except NetworkOperationError as exc:
                    _record_issue(result, section, command, exc.code, str(exc))
                    if exc.code != "command":
                        break
    except ConfigurationError:
        _record_issue(result, "connection", None, "configuration", "Environment settings are missing or invalid.")
    except NetworkOperationError as exc:
        _record_issue(result, "connection", None, exc.code, str(exc))
    logger.info("Collection finished for %s: success=%s, issues=%d.", device.name, result.success, len(result.errors))
    return result


def collect_devices(
    devices: Iterable[Device] | None = None,
    settings: Settings | None = None,
    *,
    enable: bool = True,
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
) -> list[DeviceInformation]:
    """Collect every inventory device sequentially, retaining individual failures.

    If devices is omitted, load inventory/devices.yaml using load_inventory().
    A malformed inventory raises InventoryError before any connection is opened.
    Pass load_inventory(custom_path) and load_settings(custom_env) for overrides.
    """
    inventory = load_inventory() if devices is None else devices
    return [
        collect_device(
            device, settings, enable=enable,
            connect_timeout=connect_timeout, read_timeout=read_timeout,
        )
        for device in inventory
    ]

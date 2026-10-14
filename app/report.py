"""Rich views and JSON reports over the application's structured results."""

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from app.backup import BackupSummary
from app.collector import DeviceInformation
from app.config_manager import ConfigurationResult, PROJECT_ROOT
from app.connection import ConnectivityResult
from app.exceptions import ReportError
from app.health import HealthReport
from app.inventory import Device

logger = logging.getLogger(__name__)
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"


def display_inventory(devices: Sequence[Device], console: Console) -> None:
    """Show device metadata without exposing credentials."""
    table = Table(title="Device inventory")
    for column in ("Name", "Host", "Platform", "SSH port"):
        table.add_column(column)
    for device in devices:
        table.add_row(
            Text(device.name), Text(device.host), Text(device.device_type), str(device.port)
        )
    console.print(table)


def display_backup_summary(summary: BackupSummary, console: Console) -> None:
    """Show saved paths or safe errors, never device configuration content."""
    table = Table(title="Configuration backups")
    for column in ("Device", "Result", "File or error"):
        table.add_column(column)
    for result in summary.results:
        detail = str(result.path) if result.success else (
            f"{result.error.code}: {result.error.message}" if result.error else "Backup did not complete."
        )
        table.add_row(Text(result.device.name), "Saved" if result.success else "Failed", Text(detail))
    console.print(table)
    console.print(f"Backups: {len(summary.successful)} successful, {len(summary.failed)} failed.")


def display_connectivity(results: Sequence[ConnectivityResult], console: Console) -> None:
    """Show SSH login results, without claiming an ICMP or full network test."""
    table = Table(title="SSH connectivity")
    for column in ("Device", "Host", "Result"):
        table.add_column(column)
    for result in results:
        detail = "Connected" if result.success else f"{result.error.code}: {result.error}"
        table.add_row(Text(result.device.name), Text(result.device.host), Text(detail, style="green" if result.success else "red"))
    console.print(table)


def display_device_tables(
    snapshots: Sequence[DeviceInformation], section: str, console: Console
) -> None:
    """Render interface or route records already parsed by the collector."""
    columns = {
        "interfaces": (("Interface", "interface"), ("IP address", "ip_address"), ("Status", "status"), ("Protocol", "proto")),
        "routes": (("Protocol", "protocol"), ("Network", "network"), ("Prefix", "prefix_length"), ("Next hop", "nexthop_ip"), ("Interface", "nexthop_if")),
    }[section]
    attribute = "ip_interface_brief" if section == "interfaces" else "ip_route"
    for snapshot in snapshots:
        console.print(Text(f"{snapshot.device.name} ({snapshot.device.host})", style="bold cyan"))
        rows = getattr(snapshot, attribute)
        if rows is None:
            console.print("Data unavailable.", style="yellow")
        else:
            table = Table()
            for label, _ in columns:
                table.add_column(label)
            for row in rows:
                table.add_row(*(Text(str(row.get(key) or "-")) for _, key in columns))
            console.print(table)
            if not rows:
                console.print("No entries reported.", style="dim")
        if section == "interfaces" and snapshot.interface_status is not None:
            statuses = Table(title="Interface status")
            for label in ("Interface", "Status", "Line protocol", "VLAN", "Speed"):
                statuses.add_column(label)
            for row in snapshot.interface_status:
                statuses.add_row(*(Text(str(row.get(key) or "-")) for key in ("interface", "status", "protocol_status", "vlan", "speed")))
            console.print(statuses)
        for issue in snapshot.errors:
            if issue.section in {attribute, "connection", "interface_status" if section == "interfaces" else attribute}:
                console.print(Text(f"{issue.code}: {issue.message}", style="yellow"))


def display_health(reports: Sequence[HealthReport], console: Console) -> None:
    """Show overall states and findings from the health module."""
    table = Table(title="Network health")
    for column in ("Device", "State", "Findings"):
        table.add_column(column)
    colors = {"HEALTHY": "green", "WARNING": "yellow", "CRITICAL": "red"}
    for report in reports:
        findings = "\n".join(
            f"{item.entity + ': ' if item.entity else ''}{item.message}" for item in report.findings
        ) or "No detected issues."
        table.add_row(Text(report.device.name), Text(report.state.value, style=colors[report.state.value]), Text(findings))
    console.print(table)


def display_configuration_results(results: Sequence[ConfigurationResult], console: Console) -> None:
    """Show outcomes without rendering configuration commands or echoes."""
    table = Table(title="Configuration results")
    for column in ("Device", "Result", "Details"):
        table.add_column(column)
    for result in results:
        detail = "Commands accepted. No automatic save requested." if result.success else str(result.error)
        if not result.success and result.may_have_changed:
            detail += " Partial changes may remain; inspect the device before retrying."
        table.add_row(Text(result.device.name), "Applied" if result.success else "Failed", Text(detail))
    console.print(table)


def generate_report(
    snapshots: Sequence[DeviceInformation],
    health_reports: Sequence[HealthReport],
    report_dir: Path = DEFAULT_REPORT_DIR,
) -> Path:
    """Save a fresh JSON report; exclude credentials, config plans, and raw output."""
    generated_at = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "devices": [snapshot.to_dict() for snapshot in snapshots],
        "health": [report.to_dict() for report in health_reports],
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        directory = report_dir.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"network_report_{generated_at:%Y-%m-%d_%H-%M-%S_%f}.json"
        stream = path.open("x", encoding="utf-8")
    except (OSError, RuntimeError):
        raise ReportError("Unable to create a new report file. Check destination permissions or an existing filename.") from None
    try:
        with stream:
            stream.write(content)
    except BaseException as exc:
        try:
            path.unlink()
        except OSError:
            logger.error("Could not remove incomplete report: %s", path)
        if isinstance(exc, (OSError, UnicodeError)):
            raise ReportError("Unable to finish writing the report file.") from None
        raise
    logger.info("Network report saved: %s", path)
    return path

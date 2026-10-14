"""Pure IOS output parsing; no network connections or credential access."""

import re
from typing import cast

from ntc_templates.parse import parse_output

from app.exceptions import CollectionParseError

# NTC Templates preserves CLI values as strings and multi-valued fields as lists.
Record = dict[str, str | list[str]]
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "show version": ("hostname", "uptime", "version"),
    "show ip interface brief": ("interface", "ip_address", "status", "proto"),
    "show ip route": ("protocol", "network", "prefix_length"),
    "show interfaces status": ("port", "status"),
    "show interfaces": ("interface", "link_status", "protocol_status"),
}


def _is_empty_table(command: str, output: str) -> bool:
    """Recognize empty tables without mistaking arbitrary text for empty data."""
    if command == "show ip route":
        # Route legends can precede this line; actual routes follow it.
        gateway = re.search(r"^Gateway of last resort (?:is not set|is .+)$", output, re.MULTILINE)
        return gateway is not None and not output[gateway.end():].strip()
    headers = {
        "show ip interface brief": r"Interface\s+IP-Address\s+OK\?\s+Method\s+Status\s+Protocol",
        "show interfaces status": r"Port\s+Name\s+Status\s+Vlan\s+Duplex\s+Speed\s+Type",
    }
    header = headers.get(command)
    return header is not None and re.fullmatch(r"\s*" + header + r"\s*", output) is not None


def parse_ios_output(command: str, output: str) -> list[Record]:
    """Return NTC records for a supported IOS command or a safe parsing error.

    Never fall back to raw text. Empty lists are accepted only for recognizable
    empty tables. Version data must include hostname, uptime, and IOS version.
    Parser diagnostics may contain device output and are deliberately suppressed.
    Record keys are lowercase; numeric values remain strings as supplied by NTC.
    """
    if command not in REQUIRED_FIELDS:
        raise CollectionParseError("No collector parser is configured for this command.")
    if not output.strip():
        raise CollectionParseError("The device returned empty output.")
    try:
        records = parse_output(platform="cisco_ios", command=command, data=output)
    except Exception:
        # Third-party parser errors can include full input lines and file paths.
        raise CollectionParseError("IOS output could not be parsed by the installed templates.") from None
    if not isinstance(records, list):
        raise CollectionParseError("The parser did not return structured records.")
    if not records and not _is_empty_table(command, output):
        raise CollectionParseError("No records were recognized in the IOS output.")
    for record in records:
        if not isinstance(record, dict) or any(
            not isinstance(record.get(key), str) or not record[key].strip()
            for key in REQUIRED_FIELDS[command]
        ):
            raise CollectionParseError("Parsed IOS records are missing required fields.")
        if any(
            not isinstance(key, str)
            or not (
                isinstance(value, str)
                or isinstance(value, list) and all(isinstance(item, str) for item in value)
            )
            for key, value in record.items()
        ):
            raise CollectionParseError("Parsed IOS fields have unexpected types.")
    return cast(list[Record], records)


def normalize_interface_status(command: str, records: list[Record]) -> list[Record]:
    """Give switch status tables and router interface details the same field names.

    Empty strings indicate unavailable fields, such as VLAN on router interfaces
    or line-protocol state in a switch status table. Status values remain the
    device's reported strings (for example, 'connected' or 'administratively down').
    """
    switch_table = command == "show interfaces status"
    return [
        {
            "interface": row["port" if switch_table else "interface"],
            "status": row["status" if switch_table else "link_status"],
            "protocol_status": row.get("protocol_status", ""),
            "description": row.get("name" if switch_table else "description", ""),
            "vlan": row.get("vlan_id", ""),
            "duplex": row.get("duplex", ""),
            "speed": row.get("speed", ""),
            "type": row.get("type" if switch_table else "hardware_type", ""),
        }
        for row in records
    ]

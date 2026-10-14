"""Read and validate the credential-free YAML device inventory."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config_manager import DEFAULT_INVENTORY
from app.exceptions import InventoryError


@dataclass(frozen=True)
class Device:
    """Connection metadata, separate from authentication settings."""

    name: str
    host: str
    device_type: str = "cisco_ios"
    port: int = 22


def load_inventory(path: Path = DEFAULT_INVENTORY) -> list[Device]:
    """Return validated devices or raise InventoryError with a safe message."""
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise InventoryError("Inventory file not found. Check --inventory.") from exc
    except (OSError, UnicodeError) as exc:
        raise InventoryError("Unable to read the inventory file as UTF-8.") from exc
    except yaml.YAMLError as exc:
        # YAML exceptions may include source text; do not expose that text.
        raise InventoryError("Inventory contains invalid YAML.") from exc

    if not isinstance(data, dict) or set(data) != {"devices"}:
        raise InventoryError("Inventory must contain only a top-level 'devices' list.")
    rows = data["devices"]
    if not isinstance(rows, list) or not rows:
        raise InventoryError("Inventory 'devices' must be a non-empty list.")

    devices: list[Device] = []
    names: set[str] = set()
    endpoints: set[tuple[str, int]] = set()
    allowed_fields = {"name", "host", "device_type", "port"}
    for index, row in enumerate(rows, start=1):
        label = f"Device entry {index}"
        if not isinstance(row, dict):
            raise InventoryError(f"{label} must be a mapping.")
        if set(row) - allowed_fields:
            raise InventoryError(
                f"{label} allows only name, host, device_type, and port. "
                "Keep credentials in environment variables."
            )
        for key in ("name", "host"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise InventoryError(f"{label} requires a non-empty string '{key}'.")
        host = row["host"].strip()
        if any(char.isspace() for char in host) or "://" in host:
            raise InventoryError(f"{label} host must be an IP address or hostname, not a URL.")
        device_type = row.get("device_type", "cisco_ios")
        if device_type != "cisco_ios":
            raise InventoryError(f"{label} device_type must be 'cisco_ios' in this version.")
        port = row.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise InventoryError(f"{label} port must be an integer from 1 to 65535.")

        device = Device(row["name"].strip(), host, device_type, port)
        if device.name.casefold() in names:
            raise InventoryError(f"{label} has a duplicate device name.")
        endpoint = (device.host.casefold(), device.port)
        if endpoint in endpoints:
            raise InventoryError(f"{label} has a duplicate host and port.")
        names.add(device.name.casefold())
        endpoints.add(endpoint)
        devices.append(device)
    return devices

"""Extensible, offline health checks over structured collector snapshots."""

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from ipaddress import IPv4Network

from app.collector import DeviceInformation
from app.exceptions import ConfigurationError
from app.inventory import Device

logger = logging.getLogger(__name__)


class HealthState(str, Enum):
    """Overall and individual finding severity, ordered by SEVERITY below."""

    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


SEVERITY = {HealthState.HEALTHY: 0, HealthState.WARNING: 1, HealthState.CRITICAL: 2}


@dataclass(frozen=True)
class HealthPolicy:
    """Explicit IPv4 prefixes that must exist in the device's global route table.

    Prefixes are compared exactly, after canonicalization. A covering/default
    route does not satisfy an explicitly required more-specific route. No routes
    are assumed required by default. IPv6/VRF collection is not supported yet.
    """

    required_routes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate policy before evaluating device health."""
        if isinstance(self.required_routes, (str, bytes)):
            raise ConfigurationError("required_routes must be a sequence of IPv4 CIDR prefixes.")
        try:
            prefixes = []
            for prefix in self.required_routes:
                if not isinstance(prefix, str) or "/" not in prefix:
                    raise ValueError
                prefixes.append(str(IPv4Network(prefix, strict=False)))
        except (TypeError, ValueError):
            raise ConfigurationError("required_routes must contain valid IPv4 CIDR prefixes.") from None
        object.__setattr__(self, "required_routes", tuple(dict.fromkeys(prefixes)))


@dataclass(frozen=True)
class HealthFinding:
    """A stable finding code, severity, safe explanation, and affected entity."""

    code: str
    state: HealthState
    message: str
    entity: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class HealthReport:
    """Evaluation of one snapshot; the worst finding determines overall state."""

    device: Device
    observed_at: str
    findings: tuple[HealthFinding, ...] = ()
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def state(self) -> HealthState:
        """Return HEALTHY when no issues were found, otherwise the highest severity."""
        return max((finding.state for finding in self.findings), key=SEVERITY.__getitem__, default=HealthState.HEALTHY)

    def to_dict(self) -> dict[str, object]:
        """Return JSON-serializable device metadata, state, and findings."""
        return {
            "device": asdict(self.device),
            "observed_at": self.observed_at,
            "evaluated_at": self.evaluated_at,
            "state": self.state.value,
            "findings": [{**asdict(item), "state": item.state.value} for item in self.findings],
        }


HealthCheck = Callable[[DeviceInformation, HealthPolicy], Iterable[HealthFinding]]


def check_collection(snapshot: DeviceInformation, policy: HealthPolicy) -> list[HealthFinding]:
    """Use structured error codes, never diagnostic-text matching, for SSH health."""
    findings = []
    connection_codes = {
        "unreachable": ("DEVICE_UNREACHABLE", "Device management endpoint could not be reached."),
        "authentication": ("SSH_AUTHENTICATION_FAILED", "SSH authentication failed."),
        "ssh": ("SSH_FAILED", "The SSH session failed."),
        "timeout": ("SSH_TIMEOUT", "SSH connection or command timed out; reachability is not confirmed."),
        "enable": ("SSH_ENABLE_FAILED", "Required IOS enable privilege could not be obtained."),
    }
    for issue in snapshot.errors:
        if issue.code in connection_codes:
            code, message = connection_codes[issue.code]
            findings.append(HealthFinding(code, HealthState.CRITICAL, message, source=issue.section))
        else:
            findings.append(HealthFinding(
                "COLLECTION_FAILED", HealthState.WARNING,
                "A collection section failed; its health cannot be fully assessed.", source=issue.section,
            ))
    required = ("hostname", "uptime", "version", "ip_interface_brief", "ip_route", "interface_status")
    if any(getattr(snapshot, name) is None for name in required):
        findings.append(HealthFinding(
            "INCOMPLETE_DATA", HealthState.WARNING,
            "Some required collector fields are unavailable; missing data is not proof of good health.",
        ))
    return findings


# Canonicalize interface identifiers, not terminal-output strings. This avoids
# duplicate findings from Gi0/1 in switch status and GigabitEthernet0/1 in brief.
INTERFACE_PREFIXES = {
    "gi": "gigabitethernet", "gig": "gigabitethernet", "gigabitethernet": "gigabitethernet",
    "fa": "fastethernet", "fastethernet": "fastethernet",
    "te": "tengigabitethernet", "tengigabitethernet": "tengigabitethernet",
    "eth": "ethernet", "et": "ethernet", "ethernet": "ethernet",
    "po": "port-channel", "port-channel": "port-channel",
    "lo": "loopback", "loopback": "loopback", "vl": "vlan", "vlan": "vlan",
    "se": "serial", "serial": "serial", "tu": "tunnel", "tunnel": "tunnel",
}
ADMIN_DOWN = {"administratively down", "disabled"}
OPER_DOWN = {"down", "notconnect", "inactive", "err-disabled", "suspended", "deleted"}
OPER_UP = {"up", "connected", "monitoring"}
PROTOCOL_STATES = {
    "up": "up", "down": "down", "up (connected)": "up",
    "up (spoofing)": "up", "down (notconnect)": "down", "down (disabled)": "down",
}


def _interface_key(name: str) -> str:
    """Normalize known IOS short/long identifiers without conflating unknown ones."""
    match = re.fullmatch(r"([a-z-]+)(\d.*)", name.strip().casefold())
    if match is None:
        return name.strip().casefold()
    prefix, number = match.groups()
    return INTERFACE_PREFIXES.get(prefix, prefix) + number


def check_interfaces(snapshot: DeviceInformation, policy: HealthPolicy) -> list[HealthFinding]:
    """Inspect parsed physical/admin/protocol states, deduplicating interface aliases.

    Interface warnings do not assert that a port was intended to be enabled.
    Protocol-down remains a distinct finding even when physical/admin state is
    also down. Missing protocol on switch status rows is expected, not 'down'.
    """
    observations: dict[str, tuple[str, set[str], set[str]]] = {}
    findings = []
    for rows, protocol_field, source in (
        (snapshot.ip_interface_brief, "proto", "ip_interface_brief"),
        (snapshot.interface_status, "protocol_status", "interface_status"),
    ):
        for row in rows or []:
            name, status, protocol = row.get("interface"), row.get("status"), row.get(protocol_field, "")
            if not isinstance(name, str) or not name.strip() or not isinstance(status, str) or not isinstance(protocol, str):
                findings.append(HealthFinding("INVALID_INTERFACE_DATA", HealthState.WARNING, "An interface record is malformed.", source=source))
                continue
            key = _interface_key(name)
            if key not in observations:
                observations[key] = (name, set(), set())
            observations[key][1].add(status.strip().casefold())
            if protocol.strip():
                observations[key][2].add(protocol.strip().casefold())
            elif source == "ip_interface_brief":
                findings.append(HealthFinding("UNKNOWN_PROTOCOL_STATE", HealthState.WARNING, "Line protocol state is unavailable.", name, source))

    for name, statuses, protocols in observations.values():
        if statuses & ADMIN_DOWN:
            findings.append(HealthFinding("INTERFACE_ADMIN_DOWN", HealthState.WARNING, "Interface is administratively disabled.", name))
        elif statuses & OPER_DOWN:
            findings.append(HealthFinding("INTERFACE_OPER_DOWN", HealthState.WARNING, "Interface is operationally down.", name))
        if any(PROTOCOL_STATES.get(state) == "down" for state in protocols):
            findings.append(HealthFinding("LINE_PROTOCOL_DOWN", HealthState.WARNING, "Interface line protocol is down.", name))
        if statuses - (ADMIN_DOWN | OPER_DOWN | OPER_UP) or protocols - PROTOCOL_STATES.keys():
            findings.append(HealthFinding("UNKNOWN_INTERFACE_STATE", HealthState.WARNING, "An interface state is not recognized by the configured health checks.", name))
        if statuses & OPER_UP and statuses & (ADMIN_DOWN | OPER_DOWN):
            findings.append(HealthFinding("CONFLICTING_INTERFACE_DATA", HealthState.WARNING, "Collected sources disagree about the interface state.", name))
    return findings


def check_routes(snapshot: DeviceInformation, policy: HealthPolicy) -> list[HealthFinding]:
    """Check parsed IPv4 global routes against explicit exact-prefix expectations.

    Missing/malformed/failed route data cannot establish route absence. An empty
    parsed table warns even with no policy. Required routes absent from a valid
    table are critical. A route in a VRF cannot satisfy a global expectation.
    """
    if snapshot.ip_route is None or any(issue.section == "ip_route" for issue in snapshot.errors):
        return [HealthFinding("ROUTES_UNASSESSED", HealthState.WARNING, "Route data is unavailable or failed collection; required routes could not be checked.", source="ip_route")]
    routes: set[IPv4Network] = set()
    malformed = False
    for row in snapshot.ip_route:
        try:
            network, prefix = row["network"], row["prefix_length"]
            if not isinstance(network, str) or not isinstance(prefix, str) or not prefix.isdecimal():
                raise ValueError
            route = IPv4Network(f"{network}/{prefix}", strict=False)
            vrf = row.get("vrf", "")
            if not isinstance(vrf, str):
                raise ValueError
            if not vrf.strip():
                routes.add(route)
        except (KeyError, ValueError, TypeError):
            malformed = True
    if malformed:
        return [HealthFinding("INVALID_ROUTE_DATA", HealthState.WARNING, "Some route records are invalid; missing routes cannot be determined reliably.", source="ip_route")]
    findings = []
    if not routes:
        findings.append(HealthFinding("EMPTY_ROUTING_TABLE", HealthState.WARNING, "No global IPv4 routes were observed; confirm whether routing is expected on this device.", source="ip_route"))
    for prefix in policy.required_routes:
        if IPv4Network(prefix) not in routes:
            findings.append(HealthFinding("MISSING_REQUIRED_ROUTE", HealthState.CRITICAL, "An explicitly required IPv4 route is absent from the collected global table.", prefix, "ip_route"))
    return findings


DEFAULT_CHECKS: tuple[HealthCheck, ...] = (check_collection, check_interfaces, check_routes)


def evaluate_health(
    snapshot: DeviceInformation,
    policy: HealthPolicy | None = None,
    *,
    additional_checks: Iterable[HealthCheck] = (),
) -> HealthReport:
    """Evaluate a snapshot without SSH, new collection, or raw-output parsing.

    Add checks as callables (snapshot, policy) -> iterable[HealthFinding]. Built-in
    checks always run. A failed extension becomes a warning and other checks
    continue; raw exception messages are not exposed. Interrupts propagate.
    """
    selected_policy = policy if policy is not None else HealthPolicy()
    findings = []
    for check in (*DEFAULT_CHECKS, *additional_checks):
        try:
            produced = list(check(snapshot, selected_policy))
            if any(not isinstance(item, HealthFinding) or not isinstance(item.state, HealthState) for item in produced):
                raise TypeError
            if any(
                not isinstance(item.code, str) or not isinstance(item.message, str)
                or (item.entity is not None and not isinstance(item.entity, str))
                or (item.source is not None and not isinstance(item.source, str))
                for item in produced
            ):
                raise TypeError
            findings.extend(produced)
        except Exception:
            findings.append(HealthFinding("HEALTH_CHECK_FAILED", HealthState.WARNING, "A health check could not complete; its result is unavailable."))
            logger.warning("A health check failed for %s; diagnostic details withheld.", snapshot.device.name)
    # Identical observations from repeated collection issues/extensions need not
    # produce duplicate findings. Preserve check order for readable reports.
    report = HealthReport(snapshot.device, snapshot.collected_at, tuple(dict.fromkeys(findings)))
    logger.info("Health evaluated for %s: %s, %d findings.", snapshot.device.name, report.state.value, len(report.findings))
    return report


def evaluate_devices(
    snapshots: Iterable[DeviceInformation],
    policies: Mapping[str, HealthPolicy] | None = None,
    *,
    additional_checks: Iterable[HealthCheck] = (),
) -> list[HealthReport]:
    """Evaluate every collected device, applying policies keyed by inventory name."""
    per_device = policies if policies is not None else {}
    checks = tuple(additional_checks)
    return [evaluate_health(snapshot, per_device.get(snapshot.device.name), additional_checks=checks) for snapshot in snapshots]

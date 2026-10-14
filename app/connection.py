"""Reusable Cisco IOS SSH sessions and show commands with safe failure results."""

import logging
import math
import re
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from netmiko import BaseConnection, ConnectHandler
from netmiko.exceptions import ConfigInvalidException, NetmikoTimeoutException, ReadException
from paramiko import AuthenticationException, SSHException
from paramiko.ssh_exception import NoValidConnectionsError

from app.config_manager import Settings, load_settings
from app.exceptions import ConfigurationError, NetworkOperationError
from app.inventory import Device

logger = logging.getLogger(__name__)
IOS_COMMAND_ERROR = re.compile(
    r"^\s*%\s*(?:Invalid input|Ambiguous command|Incomplete command|"
    r"Authorization failed|Command authorization failed|Unknown command)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class CommandResult:
    """Per-device results, including completed commands before a later failure.

    Output can contain sensitive device configuration and is excluded from repr.
    The error contains only a safe message and a stable error code.
    """

    device: Device
    outputs: dict[str, str] = field(default_factory=dict, repr=False)
    error: NetworkOperationError | None = None

    @property
    def success(self) -> bool:
        """Whether all requested commands completed successfully."""
        return self.error is None


@dataclass(frozen=True)
class ConnectivityResult:
    """SSH login/session check, separate from enable privilege or ICMP reachability."""

    device: Device
    error: NetworkOperationError | None = None

    @property
    def success(self) -> bool:
        """Whether the SSH session opened successfully."""
        return self.error is None


def _check_timeout(value: float) -> None:
    """Disallow unbounded or invalid timeouts before opening a session."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NetworkOperationError("configuration", "Timeouts must be positive finite numbers.")
    if not math.isfinite(value) or value <= 0:
        raise NetworkOperationError("configuration", "Timeouts must be positive finite numbers.")


def _validate_command(command: str) -> None:
    """Require a single printable show command; this is not an IOS authorization policy."""
    if (
        not isinstance(command, str)
        or not command.isprintable()
        or not command.split()
        or command.split()[0].casefold() != "show"
    ):
        raise NetworkOperationError("command", "Provide a single-line IOS show command.")


def _transport_error(exc: Exception) -> NetworkOperationError:
    """Classify wrapped socket errors without copying potentially sensitive text.

    Netmiko wraps several OS errors as timeouts. Inspect exception objects and
    their causes/contexts to identify DNS and refused/unreachable connections.
    """
    errors: list[BaseException] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        errors.append(current)
        if isinstance(current, NoValidConnectionsError):
            pending.extend(current.errors.values())
        chained = current.__cause__ or current.__context__
        if chained is not None:
            pending.append(chained)

    if any(isinstance(error, AuthenticationException) for error in errors):
        return NetworkOperationError(
            "authentication", "SSH authentication failed. Check NETMIKO_USERNAME and NETMIKO_PASSWORD."
        )
    if any(isinstance(error, socket.gaierror) for error in errors):
        return NetworkOperationError("unreachable", "Hostname could not be resolved. Check the inventory host and DNS.")
    if any(isinstance(error, OSError) and not isinstance(error, TimeoutError) for error in errors):
        return NetworkOperationError(
            "unreachable", "SSH device is unreachable or refused the connection. Check host, port, routing, and SSH service."
        )
    if any(isinstance(error, (TimeoutError, NetmikoTimeoutException, ReadException)) for error in errors):
        return NetworkOperationError(
            "timeout", "SSH operation timed out. Check device reachability and response time, or increase the timeout."
        )
    if any(isinstance(error, (SSHException, EOFError)) for error in errors):
        return NetworkOperationError("ssh", "SSH session failed or closed unexpectedly. Check the device SSH service.")
    return NetworkOperationError("unexpected", "Unexpected network operation failure; raw diagnostics were withheld.")


@contextmanager
def open_connection(
    device: Device,
    settings: Settings | None = None,
    *,
    enable: bool = True,
    connect_timeout: float = 10.0,
) -> Iterator[BaseConnection]:
    """Open a Cisco IOS SSH session and always attempt to disconnect it.

    Accept a Device from load_inventory(). Settings default to load_settings(),
    which reads NETMIKO_USERNAME, NETMIKO_PASSWORD, and optional NETMIKO_SECRET.
    Pass preloaded settings to reuse configuration across devices. By default,
    enter enable mode (or keep existing privilege); use enable=False for commands
    available without it. Connect, authentication, and banner waits are bounded.

    Raises ConfigurationError for missing environment settings and
    NetworkOperationError for device failures. Exceptions from the caller's
    with-block propagate unchanged after cleanup. No credentials, commands,
    output, or raw transport diagnostics are logged.
    """
    _check_timeout(connect_timeout)
    if device.device_type != "cisco_ios":
        raise NetworkOperationError("configuration", "Only the cisco_ios SSH platform is supported.")
    credentials = settings if settings is not None else load_settings()
    connection: BaseConnection | None = None
    logger.info("Opening SSH session to %s (%s:%s).", device.name, device.host, device.port)
    try:
        try:
            # Retain the object before network I/O so even partial opens can be
            # cleaned up. This follows Netmiko 4.x's own connection factories.
            connection = ConnectHandler(
                device_type=device.device_type,
                host=device.host,
                port=device.port,
                username=credentials.username,
                password=credentials.password,
                secret=credentials.secret or "",
                conn_timeout=connect_timeout,
                auth_timeout=connect_timeout,
                banner_timeout=connect_timeout,
                use_keys=False,
                allow_agent=False,
                verbose=False,
                session_log=None,
                auto_connect=False,
            )
            connection._open()
        except Exception as exc:
            # The external transport boundary can also raise platform-specific
            # errors. Do not leak their strings or exception chains to callers.
            error = _transport_error(exc)
            logger.warning("SSH connection failed for %s [%s]: %s", device.name, error.code, error)
            raise error from None

        if enable:
            try:
                connection.enable()
            except Exception as exc:
                error = (
                    NetworkOperationError("enable", "Unable to enter enable mode. Check NETMIKO_SECRET and IOS privileges.")
                    if isinstance(exc, ValueError)
                    else _transport_error(exc)
                )
                logger.warning("Enable mode failed for %s [%s]: %s", device.name, error.code, error)
                raise error from None
        logger.info("SSH session ready for %s.", device.name)
        yield connection
    finally:
        if connection is not None:
            try:
                connection.disconnect()
            except Exception:
                # Cleanup must not hide the original result or a caller error.
                logger.warning("SSH cleanup failed for %s; verify the session on the device.", device.name)
            else:
                logger.info("SSH session closed for %s.", device.name)


def send_show_command(
    connection: BaseConnection, command: str, *, read_timeout: float = 30.0
) -> str:
    """Execute one show command on an open session, returning raw text.

    Raises NetworkOperationError for invalid input, common IOS CLI rejection,
    timeout, or a broken session. Neither command text nor output is logged.
    The caller owns the session; use open_connection() for automatic cleanup.
    """
    _validate_command(command)
    _check_timeout(read_timeout)
    try:
        output = connection.send_command(
            command, read_timeout=read_timeout,
            use_textfsm=False, use_ttp=False, use_genie=False,
        )
    except Exception as exc:
        error = _transport_error(exc)
        logger.warning("Show command failed [%s]: %s", error.code, error)
        raise error from None
    if not isinstance(output, str) or IOS_COMMAND_ERROR.search(output):
        raise NetworkOperationError("command", "The device rejected the show command or returned an unexpected response.")
    logger.debug("Show command completed.")
    return output


def execute_show_commands(
    device: Device,
    commands: Sequence[str],
    settings: Settings | None = None,
    *,
    enable: bool = True,
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
) -> CommandResult:
    """Run commands using one session; return failures instead of raising them.

    Commands run in order and stop at the first failure. Completed output is
    preserved in result.outputs, keyed by command (duplicates use the last
    result). Call this once per device to continue after individual failures.
    An empty command list, malformed commands, and settings failures return an
    error before SSH starts. KeyboardInterrupt/SystemExit deliberately propagate.
    """
    outputs: dict[str, str] = {}
    try:
        if isinstance(commands, (str, bytes)) or not commands:
            raise NetworkOperationError("command", "Provide a non-empty sequence of show commands.")
        for command in commands:
            _validate_command(command)
        _check_timeout(read_timeout)
        with open_connection(device, settings, enable=enable, connect_timeout=connect_timeout) as connection:
            for command in commands:
                outputs[command] = send_show_command(connection, command, read_timeout=read_timeout)
        return CommandResult(device=device, outputs=outputs)
    except ConfigurationError:
        error = NetworkOperationError("configuration", "Invalid environment settings. Check NETMIKO_USERNAME, NETMIKO_PASSWORD, and APP_LOG_LEVEL.")
    except NetworkOperationError as exc:
        error = exc
    logger.warning("Device operation failed for %s [%s]: %s", device.name, error.code, error)
    return CommandResult(device=device, outputs=outputs, error=error)


def check_connectivity(device: Device, settings: Settings | None = None) -> ConnectivityResult:
    """Test SSH authentication/session setup and close the connection immediately."""
    try:
        with open_connection(device, settings, enable=False):
            pass
        return ConnectivityResult(device)
    except ConfigurationError:
        error = NetworkOperationError("configuration", "Environment settings are missing or invalid.")
    except NetworkOperationError as exc:
        error = exc
    return ConnectivityResult(device, error)


def send_config_commands(
    session: BaseConnection,
    commands: Sequence[str],
    *,
    confirmed: bool = False,
    read_timeout: float = 30.0,
) -> None:
    """Send explicitly approved configuration commands; never return/log command echoes.

    Raises sanitized NetworkOperationError on rejection or transport failure.
    Commands already accepted by IOS are not rolled back after a later failure.
    """
    if confirmed is not True:
        raise NetworkOperationError("configuration", "Configuration application was not confirmed.")
    if isinstance(commands, (str, bytes)) or not commands or any(
        not isinstance(command, str) or not command.strip() or not command.isprintable() for command in commands
    ):
        raise NetworkOperationError("configuration", "Provide non-empty, single-line configuration commands.")
    _check_timeout(read_timeout)
    try:
        session.send_config_set(
            commands, read_timeout=read_timeout, cmd_verify=True,
            error_pattern="(?im)" + IOS_COMMAND_ERROR.pattern,
        )
    except ConfigInvalidException:
        raise NetworkOperationError("command", "IOS rejected a configuration command; earlier commands may have been applied.") from None
    except Exception as exc:
        raise _transport_error(exc) from None

"""Offline connection tests: mock SSH transport, never contact lab devices."""

import errno
import os
import socket
import traceback
import unittest
import uuid
from unittest.mock import MagicMock, patch

from netmiko import BaseConnection
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException, ReadTimeout
from paramiko import SSHException
from paramiko.ssh_exception import NoValidConnectionsError

from app.config_manager import Settings
from app.connection import execute_show_commands, open_connection, send_show_command
from app.exceptions import NetworkOperationError
from app.inventory import Device


class ConnectionTests(unittest.TestCase):
    """Exercise success, transport failures, cleanup, and safe reporting."""

    def setUp(self) -> None:
        self.device = Device("R1", "192.0.2.1", port=2222)
        self.settings = Settings(
            username=uuid.uuid4().hex,
            password=uuid.uuid4().hex,
            secret=uuid.uuid4().hex,
        )
        self.session = MagicMock(spec=BaseConnection)
        self.session.send_command.return_value = "Cisco IOS output"
        factory_patch = patch("app.connection.ConnectHandler", return_value=self.session)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)

    def test_context_manager_connects_enables_and_closes(self) -> None:
        with open_connection(self.device, self.settings, connect_timeout=7) as session:
            self.assertIs(session, self.session)
            self.session.disconnect.assert_not_called()
        options = self.factory.call_args.kwargs
        for key, value in {
            "host": self.device.host, "port": 2222, "device_type": "cisco_ios",
            "username": self.settings.username, "password": self.settings.password,
            "secret": self.settings.secret, "auto_connect": False,
            "conn_timeout": 7, "auth_timeout": 7, "banner_timeout": 7,
            "verbose": False, "session_log": None,
        }.items():
            self.assertEqual(options[key], value)
        self.session._open.assert_called_once_with()
        self.session.enable.assert_called_once_with()
        self.session.disconnect.assert_called_once_with()

    def test_default_settings_are_read_from_environment(self) -> None:
        variables = {
            "NETMIKO_USERNAME": self.settings.username,
            "NETMIKO_PASSWORD": self.settings.password,
            "NETMIKO_SECRET": self.settings.secret,
        }
        with patch.dict(os.environ, variables, clear=True):
            with patch("app.config_manager.load_dotenv"):
                with open_connection(self.device):
                    pass
        self.assertEqual(self.factory.call_args.kwargs["password"], variables["NETMIKO_PASSWORD"])
        self.assertEqual(self.factory.call_args.kwargs["secret"], variables["NETMIKO_SECRET"])

    def test_enable_can_be_disabled(self) -> None:
        with open_connection(self.device, self.settings, enable=False):
            pass
        self.session.enable.assert_not_called()

    def test_missing_secret_allows_already_privileged_session(self) -> None:
        settings = Settings(self.settings.username, self.settings.password)
        with open_connection(self.device, settings):
            pass
        self.assertEqual(self.factory.call_args.kwargs["secret"], "")
        self.session.enable.assert_called_once()

    def test_unrelated_caller_error_propagates_after_cleanup(self) -> None:
        failure = ValueError("caller bug")
        with self.assertRaises(ValueError) as caught:
            with open_connection(self.device, self.settings):
                raise failure
        self.assertIs(caught.exception, failure)
        self.session.disconnect.assert_called_once()

    def test_transport_failures_are_safe_results_and_close_partial_sessions(self) -> None:
        failures = [
            (NetmikoAuthenticationException(self.settings.password), "authentication"),
            (NetmikoTimeoutException(self.settings.password), "timeout"),
            (ReadTimeout(self.settings.password), "timeout"),
            (TimeoutError(self.settings.password), "timeout"),
            (socket.gaierror(self.settings.password), "unreachable"),
            (ConnectionRefusedError(self.settings.password), "unreachable"),
            (OSError(errno.EHOSTUNREACH, self.settings.password), "unreachable"),
            (SSHException(self.settings.password), "ssh"),
            (EOFError(self.settings.password), "ssh"),
            (RuntimeError(self.settings.password), "unexpected"),
        ]
        for failure, expected in failures:
            with self.subTest(code=expected, exception=type(failure).__name__):
                self.session.reset_mock()
                self.session._open.side_effect = failure
                with self.assertLogs("app.connection", level="DEBUG") as logs:
                    result = execute_show_commands(self.device, ["show version"], self.settings)
                self.assertFalse(result.success)
                self.assertEqual(result.error.code, expected)
                self.assertNotIn(self.settings.password, repr(result) + "".join(logs.output))
                self.session.disconnect.assert_called_once()
                self.session.send_command.assert_not_called()

    def test_wrapped_socket_error_is_classified_without_message_matching(self) -> None:
        refused = NoValidConnectionsError({("192.0.2.1", 2222): ConnectionRefusedError()})
        wrapped = NetmikoTimeoutException("TCP failed")
        wrapped.__context__ = refused
        self.session._open.side_effect = wrapped
        result = execute_show_commands(self.device, ["show version"], self.settings)
        self.assertEqual(result.error.code, "unreachable")

    def test_factory_failure_returns_error(self) -> None:
        self.factory.side_effect = NetmikoAuthenticationException(self.settings.password)
        result = execute_show_commands(self.device, ["show version"], self.settings)
        self.assertEqual(result.error.code, "authentication")
        self.session.disconnect.assert_not_called()

    def test_enable_failure_closes_connection_and_hides_secret(self) -> None:
        self.session.enable.side_effect = ValueError(self.settings.secret)
        with self.assertLogs("app.connection") as logs:
            result = execute_show_commands(self.device, ["show version"], self.settings)
        self.assertEqual(result.error.code, "enable")
        self.assertNotIn(self.settings.secret, repr(result) + "".join(logs.output))
        self.session.disconnect.assert_called_once()
        self.session.send_command.assert_not_called()

    def test_transport_exception_chain_is_suppressed(self) -> None:
        self.session._open.side_effect = SSHException(self.settings.password)
        try:
            with open_connection(self.device, self.settings):
                self.fail("Session must not open")
        except NetworkOperationError:
            self.assertNotIn(self.settings.password, traceback.format_exc())

    def test_commands_share_one_connection_and_output_is_not_logged(self) -> None:
        self.session.send_command.return_value = self.settings.secret
        with self.assertLogs("app.connection", level="DEBUG") as logs:
            result = execute_show_commands(
                self.device, ["show version", "show ip interface brief"], self.settings, read_timeout=45
            )
        self.assertTrue(result.success)
        self.assertEqual(len(result.outputs), 2)
        self.assertNotIn(self.settings.secret, repr(result) + "".join(logs.output))
        self.factory.assert_called_once()
        self.assertEqual(self.session.send_command.call_args.kwargs["read_timeout"], 45)
        self.session.disconnect.assert_called_once()

    def test_command_failure_preserves_partial_results(self) -> None:
        self.session.send_command.side_effect = ["version data", ReadTimeout(self.settings.password)]
        result = execute_show_commands(
            self.device, ["show version", "show clock", "show inventory"], self.settings
        )
        self.assertEqual(result.outputs, {"show version": "version data"})
        self.assertEqual(result.error.code, "timeout")
        self.assertEqual(self.session.send_command.call_count, 2)
        self.session.disconnect.assert_called_once()

    def test_show_helper_rejects_ios_error(self) -> None:
        self.session.send_command.return_value = "% Invalid input detected at '^' marker."
        with self.assertRaises(NetworkOperationError) as caught:
            send_show_command(self.session, "show nonexistent")
        self.assertEqual(caught.exception.code, "command")

    def test_invalid_commands_do_not_open_a_connection(self) -> None:
        for commands in ([], "show version", ["reload"], ["show version\nreload"], ["show version", "configure terminal"]):
            with self.subTest(commands=commands):
                result = execute_show_commands(self.device, commands, self.settings)
                self.assertEqual(result.error.code, "command")
        self.factory.assert_not_called()

    def test_invalid_timeouts_and_platform_do_not_connect(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout):
                result = execute_show_commands(self.device, ["show version"], self.settings, connect_timeout=timeout)
                self.assertEqual(result.error.code, "configuration")
                result = execute_show_commands(self.device, ["show version"], self.settings, read_timeout=timeout)
                self.assertEqual(result.error.code, "configuration")
        result = execute_show_commands(Device("R2", "192.0.2.2", "cisco_ios_telnet"), ["show version"], self.settings)
        self.assertEqual(result.error.code, "configuration")
        self.factory.assert_not_called()

    def test_missing_credentials_return_configuration_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("app.config_manager.load_dotenv"):
            result = execute_show_commands(self.device, ["show version"])
        self.assertEqual(result.error.code, "configuration")
        self.factory.assert_not_called()

    def test_cleanup_failure_does_not_replace_command_failure(self) -> None:
        self.session.send_command.side_effect = ReadTimeout()
        self.session.disconnect.side_effect = RuntimeError(self.settings.secret)
        with self.assertLogs("app.connection") as logs:
            result = execute_show_commands(self.device, ["show version"], self.settings)
        self.assertEqual(result.error.code, "timeout")
        self.assertNotIn(self.settings.secret, "".join(logs.output))

    def test_next_device_runs_after_first_device_fails(self) -> None:
        self.session._open.side_effect = [NetmikoTimeoutException(), None]
        results = [
            execute_show_commands(device, ["show version"], self.settings)
            for device in (self.device, Device("R2", "192.0.2.2"))
        ]
        self.assertFalse(results[0].success)
        self.assertTrue(results[1].success)
        self.assertEqual(self.session.disconnect.call_count, 2)

    def test_interrupt_is_not_swallowed_and_session_is_closed(self) -> None:
        self.session.send_command.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            execute_show_commands(self.device, ["show version"], self.settings)
        self.session.disconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()

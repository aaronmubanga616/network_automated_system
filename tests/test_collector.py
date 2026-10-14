"""Test real IOS parsing against synthetic fixtures and mock only network I/O."""

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from netmiko import BaseConnection
from netmiko.exceptions import NetmikoAuthenticationException

from app.collector import collect_device, collect_devices
from app.config_manager import Settings
from app.exceptions import CollectionParseError, ConfigurationError, NetworkOperationError
from app.inventory import Device
from app.parsers import normalize_interface_status, parse_ios_output

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(command: str, platform: str = "ios") -> str:
    """Read synthetic output for a named command."""
    return (FIXTURES / f"{platform}_{command.replace(' ', '_')}.txt").read_text(encoding="utf-8")


class ParserTests(unittest.TestCase):
    """Exercise NTC templates rather than mocking structured parser results."""

    def test_classic_ios_version_hostname_and_uptime(self) -> None:
        row, = parse_ios_output("show version", fixture("show version"))
        self.assertEqual(row["hostname"], "lab-router")
        self.assertEqual(row["uptime"], "2 weeks, 3 days, 4 hours, 5 minutes")
        self.assertEqual(row["version"], "15.2(4)M11")
        self.assertEqual(row["hardware"], ["7206VXR"])

    def test_ios_xe_version(self) -> None:
        row, = parse_ios_output("show version", fixture("show version", "ios_xe"))
        self.assertEqual(row["hostname"], "lab-switch")
        self.assertEqual(row["version"], "17.3.5")
        self.assertEqual(row["uptime_years"], "1")

    def test_interface_brief_preserves_unassigned_and_admin_down(self) -> None:
        rows = parse_ios_output("show ip interface brief", fixture("show ip interface brief"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1]["ip_address"], "unassigned")
        self.assertEqual(rows[1]["status"], "administratively down")
        self.assertEqual(rows[1]["proto"], "down")

    def test_routes_inherit_masks_and_preserve_multiple_next_hops(self) -> None:
        rows = parse_ios_output("show ip route", fixture("show ip route"))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["prefix_length"], "24")
        self.assertEqual(rows[1]["distance"], "110")
        paths = [row for row in rows if row["network"] == "198.51.100.0"]
        self.assertEqual([row["nexthop_ip"] for row in paths], ["192.0.2.2", "192.0.2.3"])
        self.assertEqual(rows[4]["network"], "0.0.0.0")
        self.assertEqual(rows[4]["prefix_length"], "0")
        self.assertEqual(rows[5]["protocol"], "L")

    def test_switch_interface_status(self) -> None:
        command = "show interfaces status"
        rows = normalize_interface_status(command, parse_ios_output(command, fixture(command)))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["interface"], "Gi0/1")
        self.assertEqual(rows[0]["description"], "user-facing port")
        self.assertEqual(rows[2]["vlan"], "trunk")
        self.assertEqual(rows[3]["status"], "err-disabled")
        self.assertEqual(rows[0]["protocol_status"], "")

    def test_router_interface_status(self) -> None:
        command = "show interfaces"
        rows = normalize_interface_status(command, parse_ios_output(command, fixture(command)))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["status"], "up")
        self.assertEqual(rows[0]["description"], "uplink to core")
        self.assertEqual(rows[1]["status"], "administratively down")
        self.assertEqual(rows[1]["protocol_status"], "down")

    def test_recognized_empty_tables(self) -> None:
        self.assertEqual(parse_ios_output("show ip route", "Gateway of last resort is not set\n"), [])
        for command in ("show ip interface brief", "show interfaces status"):
            self.assertEqual(parse_ios_output(command, fixture(command).splitlines()[0]), [])

    def test_empty_and_unrecognized_output_are_failures(self) -> None:
        for command in ("show version", "show ip interface brief", "show ip route", "show interfaces"):
            for output in ("", "unexpected output"):
                with self.subTest(command=command, output=output), self.assertRaises(CollectionParseError):
                    parse_ios_output(command, output)
        with self.assertRaises(CollectionParseError):
            parse_ios_output("show ip route", "Gateway of last resort is not set\nmalformed route")

    def test_incomplete_version_is_not_successful(self) -> None:
        with self.assertRaises(CollectionParseError):
            parse_ios_output("show version", "device uptime is 1 day")

    def test_parser_exception_does_not_expose_output(self) -> None:
        sensitive = uuid.uuid4().hex
        with patch("app.parsers.parse_output", side_effect=ValueError(sensitive)):
            with self.assertRaises(CollectionParseError) as caught:
                parse_ios_output("show version", sensitive)
        self.assertNotIn(sensitive, str(caught.exception))


class CollectorTests(unittest.TestCase):
    """Exercise collection through the existing connection layer with mocked SSH."""

    def setUp(self) -> None:
        self.device = Device("inventory-name", "192.0.2.1")
        self.settings = Settings(uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex)
        self.session = MagicMock(spec=BaseConnection)
        self.session.send_command.side_effect = lambda command, **kwargs: fixture(command)
        factory_patch = patch("app.connection.ConnectHandler", return_value=self.session)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)

    def test_collects_all_fields_in_one_session(self) -> None:
        with self.assertLogs("app.collector", level="DEBUG") as logs:
            result = collect_device(self.device, self.settings)
        self.assertTrue(result.success)
        self.assertEqual(result.hostname, "lab-router")
        self.assertNotEqual(result.hostname, self.device.name)
        self.assertEqual(result.uptime, "2 weeks, 3 days, 4 hours, 5 minutes")
        self.assertEqual(len(result.ip_interface_brief), 4)
        self.assertEqual(len(result.ip_route), 6)
        self.assertEqual(len(result.interface_status), 4)
        self.assertEqual(result.interface_status_source, "show interfaces status")
        self.assertTrue(json.loads(json.dumps(result.to_dict()))["success"])
        self.factory.assert_called_once()
        self.session.disconnect.assert_called_once()
        self.assertEqual(self.session.send_command.call_count, 4)
        text = repr(result) + "".join(logs.output)
        for sensitive in ("LAB12345678", self.settings.password, self.settings.secret):
            self.assertNotIn(sensitive, text)

    def test_router_falls_back_on_unsupported_status_command(self) -> None:
        self.session.send_command.side_effect = lambda command, **kwargs: (
            "% Invalid input detected at '^' marker."
            if command == "show interfaces status" else fixture(command)
        )
        result = collect_device(self.device, self.settings)
        self.assertTrue(result.success)
        self.assertEqual(result.interface_status_source, "show interfaces")
        self.assertEqual(len(result.interface_status), 3)
        self.assertEqual(self.session.send_command.call_count, 5)
        self.factory.assert_called_once()

    def test_unrecognized_status_output_also_uses_fallback(self) -> None:
        self.session.send_command.side_effect = lambda command, **kwargs: (
            "unrecognized status output" if command == "show interfaces status" else fixture(command)
        )
        result = collect_device(self.device, self.settings)
        self.assertTrue(result.success)
        self.assertEqual(result.interface_status_source, "show interfaces")

    def test_command_rejection_preserves_other_sections(self) -> None:
        self.session.send_command.side_effect = lambda command, **kwargs: (
            "% Authorization failed." if command == "show ip route" else fixture(command)
        )
        result = collect_device(self.device, self.settings)
        self.assertFalse(result.success)
        self.assertIsNone(result.ip_route)
        self.assertIsNotNone(result.interface_status)
        self.assertEqual(result.errors[0].section, "ip_route")
        self.assertEqual(result.errors[0].code, "command")

    def test_parse_failure_does_not_discard_other_sections(self) -> None:
        self.session.send_command.side_effect = lambda command, **kwargs: (
            "unrecognized version format" if command == "show version" else fixture(command)
        )
        result = collect_device(self.device, self.settings)
        self.assertFalse(result.success)
        self.assertIsNone(result.hostname)
        self.assertIsNone(result.uptime)
        self.assertIsNotNone(result.ip_route)
        self.assertEqual(result.errors[0].code, "parse")

    def test_timeout_stops_current_device_and_closes_session(self) -> None:
        with patch("app.connection.send_show_command", side_effect=[
            fixture("show version"), NetworkOperationError("timeout", "Timed out.")
        ]) as send:
            result = collect_device(self.device, self.settings)
        self.assertFalse(result.success)
        self.assertEqual(result.hostname, "lab-router")
        self.assertEqual(result.errors[0].section, "ip_interface_brief")
        self.assertIsNone(result.ip_route)
        self.assertIsNone(result.interface_status)
        self.assertEqual(send.call_count, 2)
        self.session.disconnect.assert_called_once()

    def test_interface_timeout_does_not_attempt_fallback(self) -> None:
        with patch("app.connection.send_show_command", side_effect=[
            fixture("show version"), fixture("show ip interface brief"), fixture("show ip route"),
            NetworkOperationError("timeout", "Timed out."),
        ]) as send:
            result = collect_device(self.device, self.settings)
        self.assertEqual(result.errors[0].code, "timeout")
        self.assertEqual(send.call_count, 4)

    def test_failed_fallback_is_reported(self) -> None:
        self.session.send_command.side_effect = lambda command, **kwargs: (
            "% Invalid input detected at '^' marker." if command.startswith("show interfaces") else fixture(command)
        )
        result = collect_device(self.device, self.settings)
        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].command, "show interfaces")
        self.assertIsNone(result.interface_status)

    def test_batch_continues_after_authentication_failure(self) -> None:
        self.session._open.side_effect = [NetmikoAuthenticationException(self.settings.password), None]
        results = collect_devices([self.device, Device("R2", "192.0.2.2")], self.settings)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].errors[0].code, "authentication")
        self.assertTrue(results[1].success)
        self.assertEqual(self.session.disconnect.call_count, 2)

    def test_batch_loads_inventory_when_not_supplied(self) -> None:
        with patch("app.collector.load_inventory", return_value=[self.device]) as loader:
            result, = collect_devices(settings=self.settings)
        loader.assert_called_once_with()
        self.assertTrue(result.success)

    def test_configuration_failure_is_a_safe_result(self) -> None:
        with patch("app.connection.open_connection", side_effect=ConfigurationError(self.settings.password)):
            with self.assertLogs("app.collector") as logs:
                result = collect_device(self.device)
        self.assertEqual(result.errors[0].code, "configuration")
        self.assertNotIn(self.settings.password, str(result.to_dict()) + "".join(logs.output))

    def test_options_are_forwarded_to_connection_layer(self) -> None:
        result = collect_device(self.device, self.settings, enable=False, connect_timeout=12, read_timeout=40)
        self.assertTrue(result.success)
        self.assertEqual(self.factory.call_args.kwargs["conn_timeout"], 12)
        self.assertTrue(all(call.kwargs["read_timeout"] == 40 for call in self.session.send_command.call_args_list))
        self.session.enable.assert_not_called()

    def test_empty_routing_table_is_distinct_from_failure(self) -> None:
        self.session.send_command.side_effect = lambda command, **kwargs: (
            "Gateway of last resort is not set\n" if command == "show ip route" else fixture(command)
        )
        result = collect_device(self.device, self.settings)
        self.assertTrue(result.success)
        self.assertEqual(result.ip_route, [])

    def test_interrupt_propagates_and_disconnects(self) -> None:
        self.session.send_command.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            collect_device(self.device, self.settings)
        self.session.disconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()

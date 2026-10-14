"""Offline health policy tests against structured collector data."""

import copy
import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.collector import CollectionIssue, DeviceInformation
from app.exceptions import ConfigurationError
from app.health import HealthFinding, HealthPolicy, HealthState, evaluate_devices, evaluate_health
from app.inventory import Device
from app.parsers import normalize_interface_status, parse_ios_output


def healthy_snapshot(name: str = "R1") -> DeviceInformation:
    """Build a complete snapshot with one working interface and connected route."""
    return DeviceInformation(
        device=Device(name, "192.0.2.1"), hostname=name, uptime="1 day",
        version={"version": "15.2"},
        ip_interface_brief=[{"interface": "GigabitEthernet0/1", "status": "up", "proto": "up", "ip_address": "192.0.2.1"}],
        ip_route=[{"network": "192.0.2.0", "prefix_length": "24", "protocol": "C", "vrf": ""}],
        interface_status=[{"interface": "Gi0/1", "status": "connected", "protocol_status": ""}],
        interface_status_source="show interfaces status",
    )


class HealthTests(unittest.TestCase):
    """Check evidence, severity, extensibility, and safe error handling."""

    def test_complete_working_snapshot_is_healthy_without_ssh(self) -> None:
        with patch("app.connection.ConnectHandler") as connect:
            report = evaluate_health(healthy_snapshot())
        self.assertEqual(report.state, HealthState.HEALTHY)
        self.assertEqual(report.findings, ())
        self.assertEqual(json.loads(json.dumps(report.to_dict()))["state"], "HEALTHY")
        connect.assert_not_called()

    def test_connection_error_codes_are_critical(self) -> None:
        for code, finding_code in (
            ("unreachable", "DEVICE_UNREACHABLE"), ("ssh", "SSH_FAILED"),
            ("authentication", "SSH_AUTHENTICATION_FAILED"), ("timeout", "SSH_TIMEOUT"),
            ("enable", "SSH_ENABLE_FAILED"),
        ):
            with self.subTest(code=code):
                snapshot = DeviceInformation(Device("R1", "192.0.2.1"))
                snapshot.errors.append(CollectionIssue("connection", None, code, "arbitrary diagnostics"))
                report = evaluate_health(snapshot)
                self.assertEqual(report.state, HealthState.CRITICAL)
                self.assertIn(finding_code, [item.code for item in report.findings])

    def test_error_messages_do_not_control_classification_or_leak(self) -> None:
        snapshot = healthy_snapshot()
        sensitive = uuid.uuid4().hex
        snapshot.errors.append(CollectionIssue("ip_route", "show ip route", "parse", "unreachable authentication password " + sensitive))
        with self.assertLogs("app.health") as logs:
            report = evaluate_health(snapshot)
        self.assertEqual(report.state, HealthState.WARNING)
        text = str(report.to_dict()) + "".join(logs.output)
        self.assertNotIn(sensitive, text)
        self.assertNotIn("DEVICE_UNREACHABLE", text)

    def test_administratively_down_interface_is_warning(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_interface_brief[0].update(status="administratively down", proto="down")
        snapshot.interface_status[0]["status"] = "disabled"
        report = evaluate_health(snapshot)
        self.assertEqual(report.state, HealthState.WARNING)
        codes = [item.code for item in report.findings]
        self.assertEqual(codes.count("INTERFACE_ADMIN_DOWN"), 1)
        self.assertIn("LINE_PROTOCOL_DOWN", codes)
        self.assertNotIn("INTERFACE_OPER_DOWN", codes)

    def test_operationally_down_states(self) -> None:
        for state in ("down", "notconnect", "inactive", "err-disabled", "suspended", "deleted"):
            with self.subTest(state=state):
                snapshot = healthy_snapshot()
                snapshot.ip_interface_brief = []
                snapshot.interface_status[0]["status"] = state
                report = evaluate_health(snapshot)
                self.assertEqual(report.state, HealthState.WARNING)
                self.assertIn("INTERFACE_OPER_DOWN", [item.code for item in report.findings])

    def test_line_protocol_down_detected_even_with_link_up(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_interface_brief[0]["proto"] = "down"
        report = evaluate_health(snapshot)
        self.assertEqual([item.code for item in report.findings], ["LINE_PROTOCOL_DOWN"])

    def test_interface_aliases_are_deduplicated(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_interface_brief[0].update(status="down", proto="down")
        snapshot.interface_status[0].update(status="down", protocol_status="down")
        report = evaluate_health(snapshot)
        self.assertEqual(len(report.findings), 2)
        self.assertTrue(all(item.entity == "GigabitEthernet0/1" for item in report.findings))

    def test_structured_states_are_case_insensitive_but_not_substring_matches(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_interface_brief[0].update(status=" UP ", proto="UP")
        self.assertEqual(evaluate_health(snapshot).state, HealthState.HEALTHY)
        snapshot.ip_interface_brief[0]["status"] = "backup"
        report = evaluate_health(snapshot)
        self.assertEqual(report.state, HealthState.WARNING)
        self.assertIn("UNKNOWN_INTERFACE_STATE", [item.code for item in report.findings])

    def test_conflicting_sources_warn(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.interface_status[0]["status"] = "notconnect"
        self.assertIn("CONFLICTING_INTERFACE_DATA", [item.code for item in evaluate_health(snapshot).findings])

    def test_missing_brief_protocol_is_not_assumed_up(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_interface_brief[0]["proto"] = ""
        self.assertIn("UNKNOWN_PROTOCOL_STATE", [item.code for item in evaluate_health(snapshot).findings])

    def test_missing_collection_data_is_not_healthy(self) -> None:
        report = evaluate_health(DeviceInformation(Device("R1", "192.0.2.1")))
        self.assertEqual(report.state, HealthState.WARNING)
        self.assertIn("INCOMPLETE_DATA", [item.code for item in report.findings])

    def test_empty_routing_table_warns_without_assuming_default_route(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_route = []
        report = evaluate_health(snapshot)
        self.assertEqual(report.state, HealthState.WARNING)
        self.assertEqual([item.code for item in report.findings], ["EMPTY_ROUTING_TABLE"])

    def test_missing_required_route_is_critical(self) -> None:
        report = evaluate_health(healthy_snapshot(), HealthPolicy(required_routes=("0.0.0.0/0",)))
        self.assertEqual(report.state, HealthState.CRITICAL)
        self.assertEqual(report.findings[0].code, "MISSING_REQUIRED_ROUTE")
        self.assertEqual(report.findings[0].entity, "0.0.0.0/0")

    def test_existing_required_prefix_is_canonicalized(self) -> None:
        report = evaluate_health(healthy_snapshot(), HealthPolicy(required_routes=("192.0.2.99/24", "192.0.2.0/24")))
        self.assertEqual(report.state, HealthState.HEALTHY)

    def test_ecmp_and_default_route_are_recognized(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_route += [
            {"network": "0.0.0.0", "prefix_length": "0", "nexthop_ip": "192.0.2.2"},
            {"network": "0.0.0.0", "prefix_length": "0", "nexthop_ip": "192.0.2.3"},
        ]
        self.assertEqual(evaluate_health(snapshot, HealthPolicy(("0.0.0.0/0",))).state, HealthState.HEALTHY)

    def test_covering_route_does_not_satisfy_exact_prefix_policy(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_route = [{"network": "0.0.0.0", "prefix_length": "0"}]
        self.assertEqual(evaluate_health(snapshot, HealthPolicy(("192.0.2.0/24",))).state, HealthState.CRITICAL)

    def test_unavailable_route_table_does_not_prove_route_absence(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_route = None
        report = evaluate_health(snapshot, HealthPolicy(("0.0.0.0/0",)))
        self.assertEqual(report.state, HealthState.WARNING)
        self.assertNotIn("MISSING_REQUIRED_ROUTE", [item.code for item in report.findings])

    def test_failed_or_malformed_route_data_does_not_prove_route_absence(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_route.append({"network": "bad", "prefix_length": "999"})
        report = evaluate_health(snapshot, HealthPolicy(("0.0.0.0/0",)))
        self.assertEqual(report.state, HealthState.WARNING)
        self.assertIn("INVALID_ROUTE_DATA", [item.code for item in report.findings])
        snapshot.errors.append(CollectionIssue("ip_route", "show ip route", "parse", "failure"))
        self.assertIn("ROUTES_UNASSESSED", [item.code for item in evaluate_health(snapshot).findings])

    def test_vrf_route_does_not_satisfy_global_policy(self) -> None:
        snapshot = healthy_snapshot()
        snapshot.ip_route[0]["vrf"] = "CUSTOMER"
        report = evaluate_health(snapshot, HealthPolicy(("192.0.2.0/24",)))
        self.assertEqual(report.state, HealthState.CRITICAL)

    def test_bad_policy_is_rejected(self) -> None:
        for routes in ("0.0.0.0/0", ("not a prefix",), ("::/0",), ("192.0.2.0",)):
            with self.subTest(routes=routes), self.assertRaises(ConfigurationError):
                HealthPolicy(routes)

    def test_additional_checks_run_and_highest_severity_wins(self) -> None:
        def custom_check(snapshot, policy):
            return [HealthFinding("CUSTOM", HealthState.CRITICAL, "Custom rule failed.")]
        snapshot = healthy_snapshot()
        snapshot.ip_route = []
        report = evaluate_health(snapshot, additional_checks=[custom_check])
        self.assertEqual(report.state, HealthState.CRITICAL)
        self.assertEqual({item.code for item in report.findings}, {"EMPTY_ROUTING_TABLE", "CUSTOM"})

    def test_broken_extension_is_sanitized_and_other_checks_continue(self) -> None:
        sensitive = uuid.uuid4().hex
        def broken(snapshot, policy):
            raise RuntimeError(sensitive)
        def later(snapshot, policy):
            return [HealthFinding("LATER", HealthState.CRITICAL, "Later check ran.")]
        with self.assertLogs("app.health") as logs:
            report = evaluate_health(healthy_snapshot(), additional_checks=[broken, later])
        self.assertEqual(report.state, HealthState.CRITICAL)
        self.assertIn("HEALTH_CHECK_FAILED", [item.code for item in report.findings])
        self.assertNotIn(sensitive, str(report.to_dict()) + "".join(logs.output))

    def test_per_device_policies_and_extension_generator(self) -> None:
        def custom(snapshot, policy):
            return [HealthFinding("CUSTOM", HealthState.WARNING, "Custom check.")]
        reports = evaluate_devices(
            [healthy_snapshot("R1"), healthy_snapshot("R2")],
            {"R1": HealthPolicy(("0.0.0.0/0",))}, additional_checks=(item for item in [custom]),
        )
        self.assertEqual([report.state for report in reports], [HealthState.CRITICAL, HealthState.WARNING])
        self.assertTrue(all(any(item.code == "CUSTOM" for item in report.findings) for report in reports))

    def test_evaluation_does_not_mutate_collector_data(self) -> None:
        snapshot = healthy_snapshot()
        before = copy.deepcopy(snapshot.to_dict())
        evaluate_health(snapshot, HealthPolicy(("0.0.0.0/0",)))
        self.assertEqual(snapshot.to_dict(), before)

    def test_real_parsed_ios_fixture_data(self) -> None:
        root = Path(__file__).parent / "fixtures"
        def records(command):
            text = (root / f"ios_{command.replace(' ', '_')}.txt").read_text(encoding="utf-8")
            return parse_ios_output(command, text)
        snapshot = healthy_snapshot()
        snapshot.ip_interface_brief = records("show ip interface brief")
        snapshot.ip_route = records("show ip route")
        snapshot.interface_status = normalize_interface_status("show interfaces", records("show interfaces"))
        report = evaluate_health(snapshot, HealthPolicy(("0.0.0.0/0", "198.51.100.0/24")))
        self.assertEqual(report.state, HealthState.WARNING)
        self.assertTrue({"INTERFACE_ADMIN_DOWN", "INTERFACE_OPER_DOWN", "LINE_PROTOCOL_DOWN"}.issubset({item.code for item in report.findings}))
        self.assertNotIn("MISSING_REQUIRED_ROUTE", [item.code for item in report.findings])

    def test_interrupt_is_not_swallowed(self) -> None:
        def interrupt(snapshot, policy):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            evaluate_health(healthy_snapshot(), additional_checks=[interrupt])

    def test_malformed_extension_findings_cannot_break_report(self) -> None:
        def malformed(snapshot, policy):
            return [HealthFinding("INVALID", HealthState.WARNING, "Bad entity type.", entity=[])]
        with self.assertLogs("app.health"):
            report = evaluate_health(healthy_snapshot(), additional_checks=[malformed])
        self.assertEqual(report.state, HealthState.WARNING)
        self.assertEqual(report.findings[0].code, "HEALTH_CHECK_FAILED")


if __name__ == "__main__":
    unittest.main()

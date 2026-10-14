"""Scripted Rich interactions and confirmation tests; never contact devices."""

import io
import json
import logging
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from app.backup import BackupSummary
from app.cli import InteractiveCLI
from app.collector import DeviceInformation
from app.config_manager import ConfigurationResult, Settings
from app.connection import ConnectivityResult
from app.exceptions import ConfigurationError, NetworkOperationError, ReportError
from app.health import evaluate_devices
from app.main import main
from app.report import generate_report


class CLITests(unittest.TestCase):
    """Verify menu routing, selection, safe output, and mutation confirmation."""

    def setUp(self) -> None:
        from app.inventory import Device
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.devices = [Device("R1", "192.0.2.1"), Device("SW1", "192.0.2.2")]
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=110, color_system=None)
        self.cli = InteractiveCLI(
            self.devices, console=self.console, backup_dir=self.root / "backups", report_dir=self.root / "reports"
        )
        self.settings = Settings(uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex)
        settings_patch = patch("app.cli.config_manager.load_settings", return_value=self.settings)
        self.load_settings = settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.snippet = self.root / "changes.txt"
        self.snippet.write_text(f"hostname LAB\nenable secret {self.settings.secret}\n", encoding="utf-8")
        self.snapshot = DeviceInformation(
            self.devices[0], hostname="R1", uptime="1 day", version={"version": "15.2"},
            ip_interface_brief=[], ip_route=[], interface_status=[],
        )

    def test_menu_labels_and_list_devices_need_no_credentials_or_network(self) -> None:
        with patch("app.cli.Prompt.ask", side_effect=["1", "0"]):
            with patch("app.connection.ConnectHandler") as connect:
                self.assertEqual(self.cli.run(), 0)
        self.load_settings.assert_not_called()
        connect.assert_not_called()
        for label in ("List devices", "Check connectivity", "Show interfaces", "Show routing tables", "Backup configurations", "Run health check", "Generate report", "Apply configuration", "Exit", "CHANGES NETWORK CONFIGURATION"):
            self.assertIn(label, self.output.getvalue())

    def test_real_prompt_rejects_invalid_menu_input(self) -> None:
        with patch("builtins.input", side_effect=["invalid", "0"]):
            self.assertEqual(self.cli.run(), 0)
        self.assertIn("Please select", self.output.getvalue())

    def test_selection_validates_and_deduplicates(self) -> None:
        with patch("app.cli.Prompt.ask", side_effect=["99", "1,0", "x", "2, 1, 2"]):
            self.assertEqual(self.cli._select_devices(), [self.devices[1], self.devices[0]])

    def test_configuration_target_default_is_cancel(self) -> None:
        with patch("app.cli.Prompt.ask", return_value="0") as ask:
            self.assertEqual(self.cli._select_devices(changing=True), [])
        self.assertEqual(ask.call_args.kwargs["default"], "0")

    def test_connectivity_uses_connection_module_and_only_selected_device(self) -> None:
        with patch("app.cli.Prompt.ask", return_value="2"):
            with patch("app.cli.connection.check_connectivity", return_value=ConnectivityResult(self.devices[1])) as check:
                self.cli._perform("2")
        check.assert_called_once_with(self.devices[1], self.settings)
        self.assertIn("Connected", self.output.getvalue())

    def test_interface_and_route_views_use_collector_and_report(self) -> None:
        for choice, section in (("3", "interfaces"), ("4", "routes")):
            with self.subTest(choice=choice):
                with patch("app.cli.Prompt.ask", return_value="1"):
                    with patch("app.cli.collector.collect_devices", return_value=[self.snapshot]) as collect:
                        with patch("app.cli.report.display_device_tables") as display:
                            self.cli._perform(choice)
                collect.assert_called_once_with([self.devices[0]], self.settings)
                display.assert_called_once_with([self.snapshot], section, self.console)

    def test_backup_uses_existing_workflow_without_configuration_confirmation(self) -> None:
        with patch("app.cli.Prompt.ask", return_value="all"):
            with patch("app.cli.backup.backup_devices", return_value=BackupSummary()) as backup:
                with patch("app.cli.Confirm.ask") as confirm:
                    self.cli._perform("5")
        backup.assert_called_once_with(self.devices, self.settings, backup_dir=self.root / "backups")
        confirm.assert_not_called()

    def test_health_uses_evaluator_over_collected_data(self) -> None:
        with patch("app.cli.Prompt.ask", return_value="1"):
            with patch("app.cli.collector.collect_devices", return_value=[self.snapshot]):
                with patch("app.cli.health.evaluate_devices", return_value=[]) as evaluate:
                    self.cli._perform("6")
        evaluate.assert_called_once_with([self.snapshot])

    def test_generate_report_collects_and_saves_structured_json(self) -> None:
        with patch("app.cli.Prompt.ask", return_value="1"):
            with patch("app.cli.collector.collect_devices", return_value=[self.snapshot]):
                self.cli._perform("7")
        path, = (self.root / "reports").glob("*.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["devices"][0]["hostname"], "R1")
        self.assertEqual(payload["health"][0]["state"], "WARNING")
        self.assertNotIn(self.settings.password, path.read_text(encoding="utf-8"))

    def test_declining_configuration_does_not_open_connection_or_apply(self) -> None:
        with patch("app.cli.Prompt.ask", side_effect=["1", str(self.snippet)]):
            with patch("app.cli.Confirm.ask", return_value=False) as confirm:
                with patch("app.cli.config_manager.apply_configuration") as apply:
                    with patch("app.connection.ConnectHandler") as connect:
                        self.cli._perform("8")
        self.assertFalse(confirm.call_args.kwargs["default"])
        apply.assert_not_called()
        connect.assert_not_called()
        self.assertNotIn(self.settings.secret, self.output.getvalue())
        self.assertIn("Cancelled", self.output.getvalue())

    def test_confirmation_precedes_apply_and_file_is_not_reread(self) -> None:
        approved = False
        def confirm(*args, **kwargs):
            nonlocal approved
            self.assertIn(str(self.snippet), self.output.getvalue())
            self.assertIn("R1 (192.0.2.1)", self.output.getvalue())
            self.snippet.write_text("hostname CHANGED-AFTER-REVIEW", encoding="utf-8")
            approved = True
            return True
        def apply(device, plan, settings, *, confirmed):
            self.assertTrue(approved)
            self.assertTrue(confirmed)
            self.assertEqual(plan.commands[0], "hostname LAB")
            return ConfigurationResult(device, True, True)
        with patch("app.cli.Prompt.ask", side_effect=["1", str(self.snippet)]):
            with patch("app.cli.Confirm.ask", side_effect=confirm):
                with patch("app.cli.config_manager.apply_configuration", side_effect=apply) as action:
                    self.cli._perform("8")
        action.assert_called_once()
        self.assertNotIn(self.settings.secret, self.output.getvalue())

    def test_configuration_failure_stops_later_devices(self) -> None:
        error = NetworkOperationError("command", "Rejected.")
        with patch("app.cli.Prompt.ask", side_effect=["all", str(self.snippet)]):
            with patch("app.cli.Confirm.ask", return_value=True):
                with patch("app.cli.config_manager.apply_configuration", return_value=ConfigurationResult(self.devices[0], False, True, error)) as apply:
                    self.cli._perform("8")
        apply.assert_called_once()
        self.assertIn("Partial changes may remain", self.output.getvalue())
        self.assertIn("Remaining devices were not changed", self.output.getvalue())

    def test_eof_during_confirmation_never_applies(self) -> None:
        with patch("app.cli.Prompt.ask", side_effect=["8", "1", str(self.snippet)]):
            with patch("app.cli.Confirm.ask", side_effect=EOFError):
                with patch("app.cli.config_manager.apply_configuration") as apply:
                    self.assertEqual(self.cli.run(), 0)
        apply.assert_not_called()

    def test_real_confirmation_blank_defaults_to_no(self) -> None:
        with patch("builtins.input", side_effect=["8", "1", str(self.snippet), "", "0"]):
            with patch("app.cli.config_manager.apply_configuration") as apply:
                self.assertEqual(self.cli.run(), 0)
        apply.assert_not_called()
        self.assertIn("Cancelled. No configuration changes sent.", self.output.getvalue())

    def test_configuration_interrupt_warns_about_possible_partial_changes(self) -> None:
        with patch("app.cli.Prompt.ask", side_effect=["8", "1", str(self.snippet), "0"]):
            with patch("app.cli.Confirm.ask", return_value=True):
                with patch("app.cli.config_manager.apply_configuration", side_effect=KeyboardInterrupt):
                    self.assertEqual(self.cli.run(), 0)
        self.assertIn("Partial configuration changes may remain", self.output.getvalue())

    def test_action_errors_return_to_menu_and_hide_unexpected_diagnostics(self) -> None:
        with patch("app.cli.Prompt.ask", side_effect=["2", "2", "0"]):
            with patch.object(self.cli, "_perform", side_effect=[ConfigurationError("Missing settings."), RuntimeError(self.settings.password)]):
                self.assertEqual(self.cli.run(), 0)
        self.assertIn("Missing settings", self.output.getvalue())
        self.assertIn("could not complete", self.output.getvalue())
        self.assertNotIn(self.settings.password, self.output.getvalue())

    def test_default_main_opens_menu_without_requiring_credentials(self) -> None:
        with patch("app.main.load_inventory", return_value=self.devices):
            with patch("app.main.configure_logging", return_value=logging.getLogger("test")):
                with patch("app.main.load_settings") as settings:
                    with patch("app.main.InteractiveCLI") as cli:
                        cli.return_value.run.return_value = 0
                        self.assertEqual(main([]), 0)
        settings.assert_not_called()
        cli.return_value.run.assert_called_once()

    def test_report_collision_preserves_previous_file(self) -> None:
        with patch("app.report.datetime") as clock:
            from datetime import datetime, timezone
            clock.now.return_value = datetime(2026, 10, 6, tzinfo=timezone.utc)
            path = generate_report([self.snapshot], evaluate_devices([self.snapshot]), self.root)
            original = path.read_bytes()
            with self.assertRaises(ReportError):
                generate_report([], [], self.root)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

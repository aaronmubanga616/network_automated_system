"""Offline backup tests using mocked SSH and real temporary filesystem writes."""

import io
import json
import logging
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from netmiko import BaseConnection
from netmiko.exceptions import NetmikoAuthenticationException, ReadTimeout

from app.backup import BackupIssue, BackupResult, BackupSummary, backup_device, backup_devices
from app.config_manager import Settings
from app.connection import CommandResult
from app.inventory import Device
from app.main import main


class BackupTests(unittest.TestCase):
    """Check file safety, transport reuse, batch isolation, and summary behavior."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backups = self.root / "nested" / "backups"
        self.device = Device("R1", "192.0.2.1")
        self.settings = Settings(uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex)
        self.configuration = f"Building configuration...\n\nCurrent configuration : 80 bytes\n!\nhostname R1\nenable secret 9 {uuid.uuid4().hex}\n!\nend\n"
        self.session = MagicMock(spec=BaseConnection)
        self.session.send_command.return_value = self.configuration
        factory_patch = patch("app.connection.ConnectHandler", return_value=self.session)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        time_patch = patch("app.backup.datetime")
        self.clock = time_patch.start()
        self.addCleanup(time_patch.stop)
        self.clock.now.return_value = datetime(2026, 10, 5, 21, 30, 15, tzinfo=timezone.utc)

    def test_backup_creates_directory_and_saves_exact_output(self) -> None:
        result = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertTrue(result.success)
        self.assertEqual(result.path.name, "R1_2026-10-05_21-30-15.cfg")
        self.assertEqual(result.path.parent, self.backups.resolve())
        self.assertEqual(result.path.read_bytes(), self.configuration.encode("utf-8"))
        self.factory.assert_called_once()
        self.session.send_command.assert_called_once_with(
            "show running-config", read_timeout=60.0,
            use_textfsm=False, use_ttp=False, use_genie=False,
        )
        self.session.enable.assert_called_once()
        self.session.disconnect.assert_called_once()

    def test_timestamp_collision_never_overwrites_existing_file(self) -> None:
        first = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.session.send_command.return_value = "hostname CHANGED\nend"
        second = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertFalse(second.success)
        self.assertEqual(second.error.code, "collision")
        self.assertEqual(first.path.read_bytes(), self.configuration.encode("utf-8"))
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_later_timestamp_creates_a_second_backup(self) -> None:
        first = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.clock.now.return_value = datetime(2026, 10, 5, 21, 30, 16, tzinfo=timezone.utc)
        second = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertTrue(second.success)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(len(list(self.backups.glob("*.cfg"))), 2)

    def test_concurrent_attempts_cannot_overwrite(self) -> None:
        response = CommandResult(self.device, {"show running-config": self.configuration})
        with patch("app.backup.connection.execute_show_commands", return_value=response):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: backup_device(self.device, self.settings, backup_dir=self.backups),
                    range(2),
                ))
        self.assertEqual(sum(result.success for result in results), 1)
        failed, = [result for result in results if not result.success]
        self.assertEqual(failed.error.code, "collision")
        path, = self.backups.glob("*.cfg")
        self.assertEqual(path.read_bytes(), self.configuration.encode("utf-8"))

    def test_unsafe_names_are_rejected_before_connection(self) -> None:
        for name in ("../R1", "R1/other", "R1\\other", "R1:other", "", ".hidden", "R" * 101):
            with self.subTest(name=name):
                result = backup_device(Device(name, "192.0.2.1"), self.settings, backup_dir=self.backups)
                self.assertEqual(result.error.code, "invalid_name")
        self.factory.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_directory_failure_does_not_open_ssh(self) -> None:
        destination = self.root / "existing-file"
        destination.write_text("preserve me", encoding="utf-8")
        result = backup_device(self.device, self.settings, backup_dir=destination)
        self.assertEqual(result.error.code, "directory")
        self.assertEqual(destination.read_text(encoding="utf-8"), "preserve me")
        self.factory.assert_not_called()

    def test_empty_or_truncated_output_is_not_saved(self) -> None:
        for output in ("", "  \n", "hostname R1", "end", "% Error retrieving configuration"):
            with self.subTest(output=output):
                self.session.send_command.return_value = output
                result = backup_device(self.device, self.settings, backup_dir=self.backups)
                self.assertEqual(result.error.code, "invalid_output")
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_command_rejection_does_not_create_backup(self) -> None:
        self.session.send_command.return_value = "% Authorization failed."
        result = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertEqual(result.error.code, "command")
        self.assertEqual(list(self.backups.iterdir()), [])
        self.session.disconnect.assert_called_once()

    def test_timeout_is_a_failed_result(self) -> None:
        self.session.send_command.side_effect = ReadTimeout(self.settings.password)
        result = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertEqual(result.error.code, "timeout")
        self.assertIsNone(result.path)
        self.session.disconnect.assert_called_once()

    def test_write_failure_removes_only_its_own_partial_file(self) -> None:
        self.backups.mkdir(parents=True)
        existing = self.backups / "older.cfg"
        existing.write_text("keep existing", encoding="utf-8")
        with patch("app.backup.os.fsync", side_effect=OSError("disk full")):
            result = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertEqual(result.error.code, "write")
        self.assertFalse(result.path.exists())
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep existing")
        self.assertEqual(list(self.backups.iterdir()), [existing])

    def test_file_open_permission_failure_is_safe(self) -> None:
        with patch("app.backup.Path.open", side_effect=PermissionError(self.settings.password)):
            with self.assertLogs("app.backup") as logs:
                result = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertEqual(result.error.code, "write")
        self.assertNotIn(self.settings.password, "".join(logs.output) + repr(result))

    def test_cleanup_failure_is_reported_and_retains_failed_path(self) -> None:
        with patch("app.backup.os.fsync", side_effect=OSError("disk full")):
            with patch("app.backup.Path.unlink", side_effect=PermissionError()):
                with self.assertLogs("app.backup") as logs:
                    result = backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertFalse(result.success)
        self.assertTrue(result.path.exists())
        self.assertIn("incomplete backup file", "".join(logs.output))

    def test_interrupt_during_write_cleans_up_and_propagates(self) -> None:
        with patch("app.backup.os.fsync", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                backup_device(self.device, self.settings, backup_dir=self.backups)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_batch_continues_after_failure_and_returns_serializable_summary(self) -> None:
        self.session._open.side_effect = [NetmikoAuthenticationException(), None]
        summary = backup_devices(
            [self.device, Device("SW1", "192.0.2.2")], self.settings, backup_dir=self.backups
        )
        self.assertEqual(len(summary.successful), 1)
        self.assertEqual(len(summary.failed), 1)
        self.assertEqual(summary.failed[0].error.code, "authentication")
        self.assertEqual(summary.successful[0].device.name, "SW1")
        data = json.loads(json.dumps(summary.to_dict()))
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["success_count"], 1)
        self.assertEqual(data["failure_count"], 1)

    def test_default_inventory_is_loaded(self) -> None:
        with patch("app.backup.load_inventory", return_value=[self.device]) as load:
            summary = backup_devices(settings=self.settings, backup_dir=self.backups)
        load.assert_called_once_with()
        self.assertEqual(len(summary.successful), 1)

    def test_network_options_are_forwarded(self) -> None:
        result = backup_device(
            self.device, self.settings, backup_dir=self.backups, connect_timeout=15, read_timeout=120
        )
        self.assertTrue(result.success)
        self.assertEqual(self.factory.call_args.kwargs["conn_timeout"], 15)
        self.assertEqual(self.session.send_command.call_args.kwargs["read_timeout"], 120)

    def test_logs_and_summary_do_not_contain_configuration(self) -> None:
        with self.assertLogs("app", level="DEBUG") as logs:
            summary = backup_devices([self.device], self.settings, backup_dir=self.backups)
        text = "".join(logs.output) + repr(summary) + str(summary.to_dict())
        for value in (self.settings.password, self.settings.secret, self.configuration):
            self.assertNotIn(value, text)
        self.assertNotIn("enable secret", text)

    def test_cli_backup_mode_reports_summary_and_failure_exit_code(self) -> None:
        summaries = [
            BackupSummary([BackupResult(self.device, self.backups / "saved.cfg")]),
            BackupSummary([BackupResult(self.device, error=BackupIssue("timeout", "Timed out."))]),
        ]
        for summary, expected in zip(summaries, (0, 1)):
            with self.subTest(expected=expected):
                output = io.StringIO()
                with patch("app.main.load_settings", return_value=self.settings):
                    with patch("app.main.load_inventory", return_value=[self.device]):
                        with patch("app.main.backup_devices", return_value=summary) as backup:
                            with patch("app.main.configure_logging", return_value=logging.getLogger("test")):
                                with redirect_stdout(output), redirect_stderr(output):
                                    status = main(["--backup", "--backup-dir", str(self.backups)])
                self.assertEqual(status, expected)
                backup.assert_called_once_with([self.device], self.settings, backup_dir=self.backups)
                self.assertIn("Configuration backups", output.getvalue())
                self.assertNotIn("No SSH connections were opened", output.getvalue())


if __name__ == "__main__":
    unittest.main()

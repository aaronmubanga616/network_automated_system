"""Confirmed snippet application and transport integration without live SSH."""

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from netmiko import BaseConnection
from netmiko.exceptions import ConfigInvalidException, NetmikoAuthenticationException, ReadTimeout

from app.config_manager import Settings, apply_configuration, load_configuration_plan
from app.connection import check_connectivity, send_config_commands
from app.exceptions import ConfigurationError, NetworkOperationError
from app.inventory import Device


class ConfigurationTests(unittest.TestCase):
    """Ensure approval, sanitization, and session cleanup at reusable API boundaries."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "snippet.txt"
        self.file.write_text("! comment\nhostname LAB\n\ninterface GigabitEthernet0/1\n description lab uplink\n exit\n", encoding="utf-8")
        self.plan = load_configuration_plan(self.file)
        self.device = Device("R1", "192.0.2.1")
        self.settings = Settings(uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex)
        self.session = MagicMock(spec=BaseConnection)
        factory_patch = patch("app.connection.ConnectHandler", return_value=self.session)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)

    def test_plan_loads_comments_and_hides_commands_in_repr(self) -> None:
        self.assertEqual(len(self.plan.commands), 4)
        self.assertEqual(self.plan.commands[0], "hostname LAB")
        self.assertNotIn("hostname LAB", repr(self.plan))
        self.assertEqual(len(self.plan.digest), 64)

    def test_empty_missing_or_unsupported_snippet_fails(self) -> None:
        for contents in ("", "! comment", "banner motd ^", "configure terminal", "end", "do write memory", "hostname X\x00"):
            with self.subTest(contents=contents):
                self.file.write_text(contents, encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_configuration_plan(self.file)
        with self.assertRaises(ConfigurationError):
            load_configuration_plan(self.file.parent / "missing.txt")

    def test_unconfirmed_plan_never_opens_ssh(self) -> None:
        result = apply_configuration(self.device, self.plan, self.settings)
        self.assertFalse(result.success)
        self.assertFalse(result.may_have_changed)
        self.factory.assert_not_called()

    def test_unconfirmed_transport_call_sends_nothing(self) -> None:
        with self.assertRaises(NetworkOperationError):
            send_config_commands(self.session, self.plan.commands)
        self.session.send_config_set.assert_not_called()

    def test_confirmed_plan_uses_connection_module_and_does_not_save(self) -> None:
        result = apply_configuration(self.device, self.plan, self.settings, confirmed=True)
        self.assertTrue(result.success)
        self.session.send_config_set.assert_called_once()
        self.assertEqual(self.session.send_config_set.call_args.args[0], self.plan.commands)
        self.assertTrue(self.session.send_config_set.call_args.kwargs["cmd_verify"])
        self.session.save_config.assert_not_called()
        self.session.disconnect.assert_called_once()

    def test_rejected_command_has_safe_error_and_partial_change_flag(self) -> None:
        self.session.send_config_set.side_effect = ConfigInvalidException(self.settings.password)
        with self.assertLogs("app", level="DEBUG") as logs:
            result = apply_configuration(self.device, self.plan, self.settings, confirmed=True)
        self.assertEqual(result.error.code, "command")
        self.assertTrue(result.may_have_changed)
        self.assertNotIn(self.settings.password, repr(result) + "".join(logs.output))
        self.session.disconnect.assert_called_once()

    def test_connection_failure_does_not_claim_commands_were_sent(self) -> None:
        self.session._open.side_effect = NetmikoAuthenticationException()
        result = apply_configuration(self.device, self.plan, self.settings, confirmed=True)
        self.assertFalse(result.may_have_changed)
        self.assertEqual(result.error.code, "authentication")
        self.session.send_config_set.assert_not_called()

    def test_timeout_warns_of_partial_changes_and_closes(self) -> None:
        self.session.send_config_set.side_effect = ReadTimeout(self.settings.password)
        result = apply_configuration(self.device, self.plan, self.settings, confirmed=True)
        self.assertEqual(result.error.code, "timeout")
        self.assertTrue(result.may_have_changed)
        self.session.disconnect.assert_called_once()

    def test_connectivity_check_does_not_require_enable_or_send_show(self) -> None:
        result = check_connectivity(self.device, self.settings)
        self.assertTrue(result.success)
        self.session.enable.assert_not_called()
        self.session.send_command.assert_not_called()
        self.session.disconnect.assert_called_once()

    def test_connectivity_failure_returns_result(self) -> None:
        self.session._open.side_effect = NetmikoAuthenticationException()
        result = check_connectivity(self.device, self.settings)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "authentication")


if __name__ == "__main__":
    unittest.main()

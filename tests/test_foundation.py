"""Offline behavior checks for the initial project foundation."""

import io
import logging
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.config_manager import load_settings
from app.exceptions import ConfigurationError, InventoryError
from app.inventory import load_inventory
from app.main import main


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inventory = self.root / "devices.yaml"
        self.env_file = self.root / ".env"
        self.credentials = {
            "NETMIKO_USERNAME": uuid.uuid4().hex,
            "NETMIKO_PASSWORD": uuid.uuid4().hex,
            "NETMIKO_SECRET": uuid.uuid4().hex,
        }

    def write_inventory(self, content):
        self.inventory.write_text(content, encoding="utf-8")

    def test_inventory_defaults(self):
        self.write_inventory("devices:\n  - name: R1\n    host: 192.0.2.1\n")
        device, = load_inventory(self.inventory)
        self.assertEqual((device.name, device.device_type, device.port), ("R1", "cisco_ios", 22))

    def test_invalid_inventory_is_rejected(self):
        invalid_documents = [
            "", "[]", "devices: []", "devices: {}", "devices: [",
            "devices: [plain-text]",
            "devices: [{name: R1}]",
            "devices: [{name: R1, host: 123}]",
            "devices: [{name: R1, host: 'http://router'}]",
            "devices: [{name: R1, host: router, password: forbidden}]",
            "devices: [{name: R1, host: router, device_type: juniper_junos}]",
            "devices: [{name: R1, host: router, port: true}]",
            "devices: [{name: R1, host: router, port: '22'}]",
            "devices: [{name: R1, host: router, port: 0}]",
            "devices: [{name: R1, host: router, port: 65536}]",
            "devices: [{name: R1, host: a}, {name: r1, host: b}]",
            "devices: [{name: R1, host: a}, {name: R2, host: A}]",
        ]
        for document in invalid_documents:
            with self.subTest(document=document):
                self.write_inventory(document)
                with self.assertRaises(InventoryError):
                    load_inventory(self.inventory)

    def test_missing_inventory(self):
        with self.assertRaisesRegex(InventoryError, "not found"):
            load_inventory(self.inventory)

    def test_yaml_errors_do_not_expose_source(self):
        value = uuid.uuid4().hex
        self.write_inventory(f"devices: [{value}")
        with self.assertRaises(InventoryError) as caught:
            load_inventory(self.inventory)
        self.assertNotIn(value, str(caught.exception))

    def test_environment_credentials_and_safe_repr(self):
        with patch.dict(os.environ, self.credentials, clear=True):
            settings = load_settings(self.env_file)
        self.assertEqual(settings.password, self.credentials["NETMIKO_PASSWORD"])
        for value in self.credentials.values():
            self.assertNotIn(value, repr(settings))

    def test_dotenv_fallback_and_environment_precedence(self):
        self.env_file.write_text(
            "\n".join(f"{key}={value}" for key, value in self.credentials.items()),
            encoding="utf-8",
        )
        override = uuid.uuid4().hex
        with patch.dict(os.environ, {"NETMIKO_USERNAME": override}, clear=True):
            settings = load_settings(self.env_file)
        self.assertEqual(settings.username, override)
        self.assertEqual(settings.password, self.credentials["NETMIKO_PASSWORD"])

    def test_password_interpolation_is_disabled(self):
        value = uuid.uuid4().hex + "${NOT_A_VARIABLE}"
        self.env_file.write_text(f"NETMIKO_PASSWORD='{value}'\n", encoding="utf-8")
        with patch.dict(os.environ, {"NETMIKO_USERNAME": uuid.uuid4().hex}, clear=True):
            self.assertEqual(load_settings(self.env_file).password, value)

    def test_missing_or_blank_credentials(self):
        for variables in ({}, {**self.credentials, "NETMIKO_PASSWORD": "  "}):
            with self.subTest(variables=list(variables)):
                with patch.dict(os.environ, variables, clear=True):
                    with self.assertRaises(ConfigurationError):
                        load_settings(self.env_file)

    def test_optional_secret_and_invalid_log_level(self):
        variables = {key: value for key, value in self.credentials.items() if key != "NETMIKO_SECRET"}
        with patch.dict(os.environ, variables, clear=True):
            self.assertIsNone(load_settings(self.env_file).secret)
            os.environ["APP_LOG_LEVEL"] = "invalid"
            with self.assertRaisesRegex(ConfigurationError, "APP_LOG_LEVEL"):
                load_settings(self.env_file)

    def test_cli_success_and_failure_do_not_log_credentials(self):
        self.write_inventory("devices:\n  - name: R1\n    host: 192.0.2.1\n")
        output = io.StringIO()
        log_file = self.root / "logs" / "app.log"
        arguments = ["--validate", "--inventory", str(self.inventory), "--env-file", str(self.env_file)]
        try:
            with patch.dict(os.environ, self.credentials, clear=True):
                with patch("app.main.DEFAULT_LOG_FILE", log_file):
                    with redirect_stdout(output), redirect_stderr(output):
                        self.assertEqual(main(arguments), 0)
                        self.write_inventory("devices: []")
                        self.assertEqual(main(arguments), 1)
            text = output.getvalue() + log_file.read_text(encoding="utf-8")
            self.assertIn("R1", text)
            self.assertIn("No SSH connections were opened", text)
            self.assertIn("ERROR", text)
            for value in self.credentials.values():
                self.assertNotIn(value, text)
        finally:
            logger = logging.getLogger("app")
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()

    def test_cli_missing_credentials_has_friendly_error(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(output):
            self.assertEqual(main(["--validate", "--env-file", str(self.env_file)]), 1)
        self.assertIn("NETMIKO_USERNAME", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()

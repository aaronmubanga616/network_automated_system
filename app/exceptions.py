"""Errors that callers can explain without exposing transport diagnostics."""

from typing import Literal

OperationErrorCode = Literal[
    "authentication", "timeout", "unreachable", "ssh", "enable",
    "command", "configuration", "unexpected",
]


class NetworkAutomationError(Exception):
    """Base class for expected application errors."""


class ConfigurationError(NetworkAutomationError):
    """Application settings are missing or invalid."""


class InventoryError(NetworkAutomationError):
    """The device inventory cannot be read or does not match the schema."""


class CollectionParseError(NetworkAutomationError):
    """Device output could not be converted into the expected structured records."""


class ReportError(NetworkAutomationError):
    """A structured report could not be saved."""


class NetworkOperationError(NetworkAutomationError):
    """A sanitized network failure with a stable code for callers and reports."""

    def __init__(self, code: OperationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code

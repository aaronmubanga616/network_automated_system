"""Simple interactive Rich menu orchestrating the existing application modules."""

import logging
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from app import backup, collector, config_manager, connection, health, report
from app.exceptions import NetworkAutomationError
from app.inventory import Device

logger = logging.getLogger(__name__)
MENU = (
    ("1", "List devices", "Read only"),
    ("2", "Check connectivity", "Read only"),
    ("3", "Show interfaces", "Read only"),
    ("4", "Show routing tables", "Read only"),
    ("5", "Backup configurations", "Read only - saves local files"),
    ("6", "Run health check", "Read only"),
    ("7", "Generate report", "Read only - saves local file"),
    ("8", "Apply configuration", "CHANGES NETWORK CONFIGURATION"),
    ("0", "Exit", ""),
)


class InteractiveCLI:
    """Menu state and prompts only; domain actions remain in their own modules."""

    def __init__(
        self,
        devices: list[Device],
        *,
        env_file: Path = config_manager.DEFAULT_ENV_FILE,
        backup_dir: Path = backup.DEFAULT_BACKUP_DIR,
        report_dir: Path = report.DEFAULT_REPORT_DIR,
        console: Console | None = None,
    ) -> None:
        self.devices = devices
        self.env_file = env_file
        self.backup_dir = backup_dir
        self.report_dir = report_dir
        self.console = console if console is not None else Console()

    def _menu(self) -> None:
        """Display exact menu choices with an explicit distinction for writes."""
        self.console.print(Panel("Cisco IOS Network Automation", subtitle=f"{len(self.devices)} inventory devices", border_style="cyan"))
        table = Table(box=None, padding=(0, 2), show_header=False)
        for number, label, impact in MENU:
            style = "bold red" if number == "8" else "cyan"
            table.add_row(Text(number, style=style), Text(label), Text(impact, style=style if number == "8" else "dim"))
        self.console.print(table)

    def _select_devices(self, *, changing: bool = False) -> list[Device]:
        """Select explicit targets; changing operations default to cancellation."""
        table = Table(title="Select target devices")
        for column in ("Number", "Device", "Host"):
            table.add_column(column)
        for index, device in enumerate(self.devices, 1):
            table.add_row(str(index), Text(device.name), Text(device.host))
        self.console.print(table)
        while True:
            selected = Prompt.ask(
                "Devices: all, comma-separated numbers, or 0 to cancel",
                default="0" if changing else "all", console=self.console,
            ).strip().casefold()
            if selected == "0":
                return []
            if selected == "all":
                return list(self.devices)
            pieces = selected.split(",")
            if all(piece.strip().isascii() and piece.strip().isdecimal() for piece in pieces):
                indices = list(dict.fromkeys(int(piece) for piece in pieces))
                if indices and all(1 <= index <= len(self.devices) for index in indices):
                    return [self.devices[index - 1] for index in indices]
            self.console.print("Enter valid device numbers, all, or 0.", style="yellow")

    def _settings(self) -> config_manager.Settings:
        """Load credentials only when an action needs them; allow fixing .env and retrying."""
        settings = config_manager.load_settings(self.env_file)
        logging.getLogger("app").setLevel(settings.log_level)
        return settings

    def _apply_configuration(self, targets: list[Device]) -> None:
        """Review immutable input and targets, then require explicit default-no approval."""
        path_text = Prompt.ask("Configuration snippet file (blank to cancel)", default="", console=self.console).strip()
        if not path_text:
            self.console.print("Cancelled. No configuration changes sent.")
            return
        plan = config_manager.load_configuration_plan(Path(path_text))
        settings = self._settings()
        self.console.print(Panel(
            Text(
                f"File: {plan.source}\nCommands: {len(plan.commands)}\n"
                f"Targets: {', '.join(device.name + ' (' + device.host + ')' for device in targets)}\n\n"
                "Review this file before continuing. Command text is hidden because it may contain passwords.\n"
                "Commands change running configuration immediately. No automatic save or rollback is performed."
            ), title="Network configuration change", border_style="red",
        ))
        if not Confirm.ask("Apply this loaded configuration to the listed devices?", default=False, console=self.console):
            self.console.print("Cancelled. No configuration changes sent.")
            return
        results = []
        try:
            for device in targets:
                with self.console.status(f"Applying configuration to {device.name}..."):
                    result = config_manager.apply_configuration(device, plan, settings, confirmed=True)
                results.append(result)
                if not result.success:
                    self.console.print("Stopped after a failure. Remaining devices were not changed.", style="yellow")
                    break
        except KeyboardInterrupt:
            self.console.print("Interrupted. Partial configuration changes may remain; inspect affected devices before retrying.", style="yellow")
            raise
        report.display_configuration_results(results, self.console)

    def _perform(self, choice: str) -> None:
        """Route an action to existing modules, with fresh collection for data views."""
        if choice == "1":
            report.display_inventory(self.devices, self.console)
            return
        targets = self._select_devices(changing=choice == "8")
        if not targets:
            self.console.print("Cancelled.")
            return
        if choice == "8":
            self._apply_configuration(targets)
            return
        settings = self._settings()
        if choice == "2":
            results = []
            for device in targets:
                with self.console.status(f"Checking SSH on {device.name}..."):
                    results.append(connection.check_connectivity(device, settings))
            report.display_connectivity(results, self.console)
        elif choice == "5":
            with self.console.status("Backing up running configurations..."):
                summary = backup.backup_devices(targets, settings, backup_dir=self.backup_dir)
            report.display_backup_summary(summary, self.console)
        else:
            with self.console.status("Collecting current device information..."):
                snapshots = collector.collect_devices(targets, settings)
            if choice in {"3", "4"}:
                report.display_device_tables(snapshots, "interfaces" if choice == "3" else "routes", self.console)
            else:
                reports = health.evaluate_devices(snapshots)
                report.display_health(reports, self.console)
                self.console.print("No required routes configured; an empty routing table still triggers a warning.", style="dim")
                if choice == "7":
                    path = report.generate_report(snapshots, reports, self.report_dir)
                    self.console.print(Text(f"Report saved: {path}", style="green"))

    def run(self) -> int:
        """Keep the menu usable after expected failures; EOF exits, Ctrl+C cancels."""
        while True:
            try:
                self._menu()
                choice = Prompt.ask("Choose an action", choices=[item[0] for item in MENU], default="0", console=self.console)
                if choice == "0":
                    self.console.print("Goodbye.", style="cyan")
                    return 0
                self._perform(choice)
            except EOFError:
                self.console.print("Input closed. Goodbye.")
                return 0
            except KeyboardInterrupt:
                self.console.print("Action cancelled. Returning to menu.", style="yellow")
            except NetworkAutomationError as exc:
                self.console.print(Text(str(exc), style="red"))
                logger.warning("Interactive action failed: %s", exc)
            except Exception:
                # Never print arbitrary exception text: it can contain commands
                # or credentials from a third-party operation.
                self.console.print("The action could not complete. Returning to menu.", style="red")
                logger.error("Unexpected interactive action failure; diagnostic details withheld.")

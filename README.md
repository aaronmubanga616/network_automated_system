# Network Automation System

A modular Python foundation for automating Cisco IOS routers and switches in GNS3
over SSH with Netmiko.

**Current scope:** load environment-based settings, validate a YAML inventory,
write application logs, display an inventory table, and provide reusable Netmiko
SSH sessions, show-command execution, structured device information collection,
running-configuration backups, structured network health checks, JSON reports,
and explicitly confirmed configuration snippets. The default CLI opens an
interactive Rich menu. Batch backup and local validation modes remain available.

## Setup

Use Python 3.10 or newer. From the project directory, run these commands in
PowerShell (use `py` instead of `python` if that is your installed launcher):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally to supply `NETMIKO_USERNAME` and `NETMIKO_PASSWORD`, and
optionally `NETMIKO_SECRET` for IOS enable mode. Alternatively, provide these
variables directly in the process environment; `.env` is optional. Existing
environment variables take precedence. Values are never hardcoded in the app,
printed, or included in the settings object's representation. Quote values in
`.env` when needed; variable interpolation is disabled so `$` expressions in
passwords remain literal. Never commit the populated `.env` file.

Set `APP_LOG_LEVEL` to `DEBUG`, `INFO` (default), `WARNING`, `ERROR`, or `CRITICAL`.
Logs rotate at approximately 1 MB, retaining three older files, under `logs/`.

Update `inventory/devices.yaml` with your GNS3 management addresses, then run:

```powershell
.\.venv\Scripts\python.exe -m app.main
```

To select other local files:

```powershell
.\.venv\Scripts\python.exe -m app.main --inventory inventory/devices.yaml --env-file .env
```

The default inventory, environment file, and log file are resolved relative to
the project directory. Explicit CLI paths are relative to the working directory.
Run the CLI as a module from the project directory, not as `python app/main.py`.
Exit codes are `0` for normal menu exit or successful validation/backup, `1` for
startup/validation errors or any failed batch backup, `2` for invalid CLI
arguments, and `130` for interruption outside the menu. Interactive actions report
their own failures and return to the menu. Ctrl+C cancels an action; EOF or option
0 exits. A normal menu exit does not imply all prior actions succeeded.

## Interactive menu

Run `python -m app.main` (or the virtual-environment executable above) to open:

```text
1. List devices
2. Check connectivity
3. Show interfaces
4. Show routing tables
5. Backup configurations
6. Run health check
7. Generate report
8. Apply configuration
0. Exit
```

The menu labels options 1-7 as read-only with respect to network configuration;
backups and reports additionally write local files. Option 8 is highlighted as a
network configuration change. Listing inventory and exiting require no credentials.
Network actions load the configured environment file when selected, so missing
settings can be fixed and the action retried without restarting the menu.

Device actions accept `all`, comma-separated device numbers, or `0` to cancel.
Read-only selections default to all devices; configuration selection defaults to
cancel. Invalid choices reprompt. Connectivity checks test SSH login/session setup
without requiring enable mode. Interface and routing views use freshly collected,
structured records. Health checks use the existing evaluator with its default
policy; use the Python API for per-device required routes.

Reports collect fresh data and save timestamped JSON files under `reports/`,
including device snapshots, collection errors, and health findings. Reports never
include connection settings, configuration snippets, or running-configuration
backups. Existing report filenames are not overwritten. Use `--report-dir` or
`--backup-dir` to change output destinations.

Option 8 loads a UTF-8 snippet with one non-interactive IOS configuration command
per line. Blank lines and lines starting with `!` or `#` are ignored. For example:

```text
interface GigabitEthernet0/1
 description Lab uplink
 exit
```

Supply configuration-mode commands only. Omit `configure terminal`, `end`,
exec/save commands, and multiline banners; this workflow is for snippets rather
than restoration of a full backup. The review screen identifies the file,
command count, and every target device. Inspect the file before approving it.
Command text is hidden because it may contain passwords. The commands are loaded
once and kept in an immutable plan, so later edits to the file cannot alter an
already-reviewed operation.

Configuration application requires an explicit yes, with **No as the default**.
Declining, cancelling, or closing input before confirmation opens no SSH session
for configuration application. The configuration API and transport helper also
require `confirmed=True`. Applied commands change running configuration; the app
does not automatically save, retry, or roll back. A failed command or interrupted
session can leave earlier changes in place. The batch stops on the first failed
device and reports that later devices were not changed.

For non-interactive modes:

```powershell
.\.venv\Scripts\python.exe -m app.main --validate
.\.venv\Scripts\python.exe -m app.main --backup
```

`--interactive` explicitly selects the default menu. These modes are mutually
exclusive. `--validate` retains the original local validation and inventory view.

## Inventory format

`devices` is a non-empty list. Every entry needs a unique `name` and a `host`
(management IP address or DNS hostname). `device_type` defaults to `cisco_ios`,
the only supported platform in this initial version. `port` defaults to `22`
and must be an integer from 1 to 65535. Host/port pairs must be unique. Unknown
fields, including credential fields, are rejected. Validation checks the local
schema; it does not resolve hostnames or test reachability.

Both routers and switches use Netmiko's `cisco_ios` platform identifier.
The example addresses are placeholders for your lab; use reachable management
addresses, not GNS3 console ports, for SSH connections.

## Connection layer

`execute_show_commands()` returns a `CommandResult` for each device. Failed devices
do not stop a caller's loop. Results include `success`, `outputs` (a dictionary
keyed by command), and an optional `error` with a stable `code` and safe message.
Completed outputs remain available if a later command fails; remaining commands
on that device are skipped. Duplicate commands overwrite their earlier output.

```python
from app.config_manager import DEFAULT_LOG_FILE, load_settings
from app.connection import execute_show_commands
from app.inventory import load_inventory
from app.logging_config import configure_logging

settings = load_settings()
logger = configure_logging(settings.log_level, DEFAULT_LOG_FILE)
for device in load_inventory():
    result = execute_show_commands(
        device,
        ["show version", "show ip interface brief"],
        settings,
        connect_timeout=10,
        read_timeout=30,
    )
    if result.error is not None:
        logger.warning("%s: %s", device.name, result.error)
        continue
    # Pass result.outputs to a collector or report; do not log raw output.
```

For multiple operations on the same session, use the context manager and command
helper. The context manager closes the session after success, failure, or an
interruption. Catch `NetworkAutomationError` around each device when using this
lower-level interface:

```python
from app.connection import open_connection, send_show_command
from app.exceptions import NetworkAutomationError

for device in load_inventory():
    try:
        with open_connection(device, settings) as connection:
            version = send_show_command(connection, "show version")
            interfaces = send_show_command(connection, "show ip interface brief")
    except NetworkAutomationError as error:
        logger.warning("%s: %s", device.name, error)
```

Both session entry points load environment settings automatically when `settings`
is omitted. Reuse preloaded settings to select a custom `.env` file or avoid
repeated loading. Enable mode is requested by default; `NETMIKO_SECRET` is used
if IOS requires it. A device already at the required privilege can work without
a secret. Use `enable=False` for user-mode commands.

The connection timeout bounds TCP, authentication, and SSH banner waits; the
separate read timeout bounds each show command, not the entire batch. Error codes
are `authentication`, `timeout`, `unreachable`, `ssh`, `enable`, `command`,
`configuration`, and `unexpected`. Netmiko can wrap refused connections and DNS
failures as timeouts; the layer inspects the underlying exception when available.
If the underlying cause is unavailable, the timeout message also suggests
checking reachability. Caller programming errors and interrupts are not suppressed.

Logs contain device metadata, connection stages, and sanitized failures. They
exclude credentials, command text, raw device output, and raw transport exception
messages. Session transcripts are disabled, and result representations exclude
command output. Output returned to callers can still contain device secrets, so
treat it as sensitive. The show-command helper validates single-line `show`
commands and recognizes common IOS rejection messages; it is not a replacement
for device-side command authorization. Cleanup failures are logged without hiding
the original result; verify the session on the device if cleanup fails.

The implementation uses [Netmiko's connection API](https://ktbyers.github.io/netmiko/docs/netmiko/),
retaining the connection object before opening the transport so partial sessions
can be cleaned up. It follows Netmiko 4.x's `auto_connect=False` / `_open()` pattern.

## Structured device collection

`collect_device(device, settings)` collects one device. `collect_devices()` loads
the default inventory and collects every device sequentially, retaining failed
devices in the returned list. Pass a list from `load_inventory(custom_path)` to
use a different inventory. All SSH operations remain in `app/connection.py`;
the collector reuses one context-managed session per device.

```python
from app.collector import collect_devices
from app.config_manager import DEFAULT_LOG_FILE, load_settings
from app.logging_config import configure_logging

settings = load_settings()
configure_logging(settings.log_level, DEFAULT_LOG_FILE)
results = collect_devices(settings=settings, connect_timeout=10, read_timeout=30)
snapshots = [result.to_dict() for result in results]
# snapshots contains plain Python dictionaries/lists suitable for a report or JSON.
```

Each `DeviceInformation` result contains:

| Field | Contents |
| --- | --- |
| `device` | Inventory identity, host, platform, and SSH port. |
| `collected_at` | Collection start timestamp in UTC. |
| `hostname` | Actual device hostname parsed from `show version`, not the inventory label. |
| `uptime` | Device-reported uptime text, for example `2 weeks, 3 days, 4 hours, 5 minutes`. |
| `version` | Parsed `show version` dictionary, including IOS version, image, hardware, and serial fields when available. |
| `ip_interface_brief` | Interface dictionaries with `interface`, `ip_address`, `status`, and `proto`. |
| `ip_route` | Route-path dictionaries with protocol, network, prefix length, distance, metric, next-hop address/interface, and other template fields. Multiple next hops remain separate rows. |
| `interface_status` | Dictionaries with `interface`, `status`, `protocol_status`, `description`, `vlan`, `duplex`, `speed`, and `type`. |
| `interface_status_source` | Either `show interfaces status` or the fallback `show interfaces`. |
| `errors` | Issues with `section`, `command`, `code`, and a sanitized `message`. |
| `success` | True only when all required sections were collected without unresolved issues. |

`to_dict()` converts the result, inventory device, and issues to plain nested data
and includes `success`. Numeric CLI values remain strings; multi-valued fields
such as hardware and serial numbers remain lists. Unassigned interface addresses
remain `"unassigned"`. Fields absent from a template record are usually empty
strings. `None` means a section was unavailable or not collected; `[]` means a
recognizable empty table was collected successfully.

The collector tries `show interfaces status` for switch port details. If IOS
rejects that command or its output cannot be parsed, it tries `show interfaces`
on the same session. Router link/protocol states and switch port states are
mapped to consistent field names while preserving the reported status strings.
Some fields are unavailable in a given command: for example, switch status
tables lack line-protocol state, while routed interfaces may lack a VLAN value.

Command rejections and parsing failures leave an issue and allow other sections
to continue. Transport errors stop the current device to avoid reusing a broken
session; the batch continues with the next device. Successfully collected data
is retained. Parsing failures use code `parse`; connection errors retain the
connection layer's codes. Inventory errors occur before collection and raise
`InventoryError`. Interrupts still propagate after session cleanup.

Parsing uses [NTC Templates](https://ntc-templates.readthedocs.io/en/latest/user/lib_getting_started/),
now declared as a direct dependency. The collector never substitutes raw text for
structured records. Template support varies by IOS image and output format, and
templates can ignore unrecognized lines; test your lab images before relying on
complete field coverage. Logs describe collection stages, record counts, and safe
errors without including credentials or device output. Snapshot data is excluded
from the object's printed representation but is available through `to_dict()`.

## Configuration backups

Back up every device in the inventory with:

```powershell
.\.venv\Scripts\python.exe -m app.main --backup
```

The command automatically creates the project's `backups/` directory, connects
through the existing connection layer in enable mode, executes
`show running-config`, and saves each result as UTF-8. Filenames use the inventory
device name and UTC save time: `R1_2026-10-05_21-30-15.cfg`.

Optional input and destination overrides:

```powershell
.\.venv\Scripts\python.exe -m app.main --backup --inventory inventory/devices.yaml --env-file .env --backup-dir backups
```

The CLI displays every device's outcome and successful/failed counts. A failed
device does not stop later devices. Exit status is `0` when all backups succeed
and `1` if any fail. Use `--validate` for local input validation without the menu.

The reusable Python interface returns a summary:

```python
from app.backup import backup_devices
from app.config_manager import DEFAULT_LOG_FILE, load_settings
from app.logging_config import configure_logging

settings = load_settings()
configure_logging(settings.log_level, DEFAULT_LOG_FILE)
summary = backup_devices(settings=settings, read_timeout=60)
successful = summary.successful
failed = summary.failed
data = summary.to_dict()
# data has total, success_count, failure_count, successful, and failed.
```

Use `backup_device(device, settings)` for one device, or pass a device iterable
to `backup_devices(devices, settings)`. Both accept `backup_dir`,
`connect_timeout`, and `read_timeout`. With no supplied settings, the connection
layer loads environment credentials. Invalid inventory raises `InventoryError`
before any device operations begin.

Each result contains the inventory `device`, a saved or attempted `path`, a
`success` flag, and an optional `error` with a code and safe message. Summary
conversion produces plain dictionaries and string paths suitable for JSON.
Network failures retain the connection layer's error codes. Backup-specific
codes are `invalid_name`, `directory`, `invalid_output`, `collision`, and `write`.

Files are created exclusively: existing backups are never overwritten, including
when two runs execute concurrently. A same-device, same-second collision is
reported as a failure; run again in a later second to create another backup.
No suffix is added, so the requested filename format remains exact. Device names
must contain 1-100 ASCII letters, digits, dots, underscores, or hyphens and start
with a letter or digit. Unsafe names fail before SSH instead of being rewritten.

Empty output and output missing the final IOS `end` marker are rejected before
writing. This is a basic completeness check, not a configuration parser or restore
test. A successful write is flushed and synced before it is reported. Failed or
interrupted writes trigger cleanup of only the newly created file; a cleanup
failure is logged, and the failed result's path identifies the file to inspect.
Interruptions propagate after cleanup. Abrupt process or machine termination can
still leave an incomplete file, which later runs will not overwrite.

Configurations can contain secrets and remain under the Git-ignored `backups/`
directory. Logs and summaries contain outcome metadata, never configuration
content or credentials. This implements a backup run; it does not install a
recurring scheduler.

## Network health checks

`app/health.py` evaluates existing `DeviceInformation` snapshots. It does not open
SSH sessions, recollect data, or inspect raw terminal output. Use the collector
to obtain fresh data, then evaluate one device with `evaluate_health()` or a batch
with `evaluate_devices()`:

```python
from app.collector import collect_devices
from app.health import HealthPolicy, evaluate_devices

snapshots = collect_devices()
reports = evaluate_devices(
    snapshots,
    policies={"R1": HealthPolicy(required_routes=("0.0.0.0/0", "198.51.100.0/24"))},
)
health_data = [report.to_dict() for report in reports]
```

Policy keys are inventory device names. Each report contains `device`,
`observed_at`, `evaluated_at`, `state`, and `findings`. A finding contains a stable
`code`, its `state`, a safe `message`, and optional `entity` and `source`. Overall
state is the highest finding severity:

| Condition | State |
| --- | --- |
| Complete snapshot, no detected issues | `HEALTHY` |
| Administratively disabled or operationally down interface | `WARNING` |
| Interface line protocol down | `WARNING` |
| Empty routing table with no required-route policy | `WARNING` |
| Missing, malformed, unknown, or conflicting data; failed health check | `WARNING` |
| Unreachable management endpoint, failed SSH/authentication/enable, or SSH timeout | `CRITICAL` |
| Explicitly required route absent from valid collected route data | `CRITICAL` |

SSH health uses `CollectionIssue.code`; diagnostic message wording never controls
classification. A timeout is reported as a timeout, not proof that a device is
offline. State is based on the snapshot's observation time; evaluation does not
confirm present reachability or guarantee end-to-end network health.

Interface checks use structured status fields from both IP interface brief and
interface status. Known aliases such as `Gi0/1` and `GigabitEthernet0/1` share one
identity. Administrative shutdown suppresses a redundant operational-down finding,
while line-protocol-down remains separately visible. Switch states such as
`notconnect`, `err-disabled`, and `suspended` are operational-down warnings.
These warnings do not assume an unused port should be enabled. Unknown states
warn instead of being treated as healthy. Missing line protocol in a switch
status table is expected; the brief table supplies it where available.

Routes use `ipaddress.IPv4Network` comparisons over parsed network/prefix fields.
Expected prefixes are canonicalized and matched exactly in the global IPv4
table. A default/covering route does not satisfy a required more-specific prefix;
multiple next hops for the same prefix are accepted. A VRF route cannot satisfy
a global-table requirement. No default route is required unless explicitly
listed. IPv6 and per-VRF expectations are outside the current collector scope.
An unavailable, failed, or malformed route table produces a warning rather than
an unsupported claim that a required route is missing. Findings depend on the
collector templates recognizing your IOS output accurately.

Extend the module with callables accepting `(snapshot, policy)` and returning
an iterable of `HealthFinding`. Pass them through `additional_checks`; built-in
checks still run:

```python
from app.health import HealthFinding, HealthState, evaluate_health

def check_lab_hostname(snapshot, policy):
    if snapshot.hostname and snapshot.hostname != snapshot.device.name:
        return [HealthFinding(
            code="LAB_HOSTNAME_MISMATCH",
            state=HealthState.WARNING,
            message="Device hostname differs from the lab inventory name.",
        )]
    return []

report = evaluate_health(snapshots[0], additional_checks=[check_lab_hostname])
```

Each extension runs independently. Exceptions and malformed findings become a
`HEALTH_CHECK_FAILED` warning, with raw diagnostics withheld, and later checks
continue. Interrupts propagate. Custom checks should return safe messages and
avoid credentials or raw device output. Built-in checks do not mutate snapshots;
logs include only device identity, overall state, and finding counts.

## Files and responsibilities

| File | Purpose |
| --- | --- |
| `app/__init__.py` | Marks the application as a Python package. |
| `app/main.py` | Interactive, validation, and backup modes, startup orchestration, and expected error handling. |
| `app/cli.py` | Rich menu, device selection, read-only actions, and default-no configuration confirmation. |
| `app/inventory.py` | Immutable device model, safe YAML loading, and inventory validation. |
| `app/config_manager.py` | Environment settings, immutable snippet plans, and confirmed configuration orchestration. |
| `app/logging_config.py` | Rich console logs and rotating application file logs. |
| `app/exceptions.py` | Configuration, inventory, parsing, and sanitized network errors. |
| `app/report.py` | Rich result tables and timestamped structured JSON reports. |
| `app/connection.py` | Netmiko SSH context manager, show-command helpers, and per-device results. |
| `app/collector.py` | Structured per-device snapshots, batch collection, status fallback, and partial failure reporting. |
| `app/parsers.py` | Pure NTC output parsing, required-field validation, and interface-status normalization. |
| `app/backup.py` | Exclusive timestamped configuration files, per-device outcomes, and backup batch summaries. |
| `app/health.py` | Structured health findings, severity aggregation, route policy, and extensible checks. |
| `inventory/devices.yaml` | Credential-free example router and switch inventory. |
| `.env.example` | Blank credential variable template and logging default. |
| `requirements.txt` | Runtime dependencies, including NTC Templates for structured parsing. |
| `.gitignore` | Excludes credentials, virtual environments, caches, and generated output. |
| `tests/test_foundation.py` | Offline tests for validation, credential handling, and CLI behavior. |
| `tests/test_connection.py` | Mocked SSH tests for errors, cleanup, enable mode, commands, and credential protection. |
| `tests/test_collector.py` | Real-template parsing and mocked-SSH collection tests for router/switch success, fallback, and partial failures. |
| `tests/test_backup.py` | Backup content, collision/concurrency, write cleanup, error isolation, summary, and CLI tests. |
| `tests/test_health.py` | Health severity, interface aliases, route evidence/policies, extensions, and parsed-fixture integration tests. |
| `tests/test_cli.py` | Scripted menu routing, confirmation/cancellation, report output, and failure recovery tests. |
| `tests/test_configuration.py` | Snippet loading, approval gates, transport reuse, and safe partial-failure behavior. |
| `tests/fixtures/` | Synthetic classic IOS and IOS XE output samples, including interface and routing tables. |
| `README.md` | Setup, supported scope, schema, architecture, and validation instructions. |

The CLI coordinates settings, inventory, logging, and presentation. Future network
operations should accept `Device` and `Settings` objects instead of reading files
or environment variables themselves. Imports have no network or logging setup
side effects. Netmiko handles SSH inside the connection module. Dependency ranges
are not a lockfile; a reproducible deployment can
add a lockfile in a later phase.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use temporary files, mocked SSH sessions, and generated synthetic credential
values. No GNS3 devices or real credentials are needed. Live SSH compatibility
must be verified against your own reachable IOS devices.

# Modbus

```
███╗   ███╗   ██████╗  ██████╗  ██████╗  ██╗   ██╗  ███████╗
████╗ ████║  ██╔═══██╗ ██╔  ██╗ ██╔══██╗ ██║   ██║  ██╔════╝
██╔████╔██║  ██║   ██║ ██║  ██║ ██████╔╝ ██║   ██║  ███████╗
██║╚██╔╝██║  ██║   ██║ ██║  ██║ ██╔══██╗ ██║   ██║  ╚════██║
██║ ╚═╝ ██║  ╚██████╔╝ ██████╔╝ ██████╔╝ ╚██████╔╝  ███████║
╚═╝     ╚═╝   ╚═════╝  ╚═════╝   ╚═════╝   ╚═════╝   ╚══════╝
```

**MODBUS for dummies.** TCP/UDP/RTU/ASCII/TLS + rich terminal UI.

[![License](https://img.shields.io/github/license/DarkSoulEngineer/modbus)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![pymodbus 3.5+](https://img.shields.io/badge/pymodbus-3.5+-green.svg)](https://pymodbus.readthedocs.io/)
[![rich 13+](https://img.shields.io/badge/rich-13+-orange.svg)](https://rich.readthedocs.io/)

A command-line client for Modbus devices such as PLCs, VFDs, energy meters,
and RTUs. It reads and writes registers, coils, and discrete inputs over TCP,
UDP, RTU, ASCII, or TLS. Features include range scanning, continuous watching
with change detection, register filling, state snapshots (save/restore), an
embedded test server with drifting sensor values, and an interactive REPL shell.

All output uses **rich** for beautiful tables, panels, progress spinners, and
value-bar visualizations. Machine-readable JSON is available via `--json` for
scripting and pipelines. All diagnostics go to stderr so stdout stays clean.

## Features

- **Five transports**: TCP, UDP, RTU, ASCII (serial), and TLS with optional
  client certificates and `--no-verify` mode.
- **Nine commands**: `read`, `write`, `fill`, `save`, `restore`, `scan`,
  `watch`, `simulate`, `tui` -- covering holding/input registers, coils, and
  discrete inputs.
- **Datatype conversion** for `u16`, `i16`, `u32`, `i32`, `u64`, `i64`, `f32`,
  `f64`, and `str`, with configurable byte order, word order, and a `--scale`
  factor for engineering units.
- **Standard Modbus address notation**: `40001-49999` (holding), `30001-39999`
  (input), `10001-19999` (discrete), `1-9999` (coil) -- auto-detected. Raw
  0-based offsets also work.
- **Rich terminal UI**: colored tables, panels, value bars, spinners, and
  live-updating watch dashboard.
- **Watch mode** with change-detection diffing, configurable interval/iterations,
  optional file logging (`--output`), and JSON-lines streaming (`--json`).
- **Scan mode** sweeps a register range and reports non-zero values (or all with `--all`).
- **Fill command** writes a single value across a register/coil block.
- **Save/Restore** snapshots device state to/from JSON files.
- **Embedded simulator** (`modbus simulate`) with multi-slave TCP/UDP server
  and drifting sensor values (temperature, pressure, flow, RPM, voltage, battery, totalizer).
- **Interactive REPL** (`modbus tui`) with multiple named connections, session
  persistence (`~/.modbus_session.json`), per-command flag overrides, and shell
  commands (`ls`, `cd`, `!cmd`).
- **Protocol limits enforced**: FC1/FC2 (2000 bits), FC3/FC4 (125 registers),
  FC5 (1 coil), FC6 (1 register), FC15 (1968 coils), FC16 (123 registers).
- **Deterministic exit codes** (0-5) and connection retries for unattended scripts.

## Installation

Requires Python 3.10+ and pymodbus 3.5+.

```bash
# Install from GitHub (provides `modbus` command)
pip install git+https://github.com/DarkSoulEngineer/modbus.git

# Or clone and install in development mode
git clone https://github.com/DarkSoulEngineer/modbus.git
cd modbus
pip install -e .              # installs `modbus` + `modbus.simulate`
pip install -e '.[tui]'       # also installs textual for the TUI extra

# Run without installing (uses the root `modbus` launcher script)
python -m modbus --help
# or
./modbus --help
```

After `pip install -e .`, invoke the tool with `modbus`. The examples below
use `modbus`; substitute `python -m modbus` or `./modbus` when running from
a checkout.

## Quick Start

```bash
# Read 10 holding registers (u16) from a PLC
modbus read 192.168.1.10

# Read 4 float32 values at address 200 (standard notation: 40201)
modbus read 192.168.1.10 -a 40201 --dtype f32 -c 4

# Write two registers (FC16) at address 40101
modbus write 192.168.1.10 -a 40101 123 456

# Machine-readable output for scripts
modbus read 192.168.1.10 -a 40001 -c 10 --json

# Watch 6 registers at 1s interval, show changes only
modbus watch 192.168.1.10 -a 40001 -c 6 --interval 1

# Launch embedded test server (3 slaves, port 5021)
modbus simulate

# Interactive REPL shell
modbus tui
```

### Running the Tool

The following examples assume a live Modbus TCP server at `127.0.0.1:502`.
Use `--no-banner` (or `MODBUS_CLI_NO_BANNER=1`) for script/AI-mode output,
sending the banner and all diagnostics to stderr so stdout stays clean.

**Read holding registers (rich table output)**

```bash
modbus read 127.0.0.1
# Output (stdout):
# ┌───────── holding registers ─────────┐
# │ Address │ Value   │ Raw    │ Bar    │
# │─────────┼─────────┼────────┼────────│
# │       0 │ 42      │ 42     │ ━━━━━━ │
# │       1 │ 100     │ 100    │ ━━━━━━ │
# │       2 │ 0       │ 0      │ ─────── │
# └──────────────────────────────────────┘
# 10 register(s) from 127.0.0.1:502

# With JSON
modbus read 127.0.0.1 --json
# {"command":"read","register_type":"holding","transport":"tcp","host":"127.0.0.1","port":502,"unit_id":1,"dtype":"u16","byte_order":"big","word_order":"big","scale":1.0,"address":0,"count":10,"values":[{"address":0,"value":42,"raw_value":42,"registers":[42]},...]}
```

**Write registers (FC16, multiple registers)**

```bash
modbus write 127.0.0.1 -a 40001 123 456
# stdout: (empty -- all diagnostics go to stderr)
# stderr: [write] type=holding address=0 count=3 dtype=u16 unit=1
# stdout (rich):
# ┌───────── done ──────────────────────────────────────────────────┐
# │ Wrote [123, 456] to holding register(s) starting at 0          │
# └─────────────────────────────────────────────────────────────────┘
```

**Write a single float32 value**

```bash
modbus write 127.0.0.1 -a 40001 --dtype f32 3.14
```

**Fill registers with a single value**

```bash
# Fill 20 holding registers starting at 40001 with value 0
modbus fill 127.0.0.1 -a 40001 0 -c 20
# Fill entire holding block (100 regs) with --all
modbus fill 127.0.0.1 -a 40001 0 --all
```

**Save and restore device state**

```bash
# Save all register types to JSON
modbus save 127.0.0.1 snapshot.json
# Restore writable types (holding, coil) from JSON
modbus restore 127.0.0.1 snapshot.json
```

**Scan for live registers**

```bash
modbus scan 127.0.0.1 --start 40001 --end 40099
# Rich table with value bars

# JSON mode
modbus scan 127.0.0.1 -s 40001 -e 40099 --json
```

**Watch a register range with change detection**

```bash
# Human-readable live table (updates in place)
modbus watch 127.0.0.1 -a 40001 -c 6 --interval 0.5

# JSON-lines streaming (one object per poll)
modbus watch 127.0.0.1 -a 40001 -c 6 --interval 0.5 --json

# Log to file + JSON
modbus watch 127.0.0.1 -a 40001 -c 6 --interval 1 --output plant.log --json
```

**Script mode (no banner, clean stdout)**

```bash
modbus --no-banner read 127.0.0.1 -c 5 --json
MODBUS_CLI_NO_BANNER=1 modbus read 127.0.0.1 -c 5
```

**Serial (RTU/ASCII) example**

```bash
# Windows
modbus read COM3 --transport rtu --baudrate 19200 -a 40001 -c 10

# Linux/macOS
modbus read /dev/ttyUSB0 --transport rtu --baudrate 19200 -a 40001 -c 10
```

**TLS example**

```bash
modbus read 10.0.0.5 --transport tls --port 802 --no-verify
# or with client certificates
modbus read 10.0.0.5 --transport tls --cert client.pem --key client.key
```

### Common Connection Options

| Option | Default | Description |
|--------|---------|-------------|
| `host` | n/a | Host/IP for tcp/udp/tls; serial device (`COM3`, `/dev/ttyUSB0`) for rtu/ascii |
| `-T`, `--transport` | `tcp` | One of `tcp`, `udp`, `rtu`, `ascii`, `tls` |
| `-p`, `--port` | `502` | Port for tcp/udp/tls. TLS defaults to `802` when port is left at 502 |
| `--slave` | `1` | Modbus slave/unit ID |
| `--timeout` | `3.0` | Connection and response timeout in seconds |
| `--retries` | `0` | Extra connection attempts |
| `-v`, `--verbose` | n/a | Verbose connect logging to stderr |
| `--json` | n/a | Machine-readable JSON output |

Serial options (rtu/ascii only): `--baudrate 9600`, `--parity none|even|odd`,
`--stopbits 1|2`, `--bytesize 5-8`.

TLS options (tls only): `--cert FILE`, `--key FILE`, `--no-verify`.

### Datatype Options (read, write, fill, scan, watch)

| Option | Default | Description |
|--------|---------|-------------|
| `--dtype`, `-d` | `u16` | One of `u16`, `i16`, `u32`, `i32`, `u64`, `i64`, `f32`, `f64`, `str` |
| `--format`, `-f` | `dec` | Display format: `dec`, `hex`, `bin` |
| `--byte-order` | `big` | Byte order within a register |
| `--word-order` | `big` | Word order for multi-register types |
| `--encoding` | `utf-8` | String encoding for dtype `str` |
| `--scale` | `1.0` | Read value *= scale; write raw = value / scale |
| `--type`, `-t` | auto | Register type override: `holding`, `input`, `coil`, `discrete` |

### Standard Modbus Address Notation

The `--address`/`-a` argument (and positional address in TUI) accepts
standard Modbus notation for auto-detecting the register type:

| Notation | Register Type | Raw Address |
|----------|---------------|-------------|
| `40001-49999` | holding | `address - 40001` |
| `30001-39999` | input | `address - 30001` |
| `10001-19999` | discrete | `address - 10001` |
| `1-9999` | coil | `address - 1` |
| `0-65535` or `0x...` | holding (raw) | as given |

Override with `--type`/`-t` if needed.

### read

Read registers, coils, and discrete inputs.

```
modbus read <host> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type`, `-t` | auto | One of `holding`, `input`, `coil`, `discrete` |
| `--count`, `-c` | `1` | Number of values to read |

### write

Write registers (FC6 single, FC16 multiple) and coils (FC5 single, FC15
multiple).

```
modbus write <host> [options] <value...>
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type`, `-t` | auto | One of `holding`, `coil` |
| `values` | n/a | Value(s) to write. One value performs a single write |

Coil values accept `0/1/true/false/on/off/yes/no`.

### fill

Fill registers or coils with a single value.

```
modbus fill <host> [options] <value>
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type`, `-t` | auto | One of `holding`, `coil` |
| `--count`, `-c` | `1` | Number of values to write |
| `--all` | n/a | Fill entire block (100 regs holding, 32 coils) |

### save

Save slave register state to a JSON file.

```
modbus save <host> <path> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type`, `-t` | all | Register type to save: `holding`, `input`, `coil`, `discrete` (default: all) |

### restore

Restore slave register state from a JSON file (writable types only).

```
modbus restore <host> <path> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type`, `-t` | all writable | Register type to restore: `holding`, `coil` |

### scan

Sweep an address range and report non-zero values.

```
modbus scan <host> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type`, `-t` | `holding` | One of `holding`, `input` |
| `--start`, `-s` | `0` | Start address (inclusive, decimal or `0x` hex) |
| `--end`, `-e` | `99` | End address (inclusive, decimal or `0x` hex) |
| `--all` | n/a | Show all values including zeros |

### watch

Poll registers at an interval and show changes (Ctrl-C to quit).

```
modbus watch <host> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type`, `-t` | auto | One of `holding`, `input`, `coil`, `discrete` |
| `--count`, `-c` | `1` | Number of values to monitor |
| `--interval`, `-i` | `1.0` | Polling interval in seconds |
| `--iterations` | `0` | Stop after N polls; 0 = run until Ctrl-C |
| `--all` | n/a | Show every value every poll, not only changes |
| `--json` | n/a | Stream one JSON object per poll on stdout |
| `--output` | n/a | Append each poll's output to this file |

The first poll establishes a baseline; subsequent polls print only values
that changed. With `--json`, each poll is emitted as one JSON object on its
own line.

### simulate

Launch the embedded Modbus TCP/UDP test server with drifting sensor values.

```
modbus simulate [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address(es), comma-separated |
| `--port`, `-p` | `5021` | TCP/UDP port |
| `--slaves` | `3` | Number of slave units |
| `--start-unit` | `1` | Starting unit ID |
| `--transports` | `tcp` | Comma-separated: `tcp`, `udp` |
| `--log-connections` | n/a | Optional file to log connect/disconnect events |
| `--log-level` | `WARNING` | pymodbus logging level |

The simulator provides realistic drifting values on all slaves:
temperature, pressure, flow rate, RPM, voltage, battery (draining),
signal strength, status, runtime hours, error count, totalizers, etc.

### tui

Interactive console shell (REPL) with multiple connections.

```
modbus tui [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `host` | `127.0.0.1` | Initial target (change later with `connect`) |

**Shell commands:**

```
connect <name> host [port]    create/switch connection
use <name>                    switch active connection
list                          show all connections
disconnect [name|host:port]   close a connection
slave <id>                    set default slave for active connection
status                        show active connection state
set <key> <value>             set a session/connection option
read  [type] <addr> [n]       read registers/coils
write [type] <addr> v...      write registers/coils
fill  [type] <addr> <val> [n] write a value (--all for entire block)
scan  [start [end]]           scan range for non-zeros
watch [type] <addr> [n]       live dashboard until Ctrl-C
save  <path>                  save register state to JSON
restore <path>                restore register state from JSON
ls [path]                     list directory contents
clear | cls                   clear terminal screen
pwd                           print working directory
cd [dir]                      change directory
!<command>                    run shell command
help                          show this text
exit | quit | bye             leave shell (Ctrl-D works too)
```

Per-command flags override session/connection options for one command:
```
read 0 4 --dtype f32 --format hex --slave 2 --connection backup
watch 0 4 --interval 0.5 --iterations 20
scan 0 50 --all
```

Global session options (apply to all connections): `dtype`, `format`,
`byte_order`, `word_order`, `encoding`, `scale`, `regtype`.

Per-connection options: `host`, `port`, `transport`, `timeout`, `retries`,
`slave`, `baudrate`, `parity`, `stopbits`, `bytesize`, `cert`, `key`,
`no_verify`.

Session persists to `~/.modbus_session.json` on exit.

## JSON Output

With `--json`, the tool prints structured JSON to stdout. All diagnostics,
banners, and rich panels go to stderr. Use `--no-banner` or
`MODBUS_CLI_NO_BANNER=1` to suppress the banner entirely.

### read and scan

```json
{
  "command": "read",
  "register_type": "holding",
  "transport": "tcp",
  "host": "192.168.1.10",
  "port": 502,
  "unit_id": 1,
  "dtype": "u16",
  "byte_order": "big",
  "word_order": "big",
  "scale": 1.0,
  "address": 0,
  "count": 2,
  "values": [
    {
      "address": 0,
      "value": 1234,
      "raw_value": 1234,
      "registers": [1234]
    }
  ]
}
```

`command` is `read` or `scan`. `scan` returns only non-zero values unless
`--all` is given. Coil and discrete reads return `{"address": n, "value":
true|false}` without register fields.

### write

```json
{
  "command": "write",
  "register_type": "holding",
  "transport": "tcp",
  "host": "192.168.1.10",
  "port": 502,
  "unit_id": 1,
  "dtype": "u16",
  "byte_order": "big",
  "word_order": "big",
  "values": [
    {"address": 100, "value": 123, "registers": [123]}
  ]
}
```

### watch

Each poll is one JSON object per line. The first poll is the baseline and
reports every value; later polls report only values that changed.

```json
{
  "command": "watch",
  "register_type": "holding",
  "transport": "tcp",
  "host": "192.168.1.10",
  "port": 502,
  "unit_id": 1,
  "dtype": "u16",
  "byte_order": "big",
  "word_order": "big",
  "scale": 1.0,
  "poll": 1,
  "ts": 1755000000.123,
  "changes": [
    {"address": 0, "value": 42, "previous": null, "raw_value": 42, "registers": [42]}
  ]
}
```

`previous` is `null` on the baseline poll. Values that did not change are
omitted from `changes`; with `--all` they appear with `previous: null`.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Unexpected error |
| 2 | Usage error, including protocol limit violations |
| 3 | Connection failure |
| 4 | Modbus exception response from the device |
| 5 | I/O or timeout error |

## Protocol Limits

The tool checks these limits before sending any request:

| Function | Operation | Limit |
|----------|-----------|-------|
| FC1 / FC2 | Read coils / discrete inputs | 2000 bits |
| FC3 / FC4 | Read holding / input registers | 125 registers |
| FC5 | Write single coil | 1 coil |
| FC6 | Write single register | 1 register |
| FC15 | Write multiple coils | 1968 coils |
| FC16 | Write multiple registers | 123 registers |

## Development

```bash
git clone https://github.com/DarkSoulEngineer/modbus.git
cd modbus
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[tui]'            # installs with TUI extra
```

The package is organized under `src/modbus/`:

- `core.py` -- datatype engine, client factory, connection & validation
- `cli.py` -- argparse CLI + rich output wiring
- `simulator.py` -- embedded test server with drifting values
- `tui.py` -- interactive console shell (REPL)
- `theme/` -- colorize / rendering helpers (palette, banner, widgets)

Run the test suite (pytest, tests to be added):

```bash
pytest -v
```

Before submitting a change, make sure:
- New behavior has a corresponding test.
- The full suite passes.
- `modbus --help` and `modbus --version` still work.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and
[CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT License. Free to use, modify, and distribute, including in commercial
projects. See [LICENSE](LICENSE) for the full text.

Copyright (c) 2026 DarkSoulEngineer
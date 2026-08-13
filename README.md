# Modbus CLI

```
███╗   ███╗   ██████╗  ██████╗  ██████╗  ██╗   ██╗  ███████╗
████╗ ████║  ██╔═══██╗ ██╔  ██╗ ██╔══██╗ ██║   ██║  ██╔════╝
██╔████╔██║  ██║   ██║ ██║  ██║ ██████╔╝ ██║   ██║  ███████╗
██║╚██╔╝██║  ██║   ██║ ██║  ██║ ██╔══██╗ ██║   ██║  ╚════██║
██║ ╚═╝ ██║  ╚██████╔╝ ██████╔╝ ██████╔╝ ╚██████╔╝  ███████║
╚═╝     ╚═╝   ╚═════╝  ╚═════╝   ╚═════╝   ╚═════╝   ╚══════╝
```

Industrial Modbus diagnostics and automation from the command line.

[![License](https://img.shields.io/github/license/DarkSoulEngineer/modbus)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![pymodbus 3.5+](https://img.shields.io/badge/pymodbus-3.5+-green.svg)](https://pymodbus.readthedocs.io/)
[![CI](https://github.com/DarkSoulEngineer/modbus/actions/workflows/ci.yml/badge.svg)](https://github.com/DarkSoulEngineer/modbus/actions/workflows/ci.yml)

A command-line client for Modbus devices such as PLCs, VFDs, energy meters,
and RTUs. It reads and writes registers, coils, and discrete inputs over TCP,
UDP, RTU, ASCII, or TLS, and can scan a range of registers or watch them for
changes over time.

Output is either a readable table for interactive use or structured JSON for
scripts and pipelines. All progress, warnings, and errors go to stderr, so
stdout stays clean and can be piped directly into other tools.

## Features

- Five transports: TCP, UDP, RTU, ASCII (serial), and TLS with optional client
  certificates and a `--no-verify` mode.
- Four commands: `read`, `write`, `scan`, and `watch`, covering holding and
  input registers, coils, and discrete inputs.
- Datatype conversion for `u16`, `i16`, `u32`, `i32`, `u64`, `i64`, `f32`,
  `f64`, and `str`, with configurable byte order, word order, and a `--scale`
  factor for engineering units.
- `--json` mode emits structured JSON objects suitable for scripts.
- Watch mode compares each poll against the previous one and reports only
  values that changed, with configurable interval and iteration count.
- Scan mode sweeps a register range and reports non-zero values, or every
  value with `--all`.
- Modbus protocol limits are enforced before any request is sent. Counts
  above the allowed maximum (2000 bits for FC1/FC2, 125 registers for
  FC3/FC4, 123 registers for FC16) are rejected with a usage error.
- Deterministic exit codes (0 through 5) and connection retries make the tool
  usable from unattended scripts and cron jobs.
- A colored startup banner is printed to stderr. Suppress it with
  `--no-banner` or the `MODBUS_CLI_NO_BANNER=1` environment variable, for
  example in CI logs.

## Installation

Requires Python 3.10 or newer and pymodbus 3.5 or newer.

```bash
# Install from GitHub; this provides the `modbus` command
pip install git+https://github.com/DarkSoulEngineer/modbus.git

# Or clone and run without installing
git clone https://github.com/DarkSoulEngineer/modbus.git
cd modbus
pip install -r requirements.txt
python modbus.py --help

# Or install locally for development
pip install -e .
```

After `pip install`, invoke the tool with `modbus`. From a checkout, use
`python modbus.py`. The examples below use `modbus`; substitute
`python modbus.py` when running from a checkout.

## Quick start

```bash
# Read 10 holding registers (u16) from a PLC
modbus read 192.168.1.10

# Read 4 float32 values at address 200
modbus read 192.168.1.10 -a 200 --dtype f32 -c 4

# Write two registers (FC16)
modbus write 192.168.1.10 -a 100 123 456

# Machine-readable output for scripts
modbus read 192.168.1.10 -a 0 -c 10 --json
```

### Running the tool

The following examples assume a live Modbus TCP server at `127.0.0.1:502`. The tool uses `--no-banner` (or `MODBUS_CLI_NO_BANNER=1`) for script-/AI‑mode output, sending the banner and all diagnostics to stderr so stdout stays clean.

**Read holding registers (human‑readable mode)**

```bash
modbus read 127.0.0.1
# Output (stdout):
# Read 10 register(s) from 127.0.0.1:502, unit 1
# Register 0: 42
# Register 1: 100
# Register 2: 0
# Register 3: 0
# Register 4: 0

# With JSON
modbus read 127.0.0.1 --json
# Output (stdout, one JSON object):
# {
#   "command": "read",
#   "register_type": "holding",
#   "transport": "tcp",
#   "host": "127.0.0.1",
#   "port": 502,
#   "unit_id": 1,
#   "dtype": "u16",
#   "byte_order": "big",
#   "word_order": "big",
#   "count": 10,
#   "values": [
#     {"address": 0, "value": 42, "raw_value": 42, "registers": [42]},
#     {"address": 1, "value": 100, "raw_value": 100, "registers": [100]},
#     {"address": 2, "value": 0, "raw_value": 0, "registers": [0]},
#     {"address": 3, "value": 0, "raw_value": 0, "registers": [0]},
#     {"address": 4, "value": 0, "raw_value": 0, "registers": [0]},
#     ...
#   ]
# }
```

**Write registers (FC16, multiple registers)**

```bash
modbus write 127.0.0.1 -a 0 123 456
# stdout: (empty — all diagnostics go to stderr)
# stderr: [write] type=holding address=0 count=3 dtype=u16 unit=1
```

**Write a single float32 value**

```bash
modbus write 127.0.0.1 -a 0 --dtype f32 3.14
# stdout: (empty)
# stderr: [write] type=holding address=0 count=2 dtype=f32 unit=1
```

**Scan for live registers**

```bash
modbus scan 127.0.0.1 -s 0 -e 99
# Human‑readable mode
# Scan 0–99: 42 at 0, 7 at 5, 15 at 12

# JSON mode
modbus scan 127.0.0.1 -s 0 -e 99 --json
# [
#   {"address": 0, "value": 42, "raw_value": 42, "registers": [42]},
#   {"address": 5, "value": 7, "raw_value": 7, "registers": [7]},
#   {"address": 12, "value": 15, "raw_value": 15, "registers": [15]}
# ]
```

**Watch a register with change detection**

```bash
modbus watch 127.0.0.1 -c 1 --interval 0.1 --json
# JSON‑lines output (one object per poll):
# {"command":"watch","register_type":"holding","transport":"tcp","host":"127.0.0.1","port":502,"unit_id":1,"dtype":"u16","byte_order":"big","word_order":"big","poll":1,"ts":1755000000.123,"changes":[{"address":0,"value":42,"previous":null,"raw_value":42,"registers":[42]}]}
# Second poll (no change):
# {"command":"watch","register_type":"holding","transport":"tcp","host":"127.0.0.1","port":502,"unit_id":1,"dtype":"u16","byte_order":"big","word_order":"big","poll":2,"ts":1755000000.245,"changes":[]}
```

**Script mode (no banner, clean stdout)**

```bash
modbus --no-banner read 127.0.0.1 -c 5 --json
# stdout: the JSON object shown above
# stderr: (banner and verbose diagnostics suppressed)
```

**Serial (RTU/ASCII) example**

```bash
modbus read COM3 --transport rtu --baudrate 19200 -a 0 -c 10
# Windows:   .\modbus_cli COM3 --transport rtu --baudrate 19200 -a 0 -c 10
# Linux/macOS: modbus read /dev/ttyUSB0 --transport rtu --baudrate 19200 -a 0 -c 10
```

**TLS example**

```bash
modbus read 10.0.0.5 --transport tls --port 802 --no-verify
# or with client certificates
modbus read 10.0.0.5 --transport tls --cert client.pem --key client.key
```

### Common connection options

| Option | Default | Description |
|--------|---------|-------------|
| `ip` | n/a | Host or IP for tcp/udp/tls; serial device (`COM3`, `/dev/ttyUSB0`) for rtu/ascii |
| `--transport` | `tcp` | One of `tcp`, `udp`, `rtu`, `ascii`, `tls` |
| `--port` | `502` | Port for tcp/udp/tls. TLS defaults to `802` when the port is left at 502 |
| `--unit-id`, `--unit` | `1` | Modbus unit or device ID |
| `--timeout` | `3.0` | Connection and response timeout in seconds |
| `--retries` | `0` | Extra connection attempts |
| `-v`, `--verbose` | n/a | Verbose diagnostics to stderr; repeat for more detail |
| `--json` | n/a | Machine-readable JSON output |

Serial options (rtu/ascii only): `--baudrate 9600`, `--parity none|even|odd`,
`--stopbits 1|2`, `--bytesize 5-8`.

TLS options (tls only): `--cert FILE`, `--key FILE`, `--no-verify`.

### Datatype options (read, write, scan, watch)

| Option | Default | Description |
|--------|---------|-------------|
| `--dtype` | `u16` | One of `u16`, `i16`, `u32`, `i32`, `u64`, `i64`, `f32`, `f64`, `str` |
| `--byte-order` | `big` | Byte order within a register |
| `--word-order` | `big` | Word order for multi-register types |
| `--encoding` | `utf-8` | String encoding |
| `--scale` | `1.0` | Read value multiplied by scale; write raw value divided by scale |
| `--format` | `dec` | Human output format: `dec`, `hex`, `bin` |
| `-a`, `--address` | `0` | Starting address, decimal or `0x` hex |

### read

Reads registers, coils, and discrete inputs.

```
modbus read <host> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--register-type` | `holding` | One of `holding`, `input`, `coil`, `discrete` |
| `-c`, `--count` | `10` | Number of values to read |

### write

Writes registers (FC6 single, FC16 multiple) and coils (FC5 single, FC15
multiple).

```
modbus write <host> [options] <value...>
```

| Option | Default | Description |
|--------|---------|-------------|
| `--register-type` | `holding` | One of `holding`, `coil` |
| `values` | n/a | Value(s) to write. One value performs a single write |

Coil values accept `0/1/true/false/on/off/yes/no`.

### scan

Sweeps a register range and reports non-zero values.

```
modbus scan <host> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--register-type` | `holding` | One of `holding`, `input` |
| `-s`, `--start` | `0` | Start address, inclusive |
| `-e`, `--end` | `99` | End address, inclusive |
| `--all` | n/a | Show all values, including zeros |

### watch

Polls a range repeatedly and prints only the values that changed between
polls.

```
modbus watch <host> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-c`, `--count` | `10` | Number of values to monitor |
| `--interval` | `1.0` | Polling interval in seconds |
| `--iterations` | `0` | Stop after N polls; 0 runs until Ctrl-C |
| `--output FILE` | n/a | Append every poll to a file |
| `--all` | n/a | Show every value every poll, not only changes |

The first poll establishes a baseline; subsequent polls print only values
that changed. With `--json`, each poll is emitted as one JSON object on its
own line.

## Examples

```bash
# TCP: holding registers
modbus read 192.168.1.10                        # 10 holding registers (u16)
modbus read 192.168.1.10 -a 100 -c 5            # 5 registers at address 100
modbus read 192.168.1.10 -a 0x100 -c 5          # hex addresses work too
modbus read 192.168.1.10 --dtype f32 -c 4       # 4 float32 values (8 registers)
modbus read 192.168.1.10 --dtype u32 --word-order little -c 2
modbus read 192.168.1.10 --register-type coil -c 16
modbus read 192.168.1.10 --register-type input -c 20 --json
modbus read 192.168.1.10 --dtype str -a 0 -c 16 # string registers

# Writes
modbus write 192.168.1.10 -a 40001 123 456      # two u16 registers (FC16)
modbus write 192.168.1.10 -a 100 0x7B           # hex value
modbus write 192.168.1.10 -a 100 --dtype i16 -5 # negative signed value
modbus write 192.168.1.10 -a 200 --dtype f32 3.14
modbus write 192.168.1.10 --register-type coil -a 0 on off on
modbus write 192.168.1.10 --dtype str -a 300 "Hello"

# Scan for live registers
modbus scan 192.168.1.10 -s 0 -e 999            # find non-zero registers
modbus scan 192.168.1.10 -s 0 -e 99 --dtype f32 --json
modbus scan 192.168.1.10 -s 0 -e 99 --all       # show zeros too

# Watch a live range
modbus watch 192.168.1.10 -c 10 --interval 1       # poll every second
modbus watch 192.168.1.10 -c 5 --json --iterations 60
modbus watch 192.168.1.10 -c 10 --output plant.log # log polls to a file

# Serial (RTU/ASCII)
modbus read COM3 --transport rtu --baudrate 19200 -a 0 -c 10
modbus read /dev/ttyUSB0 --transport rtu --parity even -a 0 -c 10
modbus read /dev/ttyS0 --transport ascii --baudrate 9600 -a 0 -c 10

# TLS
modbus read 10.0.0.5 --transport tls --port 802 --no-verify
modbus read 10.0.0.5 --transport tls --cert client.pem --key client.key

# Script use: no startup banner, clean JSON on stdout
modbus --no-banner read 192.168.1.10 -c 10 --json
MODBUS_CLI_NO_BANNER=1 modbus read 192.168.1.10 -c 10
```

## JSON output

With `--json`, the tool prints a single structured object to stdout and
nothing else. The banner and all diagnostics go to stderr, so the output can
be parsed directly. Use `--no-banner` or `MODBUS_CLI_NO_BANNER=1` to
suppress the banner entirely.

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
  "poll": 1,
  "ts": 1755000000.123,
  "changes": [
    {"address": 0, "value": 42, "previous": null, "raw_value": 42, "registers": [42]}
  ]
}
```

`previous` is `null` on the baseline poll. Values that did not change are
omitted from `changes`; with `--all` they appear with `previous: null`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Unexpected error |
| 2 | Usage error, including protocol limit violations |
| 3 | Connection failure |
| 4 | Modbus exception response from the device |
| 5 | I/O or timeout error |

## Protocol limits

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
pip install -r requirements.txt

# Run the integration test suite (boots a live in-process Modbus TCP server)
python -m unittest discover -s tests -v
```

The tool is a single file, `modbus.py`, organized top-down: datatype engine,
argument parser, client factory, connection handling, command handlers, and
output formatting. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and
[CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT License. Free to use, modify, and distribute, including in commercial
projects. See [LICENSE](LICENSE) for the full text.

Copyright (c) 2026 DarkSoulEngineer

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-08-14

### Added

- **Package restructure**: Refactored from a single-file script (`modbus.py`) to a
  proper Python package under `src/modbus/` with modular design.
- **Rich terminal UI**: All output now uses `rich` for beautiful tables, panels,
  progress spinners, value-bar visualizations, and colored output.
- **Standard Modbus address notation**: Auto-detects register type from address:
  `40001-49999` (holding), `30001-39999` (input), `10001-19999` (discrete),
  `1-9999` (coil). Raw 0-based offsets still work. Override with `--type`/`-t`.
- **New commands**:
  - `fill` -- fill registers or coils with a single value (supports `--all` for
    entire block: 100 holding regs, 32 coils).
  - `save` -- snapshot slave register state to JSON file (all types or specific).
  - `restore` -- restore writable types (holding, coil) from JSON snapshot.
  - `simulate` -- embedded Modbus TCP/UDP test server with multi-slave support
    and drifting sensor values (temperature, pressure, flow, RPM, voltage,
    battery, totalizers, etc.).
  - `tui` -- interactive console shell (REPL) with multiple named connections,
    session persistence (`~/.modbus_session.json`), per-command flag overrides,
    and shell commands (`ls`, `cd`, `pwd`, `clear`, `!cmd`).
- **Enhanced existing commands**:
  - `read` -- rich table output with value bars, connection header panel.
  - `write` -- success panel with colored output.
  - `scan` -- rich table with value bars, success/warning panels.
  - `watch` -- live-updating rich table dashboard (default) plus JSON-lines
    streaming mode (`--json`), file logging (`--output`), change highlighting.
- **Theme system**: New `theme/` package with industrial color palette,
  customizable rich theme, banner with gradient rows, and reusable widgets
  (connection header, error/success panels, value bars).
- **Launcher script**: Root-level `modbus` executable that inserts `src/` onto
  `sys.path` for zero-install usage (`./modbus`).
- **Optional TUI extra**: `pip install -e '.[tui]'` installs `textual>=0.50`
  for enhanced terminal interface (placeholder for future textual-based TUI).
- **pytest configuration**: Added `[tool.pytest.ini_options]` with `pythonpath =
  ["src"]` and `testpaths = ["tests"]`.

### Changed

- **Package name**: `modbus-cli` -> `modbus` (on PyPI/GitHub).
- **Version**: 1.0.0 -> 1.1.0.
- **Entry point**: `modbus = "modbus:main"` -> `modbus = "modbus.cli:main"`.
- **Build system**: `py-modules = ["modbus"]` -> `packages.find` with
  `where = ["src"]`, `include = ["modbus*"]`.
- **Dependencies**: Added `rich>=13.0` as required dependency. `pymodbus`
  relaxed to `>=3.5` (no upper bound).
- **CLI flags**:
  - `--unit-id` / `--unit` -> `--slave` (short: none).
  - `-u` removed (was short for `--unit-id`).
  - `-T` added as short for `--transport`.
  - `-p` now short for `--port` (was not available before).
  - `-t` / `--type` added for register type override on read/write/scan/watch.
  - Positional `host` argument renamed to `host` (was `ip` in help).
  - `--address` / `-a` now accepts standard Modbus notation (40001, 30001, etc.).
- **Default count**: `read` default count changed from 10 to 1 (explicit is better).
- **Development status**: Classifier changed from "Production/Stable" (5) to
  "Beta" (4) due to major refactor.
- **Keywords**: Added `rich`; reordered.
- **Homepage URL**: Updated in pyproject.toml (note: points to 19bk/modbus-cli,
  repo is DarkSoulEngineer/modbus).

### Removed

- Single-file `modbus.py` (replaced by `src/modbus/` package).
- `.github/workflows/ci.yml` (CI workflow removed; to be re-added).
- `tests/test_cli.py` (integration test suite removed during refactor; to be
  rewritten for new modular architecture).
- `--retries` connection retry logic unchanged but now uses rich spinner.
- `--verbose` no longer repeatable (was `-vvv` for more detail).

### Fixed

- Value bar visualization now correctly handles binary/flag registers (0/1)
  by treating them as a 0..1 range (full/empty bar).
- Float types (`f32`, `f64`) now properly reject `hex`/`bin` format with clear
  error message.
- Connection retries now show spinner and attempt count in verbose mode.
- Exit codes preserved exactly (0-5) for script compatibility.

### Security

- TLS `--no-verify` flag explicitly documented as insecure (skips certificate
  and hostname verification). Use only in trusted environments.

---

## [1.0.0] - 2026-08-13

### Added

- Initial public release.
- **Transports**: Modbus TCP, UDP, RTU, ASCII, and TLS (with optional client
  certificate support and `--no-verify`).
- **Commands**: `read`, `write`, `scan`, and `watch` covering holding/input
  registers, coils, and discrete inputs.
- **Datatype engine**: `u16`, `i16`, `u32`, `i32`, `u64`, `i64`, `f32`, `f64`,
  and `str` register/value conversions with configurable byte order and word
  order plus a `--scale` factor.
- **Output modes**: human-readable tables and value lines, or machine-readable
  JSON (`--json`) with clean stdout (all diagnostics on stderr).
- **Watch command**: change-detection diffing between polls, configurable
  interval/iterations, and optional file logging via `--output`.
- **Scan command**: range scanning with non-zero filtering and `--all` mode.
- **Protocol limits**: FC1/FC2 (2000 bits), FC3/FC4 (125 registers),
  FC6 (1 register), FC15 (1968 coils), FC16 (123 registers) are enforced
  up front with clear usage errors.
- **Robustness**: connection retries with backoff, clear exit codes
  (0-5), and verbose/traceback diagnostics via `-v`.
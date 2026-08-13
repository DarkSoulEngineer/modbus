# Contributing to Modbus CLI

First off, thanks for taking the time to contribute! This is a free, open
source tool: improvements, bug reports, and documentation fixes are all
welcome.

## Table of contents

- [Code of conduct](#code-of-conduct)
- [How to report a bug](#how-to-report-a-bug)
- [How to request a feature](#how-to-request-a-feature)
- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Submitting a pull request](#submitting-a-pull-request)
- [Code style](#code-style)

## Code of conduct

Be respectful and constructive. Harassment, trolling, and personal attacks
are not tolerated. Keep discussions focused on the technical work.

## How to report a bug

Open an issue on GitHub with the following information:

1. **Expected behavior**: what you expected to happen.
2. **Actual behavior**: what actually happened (paste the exact output).
3. **Reproduction**: the exact command line you ran, including the transport,
   device, and options. Sanitize any device credentials.
4. **Environment**: OS, Python version (`python --version`), pymodbus
   version (`pip show pymodbus`), and rich version (`pip show rich`).

## How to request a feature

Open an issue describing the use case. Issues are more useful than
implementations you assume we want: explain the *problem*, not just the
proposed solution.

## Development setup

```bash
git clone https://github.com/DarkSoulEngineer/modbus.git
cd modbus

# create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# install in development mode with all extras
pip install -e '.[tui]'
```

This installs the package with:
- `modbus` console script (main CLI)
- `modbus.simulate` module entry point
- `modbus.tui` module entry point
- `textual` for the TUI extra
- `pytest` for testing

Verify the installation:

```bash
modbus --help
modbus --version
modbus simulate --help
modbus tui --help
```

## Project layout

The codebase is a Python package under `src/modbus/` with a modular design:

```
src/modbus/
├── __init__.py       # package metadata, version
├── __main__.py       # enables `python -m modbus`
├── core.py           # datatype engine, client factory, connection & validation
├── cli.py            # argparse CLI + rich output wiring (main entry)
├── simulator.py      # embedded Modbus TCP/UDP test server with drifting values
├── tui.py            # interactive console shell (REPL)
└── theme/            # colorize / rendering helpers
    ├── __init__.py
    ├── palette.py    # rich Console, color palette, custom theme
    ├── banner.py     # ASCII banner with rich Panel
    └── widgets.py    # connection_header, error_panel, success_panel, value_bar
```

Module responsibilities:

1. **core.py** -- pure protocol logic, no presentation code. Exports the datatype
   engine (register <-> value conversions), client factory (TCP/UDP/RTU/ASCII/TLS
   via pymodbus), connection retry/backoff, and all argument validation
   (register-count/address-space limits per Modbus function code, dtype/range
   checks).
2. **cli.py** -- argparse command parsing, delegates to core for protocol logic,
   uses theme for rich rendering. Entry point: `main()` (console script `modbus`).
3. **theme/** -- all rendering: color palette, console, banner, panels, tables,
   value bars. Swap this package to re-theme.
4. **simulator.py** -- pymodbus 3.x SimDevice/SimData simulator with multi-slave
   support and drifting sensor values. Runs TCP and/or UDP.
5. **tui.py** -- interactive REPL with multiple named connections, session
   persistence (`~/.modbus_session.json`), per-command flag overrides, shell
   commands.

Root-level files:

- `modbus` -- launcher script that inserts `src/` onto `sys.path` and delegates
  to `modbus.cli:main`. Allows `./modbus` without installation.
- `pyproject.toml` -- package metadata, dependencies, build config, pytest config.
- `requirements.txt` -- minimal deps for CI (pymodbus only; rich is in pyproject).
- `tests/` -- pytest test suite (to be populated).

## Testing

The project uses **pytest** (configured in `pyproject.toml` with `pythonpath =
["src"]` and `testpaths = ["tests"]`).

Run the test suite:

```bash
pytest -v
```

Or run a specific test file:

```bash
pytest tests/test_core.py -v
```

### Writing tests

- Place tests in `tests/` mirroring the module structure (e.g. `tests/test_core.py`).
- Use fixtures in `tests/conftest.py` for shared setup (e.g. a test Modbus server).
- Test the public API of each module:
  - `core.py`: datatype engine functions, client factory, validation helpers.
  - `cli.py`: command functions (mock the client).
  - `simulator.py`: device creation, drift action, server setup.
  - `tui.py`: session management, command dispatch, flag parsing.
- For integration tests, boot a real in-process Modbus TCP server (pymodbus
  `ModbusTcpServer` + `SimDevice`) and drive the CLI as a subprocess or via
  the module API.

Before submitting a change, make sure:

- New behavior has a corresponding test.
- The full suite passes (`pytest -v`).
- `modbus --help` and `modbus --version` still work.
- Type checking passes (if adding type hints): `python -m py_compile src/modbus/*.py`

## Submitting a pull request

1. Fork the repository and create a branch from `main`.
2. Make your change with tests.
3. Run the test suite locally (`pytest -v`).
4. Push and open a pull request describing the change and any trade-offs.
5. Keep the diff focused: one logical change per PR.

## Code style

- Target Python 3.10+: no syntax newer than that.
- Type hints are encouraged for new code; existing code uses them sparingly.
- Follow the existing conventions in `src/modbus/` (docstrings, error handling,
  stdout/stderr discipline).
- **All diagnostics, progress, and errors go to stderr** so stdout stays
  clean for machine-readable (`--json`) output. Never print diagnostics to
  stdout.
- Exit codes are part of the public contract (see README): do not change
  their meaning.
- Rich rendering lives in `theme/` -- keep CLI modules free of ANSI codes
  and manual formatting.
- When adding new commands, follow the pattern in `cli.py`: parse args ->
  validate -> connect -> execute -> render -> disconnect.
- Keep functions focused and small; the original single-file design was
  intentionally split to avoid 2000-line files.
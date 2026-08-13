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
4. **Environment**: OS, Python version (`python --version`), and pymodbus
   version (`pip show pymodbus`).

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

# install dependencies
pip install -r requirements.txt
```

## Project layout

The tool is intentionally a single file, `modbus.py`, with a top-down
architecture:

1. Datatype engine: register <-> value conversions (int/float/str)
2. Argument parser: CLI surface for every transport and command
3. Client factory: builds the correct pymodbus client per transport
4. Connection handling: retry/backoff connection logic
5. Command handlers: read / write / scan / watch implementations
6. Output formatting: human tables, value lines, JSON result builders

## Testing

Integration tests live in `tests/` and boot a real in-process Modbus TCP
server, then drive the CLI as a subprocess over the wire:

```bash
python -m unittest discover -s tests -v
```

Before submitting a change, make sure:

- New behavior has a corresponding test.
- The full suite passes.
- `python modbus.py --help` and `python modbus.py --version` still work.

## Submitting a pull request

1. Fork the repository and create a branch from `main`.
2. Make your change with tests.
3. Run the test suite locally.
4. Push and open a pull request describing the change and any trade-offs.
5. Keep the diff focused: one logical change per PR.

## Code style

- Target Python 3.10+: no syntax newer than that.
- Follow the existing conventions in `modbus.py` (docstrings, error handling,
  stdout/stderr discipline).
- All diagnostics, progress, and errors go to **stderr** so stdout stays
  clean for machine-readable (`--json`) output. Never print diagnostics to
  stdout.
- Exit codes are part of the public contract (see README): do not change
  their meaning.

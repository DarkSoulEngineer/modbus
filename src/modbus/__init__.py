"""modbus -- MODBUS for dummies.

A rich-terminal Modbus CLI for TCP, UDP, RTU, ASCII, and TLS.  Built on
pymodbus, styled with rich.  This package mirrors the modular design of
https://github.com/19bk/modbus-cli but keeps the broader transport/dtype
support of the original DarkSoulEngineer tool.

Submodules:
    core       : datatype engine, client factory, connection & validation
    theme        : colorize / rendering helpers (swap this package to theme)
    simulator    : embedded Modbus TCP test server with drifting values
    cli          : argparse CLI + rich output wiring
"""

__version__ = "1.1.0"
__author__ = "DarkSoulEngineer"

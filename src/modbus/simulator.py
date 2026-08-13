"""modbus.simulator -- embedded Modbus TCP/UDP test server with drifting values.

Provides a tiny Modbus server (configurable slave IDs) pre-loaded with realistic
IoT-style sensor data whose values drift over time, so ``read``/``watch``/
``scan`` have something live to talk to during development and demos.

Run it in one terminal::

    python -m modbus.simulator [--host 127.0.0.1,0.0.0.0] [--port 5021] [--slaves 3] [--transports tcp,udp]

or from the CLI::

    modbus simulate [--port 5021] [--slaves 3] [--transports tcp,udp]

Implementation note: this uses the modern pymodbus 3.x simulator API
(``SimDevice`` / ``SimData`` / the ``action`` callback) and ``ModbusTcpServer``/
``ModbusUdpServer``. No deprecated datastore classes are used, so there is no
``DeprecationWarning`` "Repeating..." spam and no background ``getValues``/
``setValues`` thread -- drifting values are applied by the ``action`` callback
on each holding-register read, which mutates the device's stored register list
in place (and therefore persists across reads).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Callable

from pymodbus.server import ModbusTcpServer, ModbusUdpServer
from pymodbus.simulator import DataType, SimData, SimDevice

# Sensor-like register map (holding registers, offset 0 == address 40001).
_DRIFT_SPEC = [
    # (index, min, max, step)
    (0, 200, 280, 3),     # temperature (23.7 C * 10)
    (1, 900, 1100, 10),   # pressure (mbar)
    (2, 40, 80, 2),       # flow rate (L/min)
    (3, 800, 1000, 20),   # RPM
    (4, 2200, 2400, 5),   # voltage (231.5 V * 10)
    (5, 3000, 5000, 2),   # battery (mV)
]

# Index of the battery register (only ever drains, never self-recharges).
_BATTERY_INDEX = 5
# Index of the totalizer register (ticks up and wraps modulo 65535).
_TOTALIZER_INDEX = 10

_N_HOLDING = 100
_N_INPUT = 100
_N_COILS = 32
_N_DISCRETE = 32

# Initial holding-register snapshot (registers 0..15); the rest are random per
# slave. Matches the original simulator's _SENSOR_INIT layout.
_SENSOR_INIT = [
    237,    # 40001 temperature
    1024,   # 40002 pressure
    58,     # 40003 flow rate
    900,    # 40004 RPM
    2315,   # 40005 voltage
    4850,   # 40006 battery
    95,     # 40007 signal strength %
    1,      # 40008 device status (1=online)
    3742,   # 40009 total runtime hours
    156,    # 40010 error count
    12045,  # 40011 flow totalizer
    8821,   # 40012 energy kWh
    2200,   # 40013 frequency (Hz * 100)
    485,    # 40014 current (mA)
    32767,  # 40015 max int16
    0,      # 40016 zero
]


async def _drift_action(
    function_code: int,
    start_address: int,
    address: int,
    count: int,
    registers: list[int],
    values: list[int] | list[bool] | None,
) -> int | None:
    """Drift the holding-register sensor values on each read.

    ``registers`` is the device's live stored register list for the block being
    accessed; mutating it in place both serves the current request and persists
    the change so the next read sees accumulated drift (no separate thread
    required).

    Only ``read holding registers`` (function code 3) is drifted -- writes and
    coil/discrete/input access are left untouched.
    """
    if function_code != 3:
        return None

    # Drift the bounded sensors.
    for index, lo, hi, step in _DRIFT_SPEC:
        if 0 <= index < len(registers):
            drifted = max(lo, min(hi, registers[index] + random.randint(-step, step)))
            registers[index] = drifted

    # Battery only drains (slowly), never recharges on its own.
    if 0 <= _BATTERY_INDEX < len(registers):
        registers[_BATTERY_INDEX] = max(3000, registers[_BATTERY_INDEX] - random.randint(0, 2))

    # Totalizer ticks up and wraps.
    if 0 <= _TOTALIZER_INDEX < len(registers):
        registers[_TOTALIZER_INDEX] = (registers[_TOTALIZER_INDEX] + random.randint(0, 3)) % 65535

    return None


def _build_holding_values(rng: random.Random) -> list[int]:
    """Holding registers: the 16 sensor init values padded to 100 random regs."""
    values = list(_SENSOR_INIT)
    values.extend(rng.randint(0, 1000) for _ in range(_N_HOLDING - len(_SENSOR_INIT)))
    return values


def _build_sim_device(slave_id: int) -> SimDevice:
    """Build a single simulated device with its own independent drift action."""
    rng = random.Random(0xC0FFEE + slave_id)

    holding = SimData(
        address=0,
        count=_N_HOLDING,
        values=_build_holding_values(rng),
        datatype=DataType.REGISTERS,
    )
    input_regs = SimData(
        address=0,
        count=_N_INPUT,
        values=[rng.randint(100, 5000) for _ in range(_N_INPUT)],
        datatype=DataType.REGISTERS,
    )
    coils = SimData(
        address=0,
        count=_N_COILS,
        values=[rng.choice([True, False]) for _ in range(_N_COILS)],
        datatype=DataType.BITS,
    )
    discrete = SimData(
        address=0,
        count=_N_DISCRETE,
        values=[rng.choice([True, False]) for _ in range(_N_DISCRETE)],
        datatype=DataType.BITS,
    )

    # Tuple layout: (coils, discrete inputs, holding registers, input registers).
    return SimDevice(
        id=slave_id,
        simdata=([coils], [discrete], [holding], [input_regs]),
        action=_drift_action,
    )


def create_devices(slaves: int = 3, start_unit: int = 1) -> list[SimDevice]:
    """Create the list of simulated devices.

    Args:
        slaves: Number of slave devices to create (default: 3).
        start_unit: Starting unit ID (default: 1). Units will be
            [start_unit, start_unit + slaves - 1].

    Returns:
        List of SimDevice instances, each with independent register state
        and independent drift action operating on its own registers.
    """
    return [_build_sim_device(slave_id=start_unit + i) for i in range(slaves)]


@dataclass
class ConnectionStats:
    """Per-connection observability counters.

    Fields:
        active: Currently active connections.
        total:  Total connections seen (connect events).

    Methods:
        on_connect: Callback suitable for pymodbus ``trace_connect``.
        summary: Human-readable summary string.
    """
    active: int = 0
    total: int = 0
    _log_file: str | None = field(default=None, repr=False)
    _log_fh: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._log_file:
            self._log_fh = open(self._log_file, "a", buffering=1)

    def on_connect(self, connected: bool) -> None:
        """Trace callback: True => connect, False => disconnect."""
        if connected:
            self.active += 1
            self.total += 1
            event = "connect"
        else:
            self.active = max(0, self.active - 1)
            event = "disconnect"
        if self._log_fh:
            print(f"{event} active={self.active} total={self.total}", file=self._log_fh)

    def summary(self) -> str:
        """Return a human-readable summary."""
        return f"active/{self.active} total/{self.total}"

    def close(self) -> None:
        """Close the log file if open."""
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None


def make_servers(
    devices: list[SimDevice],
    hosts: list[str],
    port: int,
    transports: tuple[str, ...],
    stats: ConnectionStats | None = None,
) -> list:
    """Create pymodbus server objects for the given hosts/ports/transports.

    Args:
        devices: List of SimDevice instances (multi-slave context).
        hosts: List of bind addresses (e.g. ["127.0.0.1", "0.0.0.0"]).
        port: TCP/UDP port number.
        transports: Tuple of transport names ("tcp" and/or "udp").
        stats: Optional ConnectionStats for per-connection observability.

    Returns:
        List of server objects (ModbusTcpServer and/or ModbusUdpServer).

    Raises:
        ValueError: If an unsupported transport is requested.
    """
    trace_connect = stats.on_connect if stats else None
    servers = []

    for host in hosts:
        for transport in transports:
            if transport == "tcp":
                servers.append(
                    ModbusTcpServer(
                        context=devices,
                        address=(host, port),
                        trace_connect=trace_connect,
                    )
                )
            elif transport == "udp":
                servers.append(
                    ModbusUdpServer(
                        context=devices,
                        address=(host, port),
                        trace_connect=trace_connect,
                    )
                )
            else:
                raise ValueError(
                    f"Unsupported transport: {transport!r}. "
                    "Only 'tcp' and 'udp' are supported (rtu/ascii/tls need serial/certs)."
                )
    return servers


async def serve(
    hosts: list[str],
    port: int,
    transports: tuple[str, ...],
    devices: list[SimDevice],
    stats: ConnectionStats | None = None,
) -> None:
    """Run servers until cancelled.

    Args:
        hosts: List of bind addresses.
        port: TCP/UDP port number.
        transports: Tuple of transport names ("tcp" and/or "udp").
        devices: List of SimDevice instances.
        stats: Optional ConnectionStats for observability.
    """
    servers = make_servers(devices, hosts, port, transports, stats)
    try:
        await asyncio.gather(*[s.serve_forever() for s in servers])
    except asyncio.CancelledError:
        # shutdown() is a coroutine in pymodbus 3.x -- await it for a clean teardown.
        await asyncio.gather(*(s.shutdown() for s in servers), return_exceptions=True)
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modbus simulate",
        description="Embedded Modbus TCP/UDP test server with drifting values.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address(es), comma-separated (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=5021, help="TCP/UDP port (default: 5021)."
    )
    parser.add_argument(
        "--slaves", type=int, default=3, help="Number of slave units (default: 3)."
    )
    parser.add_argument(
        "--start-unit", type=int, default=1, help="Starting unit ID (default: 1)."
    )
    parser.add_argument(
        "--transports",
        default="tcp",
        help="Comma-separated transports: tcp,udp (default: tcp).",
    )
    parser.add_argument(
        "--log-connections",
        metavar="PATH",
        help="Optional file to append connect/disconnect events.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level for pymodbus (default: WARNING).",
    )
    parser.add_argument(
        "--no-banner", action="store_true", help="Suppress startup banner."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the simulator until interrupted. Returns process exit code."""
    args = _build_parser().parse_args(argv)

    # Configure pymodbus logging if available
    try:
        from pymodbus import logging as pymodbus_logging
        pymodbus_logging.setLevel(getattr(logging, args.log_level))
    except Exception:
        pass  # no-op if pymodbus.logging not available

    # Banner
    show_banner = not args.no_banner and not os.environ.get("MODBUS_NO_BANNER")
    if show_banner:
        print()
        print("  \033[1;36mmodbus simulator\033[0m")
        print("  \033[2m" + "-" * 40 + "\033[0m")
        print()
        unit_ids = list(range(args.start_unit, args.start_unit + args.slaves))
        print(f"  \033[1;32mListening on {', '.join(args.host.split(','))}:{args.port}\033[0m")
        print(f"  Slave IDs: {', '.join(map(str, unit_ids))}")
        print(f"  Holding registers: {_N_HOLDING} (drifting values on all slaves)")
        print(f"  Input registers:   {_N_INPUT}")
        print(f"  Coils:             {_N_COILS}")
        print(f"  Discrete inputs:   {_N_DISCRETE}")
        print(f"  Transports:        {args.transports}")
        print()
        print("  \033[2mCtrl+C to stop\033[0m")
        print()

    # Parse hosts and transports
    hosts = [h.strip() for h in args.host.split(",") if h.strip()]
    transports = tuple(t.strip() for t in args.transports.split(",") if t.strip())

    # Connection stats with optional log file
    stats = ConnectionStats(_log_file=args.log_connections) if args.log_connections else ConnectionStats()

    devices = create_devices(slaves=args.slaves, start_unit=args.start_unit)

    async def _run() -> None:
        await serve(hosts, args.port, transports, devices, stats)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        stats.close()
    return 0


# Preserved entrypoint name used by modbus.cli: ``simulator_main(...)``.
simulator_main = main


if __name__ == "__main__":
    raise SystemExit(main())
"""End-to-end integration tests for the Modbus CLI.

Each test boots a real in-process Modbus TCP server (pymodbus) and drives
the CLI as a subprocess, exercising the full client/server wire path:
argument parsing -> client factory -> connection -> PDU exchange ->
datatype conversion -> output formatting.

The fixture targets the pymodbus 3.12+ async datastore (ModbusDeviceContext /
ModbusServerContext / SimCore). Tests requiring the server are skipped on
older pymodbus releases; the CLI itself still supports pymodbus 3.5+.
"""

import asyncio
import json
import logging
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import pymodbus
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

logging.getLogger("pymodbus").setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parent.parent
CLI = str(ROOT / "modbus.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def f32_words(value: float) -> list:
    """IEEE-754 f32 as two big-endian register words (big word order)."""
    packed = struct.pack(">f", value)
    return [int.from_bytes(packed[0:2], "big"), int.from_bytes(packed[2:4], "big")]


def make_block(size: int = 65535):
    """Full-size register block; address 1 because SimData covers addr-1.."""
    return ModbusSequentialDataBlock(1, [0] * size)


class ModbusTestServer:
    """In-process Modbus TCP server backed by the pymodbus async datastore."""

    def __init__(self) -> None:
        self.port = free_port()
        device = ModbusDeviceContext(
            di=make_block(),
            co=make_block(),
            ir=make_block(),
            hr=make_block(),
        )
        self.context = ModbusServerContext(devices={1: device}, single=False)
        self._loop = None
        self.server = None
        self._thread = threading.Thread(target=self._run_server, daemon=True)

    def _run_server(self) -> None:
        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            self.server = ModbusTcpServer(
                context=self.context, address=("127.0.0.1", self.port)
            )
            await self.server.serve_forever()

        asyncio.run(main())

    def __enter__(self) -> "ModbusTestServer":
        self._thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("test server did not start listening")

    def __exit__(self, *exc) -> None:
        if self.server is not None:
            future = asyncio.run_coroutine_threadsafe(self.server.shutdown(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
        self._thread.join(timeout=5)

    def _runtime(self):
        return self.server.context.devices[1]

    def set_values(self, func_code: int, address: int, values) -> None:
        asyncio.run(self._runtime().async_setValues(func_code, address, list(values)))

    def get_values(self, func_code: int, address: int, count: int) -> list:
        return asyncio.run(self._runtime().async_getValues(func_code, address, count))

    def set_registers(self, address: int, values) -> None:
        self.set_values(3, address, values)

    def set_input_registers(self, address: int, values) -> None:
        self.set_values(4, address, values)

    def get_registers(self, address: int, count: int) -> list:
        return self.get_values(3, address, count)

    def set_coils(self, address: int, bits) -> None:
        self.set_values(1, address, [bool(b) for b in bits])


def run_cli(*args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess with the startup banner suppressed."""
    return subprocess.run(
        [sys.executable, CLI, "--no-banner", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class CliSmokeTests(unittest.TestCase):
    """Static CLI surface: version, help, exit-code contract."""

    def test_version(self) -> None:
        result = run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("modbus_cli 1.0.0", result.stdout)

    def test_help_lists_commands(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for token in ("read", "write", "scan", "watch", "--json", "--transport"):
            self.assertIn(token, result.stdout)

    def test_usage_error_exits_2(self) -> None:
        result = run_cli("read")  # missing positional host
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr.lower())

    def test_connection_refused_exits_3(self) -> None:
        result = run_cli("read", "127.0.0.1", "--port", str(free_port()), "--timeout", "1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Could not connect", result.stderr)


class ServerTestCase(unittest.TestCase):
    """Base class: boots one in-process Modbus TCP server per test."""

    def setUp(self) -> None:
        version = tuple(int(part) for part in pymodbus.__version__.split(".")[:2])
        if version < (3, 12):
            self.skipTest(f"requires pymodbus >= 3.12 (async datastore), have {pymodbus.__version__}")
        self.server = ModbusTestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.host = "127.0.0.1"

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return run_cli(*args, "--port", str(self.server.port))


class ReadTests(ServerTestCase):
    def test_read_holding_u16(self) -> None:
        self.server.set_registers(0, [10, 20, 30, 40, 50])
        result = self.cli("read", self.host, "-a", "0", "-c", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Register 0: 10", result.stdout)
        self.assertIn("Register 4: 50", result.stdout)

    def test_read_hex_address(self) -> None:
        self.server.set_registers(0x100, [7])
        result = self.cli("read", self.host, "-a", "0x100", "-c", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Register 256: 7", result.stdout)

    def test_read_json_structure(self) -> None:
        self.server.set_registers(100, [1234, 5678])
        result = self.cli("read", self.host, "-a", "100", "-c", "2", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["command"], "read")
        self.assertEqual(data["register_type"], "holding")
        self.assertEqual(data["transport"], "tcp")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["values"], [
            {"address": 100, "value": 1234, "raw_value": 1234, "registers": [1234]},
            {"address": 101, "value": 5678, "raw_value": 5678, "registers": [5678]},
        ])

    def test_read_f32(self) -> None:
        self.server.set_registers(50, f32_words(3.14))
        result = self.cli("read", self.host, "-a", "50", "-c", "1", "--dtype", "f32")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(f32): 3.14", result.stdout)

    def test_read_u32_little_word_order(self) -> None:
        # Words [0x0001, 0x0002] with little word order => value 0x00020001.
        self.server.set_registers(200, [0x0001, 0x0002])
        result = self.cli("read", self.host, "-a", "200", "-c", "1",
                          "--dtype", "u32", "--word-order", "little", "--format", "hex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0x20001", result.stdout)

    def test_read_input_registers(self) -> None:
        self.server.set_input_registers(10, [99])
        result = self.cli("read", self.host, "-a", "10", "-c", "1", "--register-type", "input")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Register 10: 99", result.stdout)

    def test_read_coils(self) -> None:
        self.server.set_coils(0, [True, False, True])
        result = self.cli("read", self.host, "--register-type", "coil", "-a", "0", "-c", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Coil 0: ON", result.stdout)
        self.assertIn("Coil 1: OFF", result.stdout)
        self.assertIn("Coil 2: ON", result.stdout)

    def test_read_discrete_inputs_json(self) -> None:
        self.server.set_values(2, 0, [True, False])
        result = self.cli("read", self.host, "--register-type", "discrete", "-a", "0", "-c", "2", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["values"], [
            {"address": 0, "value": True},
            {"address": 1, "value": False},
        ])

    def test_read_register_count_limit(self) -> None:
        result = self.cli("read", self.host, "-c", "126")
        self.assertEqual(result.returncode, 2)
        self.assertIn("125", result.stderr)


class WriteTests(ServerTestCase):
    def test_write_single_register_fc6(self) -> None:
        result = self.cli("write", self.host, "-a", "300", "4242")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.get_registers(300, 1), [4242])

    def test_write_multiple_registers_fc16(self) -> None:
        result = self.cli("write", self.host, "-a", "400", "1", "2", "3", "4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.get_registers(400, 4), [1, 2, 3, 4])

    def test_write_f32(self) -> None:
        result = self.cli("write", self.host, "-a", "0", "--dtype", "f32", "3.14")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.get_registers(0, 2), f32_words(3.14))

    def test_write_hex_value(self) -> None:
        result = self.cli("write", self.host, "-a", "0", "0x10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.get_registers(0, 1), [16])

    def test_write_negative_i16(self) -> None:
        result = self.cli("write", self.host, "-a", "0", "--dtype", "i16", "-5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.get_registers(0, 1), [0xFFFB])

    def test_write_coil(self) -> None:
        result = self.cli("write", self.host, "--register-type", "coil", "-a", "5", "on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.server.get_values(1, 5, 1)[0], (True, 1, 0xFF00))

    def test_write_coils_multi(self) -> None:
        result = self.cli("write", self.host, "--register-type", "coil", "-a", "0", "on", "off", "on")
        self.assertEqual(result.returncode, 0, result.stderr)
        values = self.server.get_values(1, 0, 3)
        self.assertEqual([1 if v else 0 for v in values], [1, 0, 1])

    def test_write_range_error_exits_2(self) -> None:
        result = self.cli("write", self.host, "--dtype", "u16", "70000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("range", result.stderr)

    def test_write_json(self) -> None:
        result = self.cli("write", self.host, "-a", "10", "--json", "42")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["command"], "write")
        self.assertEqual(data["values"], [{"address": 10, "value": 42, "registers": [42]}])


class ScanTests(ServerTestCase):
    def test_scan_finds_non_zero(self) -> None:
        self.server.set_registers(0, [0] * 50)
        self.server.set_registers(10, [7])
        self.server.set_registers(20, [9])
        result = self.cli("scan", self.host, "-s", "0", "-e", "49", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["command"], "scan")
        self.assertEqual([v["value"] for v in data["values"]], [7, 9])

    def test_scan_all_skips_nothing(self) -> None:
        self.server.set_registers(0, [0, 0, 0])
        result = self.cli("scan", self.host, "-s", "0", "-e", "2", "--all", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([v["value"] for v in data["values"]], [0, 0, 0])

    def test_scan_invalid_range_exits_2(self) -> None:
        result = self.cli("scan", self.host, "-s", "50", "-e", "10")
        self.assertEqual(result.returncode, 2)


class WatchTests(ServerTestCase):
    def test_watch_json_streams_two_polls(self) -> None:
        self.server.set_registers(0, [5])
        result = self.cli("watch", self.host, "-c", "1",
                          "--iterations", "2", "--interval", "0.1", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        polls = [json.loads(line) for line in result.stdout.strip().splitlines()]
        self.assertEqual(len(polls), 2)
        self.assertEqual(polls[0]["command"], "watch")
        self.assertEqual(polls[0]["poll"], 1)
        self.assertEqual(polls[0]["changes"][0]["value"], 5)
        # Second poll sees no change -> empty changes list.
        self.assertEqual(polls[1]["poll"], 2)
        self.assertEqual(polls[1]["changes"], [])


if __name__ == "__main__":
    unittest.main()

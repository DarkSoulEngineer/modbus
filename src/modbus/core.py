"""modbus.core -- behaviour anchor for the Modbus CLI.

This module is a faithful port of the original single-file ``modbus``'s
behaviour: the datatype engine (register <-> value conversions), the client
factory (TCP/UDP/RTU/ASCII/TLS via pymodbus), connection retry/back-off, and
all argument validation (register-count / address-space limits per Modbus
function code, dtype/range checks).  It deliberately contains NO presentation
code -- rendering lives in :mod:`modbus.theme` and the argparse wiring in
:mod:`modbus.cli`.

Public API:
    INT_TYPES, FLOAT_TYPES, ALL_DTYPES, VALUE_RANGES
    value_to_registers / registers_to_value / registers_to_string /
    string_to_registers / get_dtype_width / validate_value_range
    parse_int_token / parse_parity
    slave_kwarg(slave_id)
    build_client(args) / connect_with_retries(...)
    parse_modbus_address(address)
    validate_args(args)
    EXIT_SUCCESS/EXIT_ERROR/EXIT_USAGE/EXIT_CONN/EXIT_MBEXCEPTION/EXIT_IO
"""

from __future__ import annotations

import argparse
import ssl
import struct
import sys
import time
from typing import Any, List, Optional, Tuple, Union

from pymodbus import FramerType
from pymodbus.client import (
    ModbusTcpClient,
    ModbusUdpClient,
    ModbusSerialClient,
    ModbusTlsClient,
)
from pymodbus.exceptions import ModbusException, ModbusIOException

__all__ = [
    "EXIT_SUCCESS", "EXIT_ERROR", "EXIT_USAGE", "EXIT_CONN",
    "EXIT_MBEXCEPTION", "EXIT_IO",
    "INT_TYPES", "FLOAT_TYPES", "ALL_DTYPES", "VALUE_RANGES",
    "value_to_registers", "registers_to_value", "registers_to_string",
    "string_to_registers", "get_dtype_width", "validate_value_range",
    "parse_int_token", "parse_parity", "slave_kwarg", "build_client",
    "connect_with_retries", "parse_modbus_address", "validate_args",
]

# ---------------------------------------------------------------------------
# Exit codes (kept identical to the original tool)
# ---------------------------------------------------------------------------
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONN = 3
EXIT_MBEXCEPTION = 4
EXIT_IO = 5


# ---------------------------------------------------------------------------
# Datatype Engine
# ---------------------------------------------------------------------------
INT_TYPES = {
    'u16': (1, False),
    'i16': (1, True),
    'u32': (2, False),
    'i32': (2, True),
    'u64': (4, False),
    'i64': (4, True),
}

FLOAT_TYPES = {
    'f32': (2, 'f'),
    'f64': (4, 'd'),
}

ALL_DTYPES = list(INT_TYPES.keys()) + list(FLOAT_TYPES.keys()) + ['str']

# Value ranges for validation
VALUE_RANGES = {
    'u16': (0, 65535),
    'i16': (-32768, 32767),
    'u32': (0, 4294967295),
    'i32': (-2147483648, 2147483647),
    'u64': (0, 2**64 - 1),
    'i64': (-2**63, 2**63 - 1),
}


def value_to_registers(value: Union[int, float, str], dtype: str,
                       byte_order: str, word_order: str) -> List[int]:
    """Convert a value to Modbus register words (big-endian wire format)."""
    if dtype in FLOAT_TYPES:
        wc, char = FLOAT_TYPES[dtype]
        fmt = ('>' if byte_order == 'big' else '<') + char
        packed = struct.pack(fmt, float(value))
    else:
        wc, signed = INT_TYPES[dtype]
        bits = wc * 16
        iv = int(value, 0) if isinstance(value, str) else int(value)
        if signed:
            iv &= (1 << bits) - 1  # two's complement
        packed = iv.to_bytes(wc * 2, byte_order)

    chunks = [packed[i:i + 2] for i in range(0, len(packed), 2)]
    regs = [int.from_bytes(c, 'big') for c in chunks]  # wire format: each register big-endian
    if word_order == 'little':
        regs.reverse()
    return regs


def registers_to_value(regs: List[int], dtype: str,
                       byte_order: str, word_order: str) -> Union[int, float]:
    """Convert Modbus register words to a typed value."""
    words = list(regs)
    if word_order == 'little':
        words.reverse()
    packed = b''.join(w.to_bytes(2, 'big') for w in words)

    if dtype in FLOAT_TYPES:
        wc, char = FLOAT_TYPES[dtype]
        fmt = ('>' if byte_order == 'big' else '<') + char
        return struct.unpack(fmt, packed)[0]

    wc, signed = INT_TYPES[dtype]
    v = int.from_bytes(packed, byte_order)
    if signed and v >= (1 << (wc * 16 - 1)):
        v -= 1 << (wc * 16)
    return v


def registers_to_string(regs: List[int], byte_order: str, encoding: str) -> str:
    """Convert register words to a string."""
    packed = b''.join((w & 0xFFFF).to_bytes(2, byte_order) for w in regs)
    return packed.decode(encoding, errors='replace').rstrip('\x00').rstrip()


def string_to_registers(text: str, encoding: str) -> List[int]:
    """Convert a string to register words."""
    b = text.encode(encoding)
    if len(b) % 2:
        b += b'\x00'
    return [int.from_bytes(b[i:i + 2], 'big') for i in range(0, len(b), 2)]


def get_dtype_width(dtype: str) -> int:
    """Return number of registers (words) for a dtype."""
    if dtype in INT_TYPES:
        return INT_TYPES[dtype][0]
    if dtype in FLOAT_TYPES:
        return FLOAT_TYPES[dtype][0]
    if dtype == 'str':
        return 1  # variable, handled specially
    raise ValueError(f"Unknown dtype: {dtype}")


def validate_value_range(value: Union[int, float, str], dtype: str) -> None:
    """Validate that a value fits in the dtype's range."""
    if dtype in VALUE_RANGES:
        lo, hi = VALUE_RANGES[dtype]
        iv = int(value, 0) if isinstance(value, str) else int(value)
        if not (lo <= iv <= hi):
            raise ValueError(f"Value {value} out of range for {dtype} ({lo}..{hi})")
    elif dtype in FLOAT_TYPES:
        fv = float(value)
        if fv != fv or fv in (float('inf'), float('-inf')):
            raise ValueError(f"Value {value} is not a finite float")


# ---------------------------------------------------------------------------
# Argument Parsing Helpers
# ---------------------------------------------------------------------------
def parse_int_token(token: str) -> int:
    """Parse decimal or 0x-prefixed hex integer."""
    try:
        return int(token, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid integer: {token}")


def parse_parity(token: str) -> str:
    """Map parity string to pymodbus single-char."""
    mapping = {'none': 'N', 'even': 'E', 'odd': 'O'}
    token_lower = token.lower()
    if token_lower not in mapping:
        raise argparse.ArgumentTypeError(
            f"Invalid parity: {token} (choose none, even, odd)")
    return mapping[token_lower]


# ---------------------------------------------------------------------------
# pymodbus version compatibility (device_id vs slave)
# ---------------------------------------------------------------------------
def slave_kwarg(slave_id: int) -> dict:
    """Return the correct keyword arg for the installed pymodbus version.

    pymodbus < 3.7 uses ``slave``, >= 3.7 (and the 3.15 installed here) uses
    ``device_id``.
    """
    import pymodbus
    try:
        major, minor = (int(x) for x in pymodbus.__version__.split(".")[:2])
    except Exception:
        major, minor = 3, 7
    if major >= 4 or (major == 3 and minor >= 7):
        return {"device_id": slave_id}
    return {"slave": slave_id}


# ---------------------------------------------------------------------------
# Client Building
# ---------------------------------------------------------------------------
def build_client(args: Any):
    """Build and return a pymodbus client based on parsed arguments.

    ``args`` is duck-typed (any object exposing the connection options).  The
    original CLI passed the :class:`argparse.Namespace` directly.
    """
    transport = args.transport
    host = args.ip
    port = args.port
    timeout = args.timeout

    if transport == 'tcp':
        return ModbusTcpClient(host=host, port=port, timeout=timeout)
    elif transport == 'udp':
        return ModbusUdpClient(host=host, port=port, timeout=timeout)
    elif transport in ('rtu', 'ascii'):
        framer = FramerType.RTU if transport == 'rtu' else FramerType.ASCII
        return ModbusSerialClient(
            port=host,
            baudrate=args.baudrate,
            parity=args.parity,
            stopbits=args.stopbits,
            bytesize=args.bytesize,
            timeout=timeout,
            framer=framer,
        )
    elif transport == 'tls':
        # Default port for TLS is 802 when the user supplied the generic 502.
        if port == 502:
            port = 802
        ctx = ssl.create_default_context()
        if getattr(args, 'no_verify', False):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif getattr(args, 'cert', None):
            ctx.load_cert_chain(certfile=args.cert, keyfile=getattr(args, 'key', None))
        return ModbusTlsClient(host=host, port=port, timeout=timeout, sslctx=ctx)
    else:
        raise ValueError(f"Unknown transport: {transport}")


def connect_with_retries(client, retries: int, transport_desc: str,
                         verbose: bool = False) -> bool:
    """Attempt connection with retries (``retries`` extra attempts)."""
    for attempt in range(retries + 1):
        if verbose:
            print(f"[connect] attempt {attempt + 1}/{retries + 1}: {transport_desc}",
                  file=sys.stderr)
        if client.connect():
            return True
        if attempt < retries:
            time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Smart address notation (inspired by 19bk/modbus-cli)
# ---------------------------------------------------------------------------
def parse_modbus_address(address: int) -> Tuple[str, int]:
    """Parse a Modbus address token.

    Standard Modbus notation auto-detects the register type:
        40001-49999  -> holding  (raw address 0-9998)
        30001-39999  -> input    (raw address 0-9998)
        10001-19999  -> discrete (raw address 0-9998)
        1-9999       -> coil     (raw address 0-9998)

    Anything outside those bands (e.g. raw 0-based 0..65535) is treated as a
    raw holding-register offset with type ``holding`` (caller may override
    via ``--register-type``).
    """
    if 40001 <= address <= 49999:
        return "holding", address - 40001
    elif 30001 <= address <= 39999:
        return "input", address - 30001
    elif 10001 <= address <= 19999:
        return "discrete", address - 10001
    elif 1 <= address <= 9999:
        return "coil", address - 1
    else:
        return "holding", address


# ---------------------------------------------------------------------------
# Validation (faithfully ported from the original tool)
# ---------------------------------------------------------------------------
def validate_args(args: Any) -> Optional[int]:
    """Validate parsed arguments. Returns an exit code if error, None if OK."""
    if getattr(args, "command", None) == "scan":
        addr = None  # scan uses --start/--end, no positional address
    else:
        addr = getattr(args, "address", None)
    if addr is not None and (addr < 0 or addr > 65535):
        print(f"Error: address {addr} out of range (0..65535)", file=sys.stderr)
        return EXIT_USAGE

    if args.command == 'scan':
        if args.start < 0 or args.start > 65535 or args.end < 0 or args.end > 65535:
            print("Error: start/end must be within 0..65535", file=sys.stderr)
            return EXIT_USAGE
        if args.start > args.end:
            print("Error: --start must be <= --end", file=sys.stderr)
            return EXIT_USAGE
        width = get_dtype_width(args.dtype)
        if args.start + (args.end - args.start + 1) * width > 65536:
            print("Error: scan range exceeds the Modbus address space", file=sys.stderr)
            return EXIT_USAGE
        if args.end - args.start >= 10000:
            print("Error: scan range (end - start) must be < 10000", file=sys.stderr)
            return EXIT_USAGE
        if args.dtype == 'str':
            print("Error: dtype str not allowed for scan", file=sys.stderr)
            return EXIT_USAGE

    if args.command in ('read', 'write', 'watch'):
        if args.dtype in FLOAT_TYPES and args.format in ('hex', 'bin'):
            print(f"Error: format {args.format} not allowed for float types", file=sys.stderr)
            return EXIT_USAGE

    if args.command == 'read':
        width = get_dtype_width(args.dtype)
        reg_count = args.count * width if args.dtype != 'str' else args.count
        if args.register_type in ('holding', 'input'):
            if reg_count > 125:
                print(f"Error: register count {reg_count} exceeds FC3/FC4 limit of 125", file=sys.stderr)
                return EXIT_USAGE
            if args.address + reg_count > 65536:
                print("Error: address + count exceeds the Modbus address space", file=sys.stderr)
                return EXIT_USAGE
        elif args.register_type in ('coil', 'discrete'):
            if args.count > 2000:
                print(f"Error: bit count {args.count} exceeds FC1/FC2 limit of 2000", file=sys.stderr)
                return EXIT_USAGE
            if args.dtype != 'u16':
                print("Error: dtype must be u16 for coil/discrete reads", file=sys.stderr)
                return EXIT_USAGE

    if args.command == 'watch':
        if args.interval <= 0:
            print("Error: --interval must be > 0", file=sys.stderr)
            return EXIT_USAGE
        if args.iterations < 0:
            print("Error: --iterations must be >= 0", file=sys.stderr)
            return EXIT_USAGE
        width = get_dtype_width(args.dtype)
        reg_count = args.count * width if args.dtype != 'str' else args.count
        if args.register_type in ('holding', 'input'):
            if reg_count > 125:
                print(f"Error: register count {reg_count} exceeds FC3/FC4 limit of 125", file=sys.stderr)
                return EXIT_USAGE
            if args.address + reg_count > 65536:
                print("Error: address + count exceeds the Modbus address space", file=sys.stderr)
                return EXIT_USAGE
        elif args.register_type in ('coil', 'discrete'):
            if args.count > 2000:
                print(f"Error: bit count {args.count} exceeds FC1/FC2 limit of 2000", file=sys.stderr)
                return EXIT_USAGE
            if args.dtype != 'u16':
                print("Error: dtype must be u16 for coil/discrete reads", file=sys.stderr)
                return EXIT_USAGE

    if args.command == 'write':
        width = get_dtype_width(args.dtype)
        reg_count = (len(args.values) * width
                     if args.dtype != 'str'
                     else len(string_to_registers(' '.join(args.values), args.encoding)))
        if reg_count > 123:
            print(f"Error: register count {reg_count} exceeds FC16 limit of 123", file=sys.stderr)
            return EXIT_USAGE
        if args.address + reg_count > 65536:
            print("Error: address + count exceeds the Modbus address space", file=sys.stderr)
            return EXIT_USAGE
        if reg_count == 1 and len(args.values) > 1:
            print("Error: single register write (FC6) takes exactly one value", file=sys.stderr)
            return EXIT_USAGE
        if args.dtype != 'str':
            for token in args.values:
                try:
                    validate_value_range(token, args.dtype)
                except ValueError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    return EXIT_USAGE
        elif args.register_type == 'coil':
            if len(args.values) > 1968:
                print(f"Error: coil count {len(args.values)} exceeds FC15 limit of 1968", file=sys.stderr)
                return EXIT_USAGE
            bool_map = {'0': False, '1': True, 'true': True, 'false': False,
                        'on': True, 'off': False, 'yes': True, 'no': False}
            for token in args.values:
                if token.lower() not in bool_map:
                    print(f"Error: invalid coil value '{token}' "
                          f"(use 0/1/true/false/on/off/yes/no)", file=sys.stderr)
                    return EXIT_USAGE

    return None

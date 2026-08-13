#!/usr/bin/env python3
"""
MODBUS — Industrial Modbus CLI
================================================================================

A production-quality Modbus command-line tool for talking to industrial
devices (PLCs, VFDs, meters, RTUs) over TCP, UDP, RTU, ASCII, and TLS
transports. Supports reading/writing holding/input registers, coils, and
discrete inputs with full datatype support (u16, i16, u32, i32, u64, i64,
f32, f64, str).

--------------------------------------------------------------------------------
DEVELOPER NOTES
--------------------------------------------------------------------------------
  Project    : modbus
  Author     : DarkSoulEngineer
  Version    : 1.0.0

  PURPOSE
  -------
  Automation and diagnostics CLI for Modbus devices. Designed for both human
  operators (readable tables) and scripts (--json machine output).

  ARCHITECTURE (top-down)
  -----------------------
  1. Datatype engine     - register <-> value conversions (int/float/str)
  2. Argument parser     - CLI surface for every transport and command
  3. Client factory      - builds the correct pymodbus client per transport
  4. Connection handling - retry / backoff connection logic
  5. Command handlers    - read / write / scan / watch implementations
  6. Output formatting   - human tables, value lines, JSON result builders

  CONVENTIONS
  -----------
  * All diagnostics, progress, and errors go to STDERR so STDOUT stays clean
    for machine-readable (--json) output. The startup banner is written to
    STDERR for the same reason.
  * Wire-format encoding is big-endian; byte/word order can be overridden
    via the --byte-order / --word-order options.

  QUICK REFERENCE
  ---------------
    python modbus.py --transport tcp --ip 192.168.1.10 --port 502 \
        read --type holding --address 0 --count 10
    python modbus.py --transport rtu --port COM3 --baudrate 9600 \
        write --type holding --address 0 --dtype u16 --value 1234
    python modbus.py --transport tcp --ip 192.168.1.10 \
        scan --start 0 --end 100 --json

  Run `python modbus.py --help` for the full option reference.
"""

import argparse
import json
import os
import ssl
import struct
import sys
import time
import traceback
from typing import Any, List, Optional, Tuple, Union

from pymodbus import FramerType
from pymodbus.client import ModbusTcpClient, ModbusUdpClient, ModbusSerialClient, ModbusTlsClient
from pymodbus.exceptions import ModbusException, ModbusIOException


__version__ = "1.0.0"


# =============================================================================
# Startup Banner
# =============================================================================
BANNER_ART = """\
███╗   ███╗   ██████╗  ██████╗  ██████╗  ██╗   ██╗  ███████╗
████╗ ████║  ██╔═══██╗ ██╔  ██╗ ██╔══██╗ ██║   ██║  ██╔════╝
██╔████╔██║  ██║   ██║ ██║  ██║ ██████╔╝ ██║   ██║  ███████╗
██║╚██╔╝██║  ██║   ██║ ██║  ██║ ██╔══██╗ ██║   ██║  ╚════██║
██║ ╚═╝ ██║  ╚██████╔╝ ██████╔╝ ██████╔╝ ╚██████╔╝  ███████║
╚═╝     ╚═╝   ╚═════╝  ╚═════╝  ╚═════╝   ╚═════╝   ╚══════╝"""


def _supports_color(stream: Any) -> bool:
    """Best-effort ANSI color support check (TTY + NO_COLOR / TERM=dumb guards)."""
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def _enable_vt() -> None:
    """Enable ANSI VT processing on Windows consoles. Best-effort, no-op elsewhere."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_ERROR_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def print_banner() -> None:
    """Print the startup splash banner to stderr (keeps stdout clean for JSON/data)."""
    color = _supports_color(sys.stderr)
    cyan, yellow, dim, reset = (
        ("\x1b[36m", "\x1b[33m", "\x1b[2m", "\x1b[0m") if color else ("", "", "", "")
    )
    if color:
        _enable_vt()

    art = [ln.rstrip() for ln in BANNER_ART.splitlines()]
    art_width = max(len(ln) for ln in art)

    pad = 2
    inner = art_width + pad * 2
    rule = "═" * inner
    border = f"╔{rule}╗"

    tag_parts = [
        (cyan, f"MODBUS CLI v{__version__}"),
        (dim, "  •  "),
        (yellow, "Crafted by DarkSoulEngineer"),
    ]
    tag_plain = "".join(text for _, text in tag_parts)
    tag_left = max(0, (inner - len(tag_plain)) // 2)
    tag_right = max(0, inner - tag_left - len(tag_plain))

    lines = ["", border, f"║{' ' * inner}║"]
    for ln in art:
        lines.append(f"║{' ' * pad}{cyan}{ln}{reset}{' ' * (inner - pad - len(ln))}║")
    lines.append(f"║{' ' * inner}║")
    lines.append(
        f"║{' ' * tag_left}{''.join(code + text for code, text in tag_parts)}{reset}"
        f"{' ' * tag_right}║"
    )
    lines.append(f"║{' ' * inner}║")
    lines.append(f"╚{rule}╝")
    lines.append(f"{' ' * pad}{dim}TCP · UDP · RTU · ASCII · TLS{reset}")
    lines.append(f"{' ' * pad}{dim}Registers · Coils · Discrete · Scan · Watch{reset}")
    lines.append("")

    text = "\n".join(lines) + "\n"
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        sys.stderr.write(text)
    except UnicodeEncodeError:
        sys.stderr.write(text.encode(encoding, "replace").decode(encoding))


# =============================================================================
# Datatype Engine
# =============================================================================

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


def value_to_registers(value: Union[int, float, str], dtype: str, byte_order: str, word_order: str) -> List[int]:
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
    
    chunks = [packed[i:i+2] for i in range(0, len(packed), 2)]
    regs = [int.from_bytes(c, 'big') for c in chunks]  # wire format: each register is big-endian
    if word_order == 'little':
        regs.reverse()
    return regs


def registers_to_value(regs: List[int], dtype: str, byte_order: str, word_order: str) -> Union[int, float]:
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
    return packed.decode(encoding).rstrip('\x00').rstrip()


def string_to_registers(text: str, encoding: str) -> List[int]:
    """Convert a string to register words."""
    b = text.encode(encoding)
    if len(b) % 2:
        b += b'\x00'
    return [int.from_bytes(b[i:i+2], 'big') for i in range(0, len(b), 2)]


def get_dtype_width(dtype: str) -> int:
    """Return number of registers (words) for a dtype."""
    if dtype in INT_TYPES:
        return INT_TYPES[dtype][0]
    if dtype in FLOAT_TYPES:
        return FLOAT_TYPES[dtype][0]
    if dtype == 'str':
        return 1  # variable, handled specially
    raise ValueError(f"Unknown dtype: {dtype}")


def validate_value_range(value: Union[int, float], dtype: str) -> None:
    """Validate that a value fits in the dtype's range."""
    if dtype in VALUE_RANGES:
        lo, hi = VALUE_RANGES[dtype]
        iv = int(value, 0) if isinstance(value, str) else int(value)
        if not (lo <= iv <= hi):
            raise ValueError(f"Value {value} out of range for {dtype} ({lo}..{hi})")
    elif dtype in FLOAT_TYPES:
        fv = float(value)
        if not (fv == fv and fv != float('inf') and fv != float('-inf')):  # NaN or inf check
            raise ValueError(f"Value {value} is not a finite float")


# =============================================================================
# Argument Parsing Helpers
# =============================================================================

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
        raise argparse.ArgumentTypeError(f"Invalid parity: {token} (choose none, even, odd)")
    return mapping[token_lower]


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """Add shared connection arguments to a subparser."""
    parser.add_argument('ip', help='Hostname/IP for tcp/udp/tls; serial device path (e.g. /dev/ttyUSB0) for rtu/ascii')
    parser.add_argument('--port', type=int, default=502, help='Port for tcp/udp/tls (default: 502, 802 for tls). Ignored for rtu/ascii.')
    parser.add_argument('--transport', choices=['tcp', 'udp', 'rtu', 'ascii', 'tls'], default='tcp', help='Transport type (default: tcp)')
    parser.add_argument('--unit-id', '--unit', type=int, default=1, dest='unit_id', help='Modbus unit/device ID (default: 1)')
    parser.add_argument('--timeout', type=float, default=3.0, help='Connection/response timeout in seconds (default: 3)')
    parser.add_argument('--retries', type=int, default=0, help='Extra connection attempts (default: 0)')
    parser.add_argument('-v', '--verbose', action='count', default=0, help='Verbose output to stderr (repeat for more)')
    parser.add_argument('--json', action='store_true', help='Machine-readable JSON output')
    
    # Serial-only
    serial_group = parser.add_argument_group('Serial options (rtu/ascii only)')
    serial_group.add_argument('--baudrate', type=int, default=9600, help='Baud rate (default: 9600)')
    serial_group.add_argument('--parity', type=parse_parity, default='none', help='Parity: none/even/odd (default: none)')
    serial_group.add_argument('--stopbits', type=int, choices=[1, 2], default=1, help='Stop bits (default: 1)')
    serial_group.add_argument('--bytesize', type=int, choices=[5, 6, 7, 8], default=8, help='Data bits (default: 8)')
    
    # TLS-only
    tls_group = parser.add_argument_group('TLS options (tls only)')
    tls_group.add_argument('--cert', help='Client certificate file (optional)')
    tls_group.add_argument('--key', help='Client private key file (optional)')
    tls_group.add_argument('--no-verify', action='store_true', help='Skip server certificate verification')


def add_dtype_args(parser: argparse.ArgumentParser) -> None:
    """Add datatype-related arguments to a subparser."""
    parser.add_argument('--dtype', choices=ALL_DTYPES, default='u16', help='Data type (default: u16)')
    parser.add_argument('--byte-order', choices=['big', 'little'], default='big', help='Byte order within register (default: big)')
    parser.add_argument('--word-order', choices=['big', 'little'], default='big', help='Word order for multi-register types (default: big)')
    parser.add_argument('--encoding', default='utf-8', help='String encoding (default: utf-8)')
    parser.add_argument('--scale', type=float, default=1.0, help='Scale factor: read value *= scale, write raw = value / scale (default: 1.0)')
    parser.add_argument('--format', choices=['dec', 'hex', 'bin'], default='dec', help='Output format for human mode (default: dec)')
    parser.add_argument('-a', '--address', type=parse_int_token, default=0, help='Starting address (decimal or 0x hex, default: 0)')


# =============================================================================
# Client Building
# =============================================================================

def build_client(args: argparse.Namespace):
    """Build and return a pymodbus client based on parsed arguments."""
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
            framer=framer
        )
    
    elif transport == 'tls':
        # Default port for TLS is 802
        if port == 502:
            port = 802
        
        # Build SSL context
        ctx = ssl.create_default_context()
        if args.no_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif args.cert:
            ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
        
        return ModbusTlsClient(host=host, port=port, timeout=timeout, sslctx=ctx)
    
    else:
        raise ValueError(f"Unknown transport: {transport}")


def connect_with_retries(client, args: argparse.Namespace, verbose: bool) -> bool:
    """Attempt connection with retries."""
    for attempt in range(args.retries + 1):
        if verbose:
            transport_desc = f"{args.transport}"
            if args.transport in ('tcp', 'udp', 'tls'):
                transport_desc += f" {args.ip}:{args.port}"
            else:
                transport_desc += f" {args.ip}"
            print(f"[connect] attempt {attempt + 1}/{args.retries + 1}: {transport_desc}", file=sys.stderr)
        
        if client.connect():
            return True
        
        if attempt < args.retries:
            time.sleep(0.5)
    
    return False


# =============================================================================
# Output Formatting
# =============================================================================

def format_value(value: Union[int, float, str], dtype: str, fmt: str, scale: float = 1.0) -> str:
    """Format a value for human output."""
    if dtype == 'str':
        return value
    
    if scale != 1.0:
        if dtype in INT_TYPES:
            scaled = value * scale
            if scaled != int(scaled):
                value = scaled
            else:
                value = int(scaled)
        else:
            value = value * scale
    
    if dtype in FLOAT_TYPES or isinstance(value, float):
        if fmt in ('hex', 'bin'):
            raise ValueError(f"Format {fmt} not allowed for float types")
        return f"{value:.9g}"
    
    # Integer formatting
    if fmt == 'dec':
        return str(int(value))
    elif fmt == 'hex':
        return f"0x{int(value):X}"
    elif fmt == 'bin':
        return f"0b{int(value):b}"
    return str(value)


def print_summary(args: argparse.Namespace, count: int, reg_count: int, reg_type: str, is_scan: bool = False) -> None:
    """Print the summary line (human mode)."""
    transport_suffix = f", transport {args.transport}" if args.transport != 'tcp' else ""
    
    if is_scan:
        print(f"Scanning {args.address}..{getattr(args, 'end', args.address + count - 1)} ({reg_count} register(s)) from {args.ip}:{args.port}, unit {args.unit_id}{transport_suffix}")
    elif reg_type in ('coil', 'discrete'):
        label = "coil(s)" if reg_type == 'coil' else "discrete input(s)"
        print(f"Read {count} {label} from {args.ip}:{args.port}, unit {args.unit_id}{transport_suffix}")
    elif args.dtype in ('u16', 'i16'):
        print(f"Read {count} register(s) from {args.ip}:{args.port}, unit {args.unit_id}{transport_suffix}")
    else:
        print(f"Read {count} value(s) ({reg_count} register(s)) from {args.ip}:{args.port}, unit {args.unit_id}{transport_suffix}")


def print_value_line(address: int, value: Any, dtype: str, fmt: str, scale: float, raw_regs: List[int], reg_type: str = 'register') -> None:
    """Print a single value line (human mode)."""
    if reg_type in ('coil', 'discrete'):
        label = "Coil" if reg_type == 'coil' else "Discrete"
        print(f"{label} {address}: {'ON' if value else 'OFF'}")
        return

    label = "Register"

    if dtype == 'str':
        formatted = format_value(value, dtype, fmt, scale)
        raw_hex = ', '.join(f"0x{r:04X}" for r in raw_regs)
        print(f"{label} {address} ({dtype}): {formatted} [raw: {raw_hex}]")
    elif dtype == 'u16':
        formatted = format_value(value, dtype, fmt, scale)
        print(f"{label} {address}: {formatted}")
    elif dtype == 'i16':
        formatted = format_value(value, dtype, fmt, scale)
        print(f"{label} {address} ({dtype}): {formatted}")
    else:
        formatted = format_value(value, dtype, fmt, scale)
        raw_hex = ', '.join(f"0x{r:04X}" for r in raw_regs)
        print(f"{label} {address} ({dtype}): {formatted} [raw: {raw_hex}]")


def build_json_read_result(args: argparse.Namespace, values: List[dict], reg_type: str) -> dict:
    """Build JSON result for read/scan."""
    return {
        "command": "scan" if getattr(args, 'is_scan', False) else "read",
        "register_type": reg_type,
        "transport": args.transport,
        "host": args.ip,
        "port": args.port,
        "unit_id": args.unit_id,
        "dtype": args.dtype,
        "byte_order": args.byte_order,
        "word_order": args.word_order,
        "count": len(values),
        "values": values
    }


def build_json_write_result(args: argparse.Namespace, values: List[dict], reg_type: str) -> dict:
    """Build JSON result for write."""
    return {
        "command": "write",
        "register_type": reg_type,
        "transport": args.transport,
        "host": args.ip,
        "port": args.port,
        "unit_id": args.unit_id,
        "dtype": args.dtype,
        "byte_order": args.byte_order,
        "word_order": args.word_order,
        "values": values
    }


# =============================================================================
# Command Implementations
# =============================================================================

def cmd_read(args: argparse.Namespace, client) -> int:
    """Execute read command."""
    reg_type = args.register_type
    address = args.address
    count = args.count
    dtype = args.dtype
    unit_id = args.unit_id
    verbose = args.verbose
    json_out = args.json
    
    width = get_dtype_width(dtype)
    if reg_type in ('holding', 'input'):
        reg_count = count * width if dtype != 'str' else count
        if reg_count > 125:
            print(f"Error: register count {reg_count} exceeds FC3/FC4 limit of 125", file=sys.stderr)
            return 2
    elif reg_type in ('coil', 'discrete'):
        if count > 2000:
            print(f"Error: bit count {count} exceeds FC1/FC2 limit of 2000", file=sys.stderr)
            return 2
    
    if dtype == 'str' and reg_type in ('coil', 'discrete'):
        print("Error: dtype str not valid for coil/discrete", file=sys.stderr)
        return 2
    
    if dtype in FLOAT_TYPES and args.format in ('hex', 'bin'):
        print(f"Error: format {args.format} not allowed for float types", file=sys.stderr)
        return 2
    
    if verbose:
        print(f"[read] type={reg_type} address={address} count={count} dtype={dtype} unit={unit_id}", file=sys.stderr)
    
    try:
        if reg_type == 'holding':
            response = client.read_holding_registers(address=address, count=reg_count if dtype != 'str' else count, device_id=unit_id)
        elif reg_type == 'input':
            response = client.read_input_registers(address=address, count=reg_count if dtype != 'str' else count, device_id=unit_id)
        elif reg_type == 'coil':
            response = client.read_coils(address=address, count=count, device_id=unit_id)
        elif reg_type == 'discrete':
            response = client.read_discrete_inputs(address=address, count=count, device_id=unit_id)
        else:
            print(f"Error: unknown register type {reg_type}", file=sys.stderr)
            return 2
    except (ModbusIOException, ModbusException, OSError, TimeoutError) as e:
        print(f"Modbus read error: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return 5
    
    if response.isError():
        exc_code = getattr(response, 'exception_code', 'unknown')
        print(f"Modbus error (code {exc_code}): {response}", file=sys.stderr)
        return 4
    
    # Process results
    json_values = []
    
    if reg_type in ('coil', 'discrete'):
        bits = response.bits[:count]
        if not json_out:
            print_summary(args, count, count, reg_type)
        for i, bit in enumerate(bits):
            addr = address + i
            val = bool(bit)
            if json_out:
                json_values.append({"address": addr, "value": val})
            else:
                print_value_line(addr, val, 'coil', args.format, args.scale, [], reg_type)
    
    elif dtype == 'str':
        regs = response.registers[:count]
        decoded = registers_to_string(regs, args.byte_order, args.encoding)
        if not json_out:
            print_summary(args, 1, count, reg_type)
            print_value_line(address, decoded, 'str', args.format, args.scale, regs)
        json_values.append({
            "address": address,
            "value": decoded,
            "raw_value": decoded,
            "registers": regs
        })
    
    else:
        regs = response.registers
        width = get_dtype_width(dtype)
        reg_count = count * width
        
        if not json_out:
            print_summary(args, count, reg_count, reg_type)
        
        for i in range(count):
            start = i * width
            end = start + width
            chunk = regs[start:end]
            if len(chunk) < width:
                break
            val = registers_to_value(chunk, dtype, args.byte_order, args.word_order)
            addr = address + start
            
            if json_out:
                json_values.append({
                    "address": addr,
                    "value": val * args.scale if args.scale != 1.0 else val,
                    "raw_value": val,
                    "registers": chunk
                })
            else:
                print_value_line(addr, val, dtype, args.format, args.scale, chunk)
    
    if json_out:
        result = build_json_read_result(args, json_values, reg_type)
        print(json.dumps(result, indent=2))
    
    return 0


def cmd_write(args: argparse.Namespace, client) -> int:
    """Execute write command."""
    reg_type = args.register_type
    address = args.address
    dtype = args.dtype
    unit_id = args.unit_id
    verbose = args.verbose
    json_out = args.json
    values_tokens = args.values
    
    # Parse values
    if reg_type == 'holding':
        if dtype == 'str':
            text = ' '.join(values_tokens)
            regs = string_to_registers(text, args.encoding)
            values = [text]
        else:
            width = get_dtype_width(dtype)
            values = []
            regs = []
            for token in values_tokens:
                # Validate range
                validate_value_range(token, dtype)
                # Apply inverse scaling for write
                if args.scale != 1.0:
                    if dtype in INT_TYPES:
                        raw_val = int(token, 0) / args.scale
                        if raw_val != int(raw_val):
                            print(f"Error: scaled value {token} / {args.scale} = {raw_val} is not integral for {dtype}", file=sys.stderr)
                            return 2
                        token = str(int(raw_val))
                    else:
                        token = str(float(token) / args.scale)
                v = int(token, 0) if dtype in INT_TYPES else float(token)
                values.append(v)
                regs.extend(value_to_registers(v, dtype, args.byte_order, args.word_order))
        
        reg_count = len(regs)
        if reg_count > 123:
            print(f"Error: register count {reg_count} exceeds FC16 limit of 123", file=sys.stderr)
            return 2
        if reg_count == 1 and len(values_tokens) > 1:
            print("Error: single register write (FC6) takes exactly one value", file=sys.stderr)
            return 2
    
    elif reg_type == 'coil':
        bool_map = {'0': False, '1': True, 'true': True, 'false': False, 'on': True, 'off': False, 'yes': True, 'no': False}
        values = []
        for token in values_tokens:
            tl = token.lower()
            if tl not in bool_map:
                print(f"Error: invalid coil value '{token}' (use 0/1/true/false/on/off/yes/no)", file=sys.stderr)
                return 2
            values.append(bool_map[tl])
        
        if len(values) > 1968:
            print(f"Error: coil count {len(values)} exceeds FC15 limit of 1968", file=sys.stderr)
            return 2
        regs = []
    
    else:
        print(f"Error: register type {reg_type} not writable", file=sys.stderr)
        return 2
    
    if verbose:
        print(f"[write] type={reg_type} address={address} values={values} dtype={dtype} unit={unit_id}", file=sys.stderr)
    
    try:
        if reg_type == 'holding':
            if len(regs) == 1:
                response = client.write_register(address=address, value=regs[0], device_id=unit_id)
            else:
                response = client.write_registers(address=address, values=regs, device_id=unit_id)
        elif reg_type == 'coil':
            if len(values) == 1:
                response = client.write_coil(address=address, value=values[0], device_id=unit_id)
            else:
                response = client.write_coils(address=address, values=values, device_id=unit_id)
    except (ModbusIOException, ModbusException, OSError, TimeoutError) as e:
        print(f"Modbus write error: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return 5
    
    if response.isError():
        exc_code = getattr(response, 'exception_code', 'unknown')
        print(f"Modbus error (code {exc_code}): {response}", file=sys.stderr)
        return 4
    
    # Output
    json_values = []
    
    if not json_out:
        label = "register(s)" if reg_type == 'holding' else "coil(s)"
        print(f"Wrote {len(values)} {label} starting at address {address}:")
    
    if reg_type == 'holding':
        if dtype == 'str':
            if not json_out:
                print_value_line(address, values[0], 'str', args.format, args.scale, regs)
            json_values.append({"address": address, "value": values[0], "registers": regs})
        else:
            width = get_dtype_width(dtype)
            for i, val in enumerate(values):
                chunk = regs[i*width:(i+1)*width]
                addr = address + i*width
                if not json_out:
                    print_value_line(addr, val, dtype, args.format, args.scale, chunk)
                json_values.append({"address": addr, "value": val, "registers": chunk})
    else:  # coil
        for i, val in enumerate(values):
            addr = address + i
            if not json_out:
                print_value_line(addr, val, 'coil', args.format, args.scale, [], 'coil')
            json_values.append({"address": addr, "value": val, "registers": []})
    
    if json_out:
        result = build_json_write_result(args, json_values, reg_type)
        print(json.dumps(result, indent=2))
    
    return 0


def cmd_scan(args: argparse.Namespace, client) -> int:
    """Execute scan command."""
    reg_type = args.register_type
    start = args.start
    end = args.end
    dtype = args.dtype
    unit_id = args.unit_id
    verbose = args.verbose
    json_out = args.json
    show_all = args.all
    
    if dtype == 'str':
        print("Error: dtype str not allowed for scan", file=sys.stderr)
        return 2
    
    if dtype in FLOAT_TYPES and args.format in ('hex', 'bin'):
        print(f"Error: format {args.format} not allowed for float types", file=sys.stderr)
        return 2
    
    if start > end:
        print("Error: --start must be <= --end", file=sys.stderr)
        return 2
    if end - start >= 10000:
        print("Error: scan range (end - start) must be < 10000", file=sys.stderr)
        return 2
    
    width = get_dtype_width(dtype)
    chunk_regs = 123  # FC3/FC4 max per request
    chunk_regs = (chunk_regs // width) * width  # round down to multiple of width
    if chunk_regs == 0:
        chunk_regs = width
    
    args.is_scan = True  # for JSON output
    
    all_values = []
    total_regs = end - start + 1
    
    if not json_out:
        print_summary(args, 0, total_regs, reg_type, is_scan=True)
    
    addr = start
    while addr <= end:
        remaining = end - addr + 1
        read_regs = min(chunk_regs, remaining)
        read_count = read_regs // width
        
        if verbose:
            print(f"[scan] reading {read_regs} registers at address {addr}", file=sys.stderr)
        
        try:
            if reg_type == 'holding':
                response = client.read_holding_registers(address=addr, count=read_regs, device_id=unit_id)
            else:  # input
                response = client.read_input_registers(address=addr, count=read_regs, device_id=unit_id)
        except (ModbusIOException, ModbusException, OSError, TimeoutError) as e:
            print(f"Modbus read error: {e}", file=sys.stderr)
            if verbose:
                traceback.print_exc(file=sys.stderr)
            return 5
        
        if response.isError():
            exc_code = getattr(response, 'exception_code', 'unknown')
            print(f"Modbus error (code {exc_code}): {response}", file=sys.stderr)
            return 4
        
        regs = response.registers
        for i in range(read_count):
            chunk = regs[i*width:(i+1)*width]
            if len(chunk) < width:
                break
            val = registers_to_value(chunk, dtype, args.byte_order, args.word_order)
            curr_addr = addr + i*width
            
            is_zero = (val == 0)
            if show_all or not is_zero:
                if json_out:
                    all_values.append({
                        "address": curr_addr,
                        "value": val * args.scale if args.scale != 1.0 else val,
                        "raw_value": val,
                        "registers": chunk
                    })
                else:
                    print_value_line(curr_addr, val, dtype, args.format, args.scale, chunk)
        
        addr += read_regs
    
    if json_out:
        result = build_json_read_result(args, all_values, reg_type)
        print(json.dumps(result, indent=2))
    
    return 0


def cmd_watch(args: argparse.Namespace, client) -> int:
    """Continuously poll registers and print changed values (watch command).

    Polls the configured range every --interval seconds. The first poll
    establishes a baseline (all values are shown); subsequent polls print
    only values that changed since the previous poll. --all prints every
    value on every poll. --json streams one JSON object per poll (JSON
    lines); --output appends the same output to a file.
    """
    reg_type = args.register_type
    dtype = args.dtype
    width = get_dtype_width(dtype)
    unit_id = args.unit_id
    json_out = args.json
    show_all = args.all
    verbose = args.verbose

    reg_count = args.count * width if dtype != 'str' else args.count
    end_addr = args.address + (args.count if reg_type in ('coil', 'discrete') else reg_count) - 1

    out_file = None
    if args.output:
        try:
            out_file = open(args.output, 'a', encoding='utf-8')
        except OSError as e:
            print(f"Error: cannot open output file {args.output}: {e}", file=sys.stderr)
            return 1

    def emit(line: str) -> None:
        print(line, flush=True)
        if out_file is not None:
            print(line, file=out_file, flush=True)

    stop_desc = "Ctrl-C to stop" if not args.iterations else f"stop after {args.iterations} poll(s)"
    print(f"[watch] monitoring {reg_type} range {args.address}..{end_addr} on "
          f"{args.ip}:{args.port}, interval {args.interval}s ({stop_desc})", file=sys.stderr)

    previous = {}  # address -> raw value from the previous poll
    poll = 0
    consecutive_errors = 0

    try:
        while True:
            if args.iterations and poll >= args.iterations:
                break
            poll += 1
            stamp = time.strftime('%H:%M:%S')

            try:
                if reg_type == 'coil':
                    response = client.read_coils(address=args.address, count=args.count, device_id=unit_id)
                elif reg_type == 'discrete':
                    response = client.read_discrete_inputs(address=args.address, count=args.count, device_id=unit_id)
                elif reg_type == 'input':
                    response = client.read_input_registers(address=args.address, count=reg_count, device_id=unit_id)
                else:
                    response = client.read_holding_registers(address=args.address, count=reg_count, device_id=unit_id)
            except (ModbusIOException, ModbusException, OSError, TimeoutError) as e:
                consecutive_errors += 1
                print(f"[watch] poll {poll} read error: {e}", file=sys.stderr)
                if consecutive_errors >= 10:
                    return 5
                if not (args.iterations and poll >= args.iterations):
                    time.sleep(args.interval)
                continue

            if response.isError():
                consecutive_errors += 1
                exc_code = getattr(response, 'exception_code', 'unknown')
                print(f"[watch] poll {poll} modbus error (code {exc_code}): {response}", file=sys.stderr)
                if consecutive_errors >= 10:
                    return 4
                if not (args.iterations and poll >= args.iterations):
                    time.sleep(args.interval)
                continue

            consecutive_errors = 0

            current = {}
            if reg_type in ('coil', 'discrete'):
                for i, b in enumerate(response.bits[:args.count]):
                    current[args.address + i] = (bool(b), [])
            elif dtype == 'str':
                text = registers_to_string(list(response.registers[:reg_count]), args.byte_order, args.encoding)
                current[args.address] = (text, list(response.registers[:reg_count]))
            else:
                for i in range(args.count):
                    chunk = list(response.registers[i * width:(i + 1) * width])
                    raw = registers_to_value(chunk, dtype, args.byte_order, args.word_order)
                    current[args.address + i * width] = (raw, chunk)

            # Diff against the previous poll
            changes = []
            for addr in sorted(current):
                raw, regs = current[addr]
                prev_raw = previous.get(addr)
                changed = prev_raw is None or prev_raw != raw
                if show_all or changed:
                    changes.append((addr, prev_raw if changed else None, raw, regs))
            previous = {addr: raw for addr, (raw, _regs) in current.items()}

            if json_out:
                payload = {
                    "command": "watch",
                    "register_type": reg_type,
                    "transport": args.transport,
                    "host": args.ip,
                    "port": args.port,
                    "unit_id": unit_id,
                    "dtype": dtype,
                    "byte_order": args.byte_order,
                    "word_order": args.word_order,
                    "poll": poll,
                    "ts": time.time(),
                }
                json_changes = []
                for addr, prev_raw, raw, regs in changes:
                    if reg_type in ('coil', 'discrete'):
                        value = bool(raw)
                        previous_val = None if prev_raw is None else bool(prev_raw)
                    elif dtype == 'str':
                        value = raw
                        previous_val = prev_raw
                    else:
                        value = raw * args.scale
                        previous_val = None if prev_raw is None else prev_raw * args.scale
                    json_changes.append({
                        "address": addr,
                        "value": value,
                        "previous": previous_val,
                        "raw_value": raw,
                        "registers": regs,
                    })
                payload["changes"] = json_changes
                emit(json.dumps(payload))
            else:
                if changes:
                    emit(f"[{stamp}] poll {poll}: {len(changes)} change(s)")
                    for addr, prev_raw, raw, regs in changes:
                        if reg_type in ('coil', 'discrete'):
                            label = "Coil" if reg_type == 'coil' else "Discrete"
                            cur = 'ON' if raw else 'OFF'
                            prev = '' if prev_raw is None else f"{'ON' if prev_raw else 'OFF'} -> "
                            emit(f"{label} {addr}: {prev}{cur}")
                        else:
                            cur_s = format_value(raw, dtype, args.format, args.scale)
                            prev_s = '' if prev_raw is None else f"{format_value(prev_raw, dtype, args.format, args.scale)} -> "
                            tag = "" if dtype == 'u16' else f" ({dtype})"
                            raw_hex = (f" [raw: {', '.join(f'0x{r:04X}' for r in regs)}]"
                                       if (dtype == 'str' or width > 1) else "")
                            emit(f"Register {addr}{tag}: {prev_s}{cur_s}{raw_hex}")

            if args.iterations and poll >= args.iterations:
                break
            time.sleep(args.interval)

        return 0
    except KeyboardInterrupt:
        print("[watch] stopped (Ctrl-C)", file=sys.stderr)
        return 0
    finally:
        if out_file is not None:
            out_file.close()


# =============================================================================
# Main
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog='modbus_cli',
        description='Production-quality Modbus CLI tool for TCP, UDP, RTU, ASCII, and TLS.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  ./modbus_cli read 10.0.0.5                       # 10 holding registers (u16)
  ./modbus_cli read 10.0.0.5 -a 100 -c 5           # 5 registers at 100
  ./modbus_cli read 10.0.0.5 --dtype f32 -c 4      # 4 float32 values (8 registers)
  ./modbus_cli read 10.0.0.5 --dtype u32 --word-order little -c 2
  ./modbus_cli read 10.0.0.5 --register-type coil -c 16
  ./modbus_cli read 10.0.0.5 --register-type input -c 20 --json
  ./modbus_cli write 10.0.0.5 -a 40001 123 456     # write two u16 registers (FC16)
  ./modbus_cli write 10.0.0.5 -a 100 --dtype i16 -5
  ./modbus_cli write 10.0.0.5 -a 200 --dtype f32 3.14
  ./modbus_cli write 10.0.0.5 --register-type coil -a 0 on off on
  ./modbus_cli scan 10.0.0.5 -s 0 -e 999           # find non-zero registers
  ./modbus_cli scan 10.0.0.5 -s 0 -e 99 --dtype f32 --json
  ./modbus_cli watch 10.0.0.5 -c 10 --interval 1      # poll every second
  ./modbus_cli watch 10.0.0.5 -c 5 --json --iterations 60
  ./modbus_cli watch 10.0.0.5 -c 10 --output plant.log    # log polls to file
  ./modbus_cli read /dev/ttyUSB0 --transport rtu --baudrate 19200 -a 0 -c 10
  ./modbus_cli read 10.0.0.5 --transport tls --port 802 --no-verify

script/AI mode:
  ./modbus_cli --no-banner read 10.0.0.5 -c 10 --json   # no startup banner
  MODBUS_CLI_NO_BANNER=1 ./modbus_cli read 10.0.0.5 -c 10

exit codes:
  0 success, 1 unexpected error, 2 usage error, 3 connection failure,
  4 modbus exception response, 5 i/o or timeout error"""
    )
    parser.add_argument('--version', action='version', version=f'modbus_cli {__version__}')
    parser.add_argument('--no-banner', action='store_true',
                        help='Suppress the startup banner (script/AI mode; also settable via MODBUS_CLI_NO_BANNER=1)')
    
    subparsers = parser.add_subparsers(dest='command', required=True, help='Subcommand')
    
    # Read subcommand
    read_parser = subparsers.add_parser('read', help='Read registers/coils/inputs', formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(read_parser)
    add_dtype_args(read_parser)
    read_parser.add_argument('--register-type', choices=['holding', 'input', 'coil', 'discrete'], default='holding', help='Register type (default: holding)')
    read_parser.add_argument('-c', '--count', type=int, default=10, help='Number of values (default: 10)')
    
    # Write subcommand
    write_parser = subparsers.add_parser('write', help='Write registers/coils', formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(write_parser)
    add_dtype_args(write_parser)
    write_parser.add_argument('--register-type', choices=['holding', 'coil'], default='holding', help='Register type (default: holding)')
    write_parser.add_argument('values', nargs='+', help='Value(s) to write')
    
    # Scan subcommand
    scan_parser = subparsers.add_parser('scan', help='Scan register range for non-zero values', formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(scan_parser)
    add_dtype_args(scan_parser)
    scan_parser.add_argument('--register-type', choices=['holding', 'input'], default='holding', help='Register type (default: holding)')
    scan_parser.add_argument('-s', '--start', type=parse_int_token, default=0, help='Start address (inclusive, default: 0)')
    scan_parser.add_argument('-e', '--end', type=parse_int_token, default=99, help='End address (inclusive, default: 99)')
    scan_parser.add_argument('--all', action='store_true', help='Show all values including zeros')
    # Remove --count from scan (not used)

    # Watch subcommand
    watch_parser = subparsers.add_parser('watch', help='Continuously poll registers, print changed values', formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(watch_parser)
    add_dtype_args(watch_parser)
    watch_parser.add_argument('--register-type', choices=['holding', 'input', 'coil', 'discrete'], default='holding', help='Register type (default: holding)')
    watch_parser.add_argument('-c', '--count', type=int, default=10, help='Number of values (default: 10)')
    watch_parser.add_argument('--interval', type=float, default=1.0, help='Polling interval in seconds (default: 1.0)')
    watch_parser.add_argument('--iterations', type=int, default=0, help='Stop after N polls (default: 0 = run until Ctrl-C)')
    watch_parser.add_argument('--output', default=None, help='Append each poll output to this file')
    watch_parser.add_argument('--all', action='store_true', help='Show all values every poll, not only changes')
    scan_parser._optionals._actions = [a for a in scan_parser._optionals._actions if a.dest != 'count']
    
    return parser


def validate_args(args: argparse.Namespace) -> Optional[int]:
    """Validate parsed arguments. Returns exit code if error, None if OK."""
    if args.address < 0 or args.address > 65535:
        print(f"Error: address {args.address} out of range (0..65535)", file=sys.stderr)
        return 2
    
    if args.command == 'scan':
        if args.start < 0 or args.start > 65535 or args.end < 0 or args.end > 65535:
            print("Error: start/end must be within 0..65535", file=sys.stderr)
            return 2
        if args.start > args.end:
            print("Error: --start must be <= --end", file=sys.stderr)
            return 2
        width = get_dtype_width(args.dtype)
        if args.start + (args.end - args.start + 1) * width > 65536:
            print("Error: scan range exceeds the Modbus address space", file=sys.stderr)
            return 2
        if args.end - args.start >= 10000:
            print("Error: scan range (end - start) must be < 10000", file=sys.stderr)
            return 2
        if args.dtype == 'str':
            print("Error: dtype str not allowed for scan", file=sys.stderr)
            return 2
    
    if args.command in ('read', 'write', 'watch'):
        if args.dtype in FLOAT_TYPES and args.format in ('hex', 'bin'):
            print(f"Error: format {args.format} not allowed for float types", file=sys.stderr)
            return 2
    
    if args.command == 'read':
        width = get_dtype_width(args.dtype)
        reg_count = args.count * width if args.dtype != 'str' else args.count
        if args.register_type in ('holding', 'input'):
            if reg_count > 125:
                print(f"Error: register count {reg_count} exceeds FC3/FC4 limit of 125", file=sys.stderr)
                return 2
            if args.address + reg_count > 65536:
                print("Error: address + count exceeds the Modbus address space", file=sys.stderr)
                return 2
        elif args.register_type in ('coil', 'discrete'):
            if args.count > 2000:
                print(f"Error: bit count {args.count} exceeds FC1/FC2 limit of 2000", file=sys.stderr)
                return 2
            if args.dtype != 'u16':  # coils are always bits
                print("Error: dtype must be u16 for coil/discrete reads", file=sys.stderr)
                return 2
    
    if args.command == 'watch':
        if args.interval <= 0:
            print("Error: --interval must be > 0", file=sys.stderr)
            return 2
        if args.iterations < 0:
            print("Error: --iterations must be >= 0", file=sys.stderr)
            return 2
        width = get_dtype_width(args.dtype)
        reg_count = args.count * width if args.dtype != 'str' else args.count
        if args.register_type in ('holding', 'input'):
            if reg_count > 125:
                print(f"Error: register count {reg_count} exceeds FC3/FC4 limit of 125", file=sys.stderr)
                return 2
            if args.address + reg_count > 65536:
                print("Error: address + count exceeds the Modbus address space", file=sys.stderr)
                return 2
        elif args.register_type in ('coil', 'discrete'):
            if args.count > 2000:
                print(f"Error: bit count {args.count} exceeds FC1/FC2 limit of 2000", file=sys.stderr)
                return 2
            if args.dtype != 'u16':
                print("Error: dtype must be u16 for coil/discrete reads", file=sys.stderr)
                return 2

    if args.command == 'write':
        if args.register_type == 'holding':
            width = get_dtype_width(args.dtype)
            reg_count = len(args.values) * width if args.dtype != 'str' else len(string_to_registers(' '.join(args.values), args.encoding))
            if reg_count > 123:
                print(f"Error: register count {reg_count} exceeds FC16 limit of 123", file=sys.stderr)
                return 2
            if args.address + reg_count > 65536:
                print("Error: address + count exceeds the Modbus address space", file=sys.stderr)
                return 2
            if reg_count == 1 and len(args.values) > 1:
                print("Error: single register write (FC6) takes exactly one value", file=sys.stderr)
                return 2
            # Validate value ranges
            if args.dtype != 'str':
                for token in args.values:
                    try:
                        validate_value_range(token, args.dtype)
                    except ValueError as e:
                        print(f"Error: {e}", file=sys.stderr)
                        return 2
        elif args.register_type == 'coil':
            if len(args.values) > 1968:
                print(f"Error: coil count {len(args.values)} exceeds FC15 limit of 1968", file=sys.stderr)
                return 2
            bool_map = {'0': False, '1': True, 'true': True, 'false': False, 'on': True, 'off': False, 'yes': True, 'no': False}
            for token in args.values:
                if token.lower() not in bool_map:
                    print(f"Error: invalid coil value '{token}' (use 0/1/true/false/on/off/yes/no)", file=sys.stderr)
                    return 2
    
    return None


def main() -> int:
    parser = build_parser()
    banner_suppressed = (
        '--no-banner' in sys.argv
        or os.environ.get('MODBUS_CLI_NO_BANNER')
        or os.environ.get('NO_BANNER')
    )
    # Bare invocation (no subcommand): show the splash banner before the usage error.
    if len(sys.argv) == 1 and not banner_suppressed:
        print_banner()
    args = parser.parse_args()
    if not banner_suppressed:
        print_banner()
    
    validation_error = validate_args(args)
    if validation_error is not None:
        return validation_error
    
    verbose = args.verbose
    json_out = args.json
    
    # Build client
    try:
        client = build_client(args)
    except Exception as e:
        print(f"Error building client: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return 1
    
    if verbose:
        transport_desc = f"{args.transport}"
        if args.transport in ('tcp', 'udp', 'tls'):
            transport_desc += f" {args.ip}:{args.port}"
        else:
            transport_desc += f" {args.ip}"
        print(f"[config] {transport_desc}, unit={args.unit_id}, timeout={args.timeout}s", file=sys.stderr)
    
    connected = connect_with_retries(client, args, verbose)
    if not connected:
        host_port = f"{args.ip}:{args.port}" if args.transport in ('tcp', 'udp', 'tls') else args.ip
        print(f"Could not connect to {host_port}", file=sys.stderr)
        return 3
    
    try:
        if args.command == 'read':
            return cmd_read(args, client)
        elif args.command == 'write':
            return cmd_write(args, client)
        elif args.command == 'scan':
            return cmd_scan(args, client)
        elif args.command == 'watch':
            return cmd_watch(args, client)
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            return 2
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())




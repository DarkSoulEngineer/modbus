"""modbus.cli -- argparse CLI + rich output wiring.

Entry point: ``main()`` (console script ``modbus`` / ``python -m modbus``).

Commands:
    read      read registers/coils with optional dtype decoding
    write     write registers/coils
    scan      sweep an address range and report non-zero registers
    watch     poll registers at an interval with change highlighting
    simulate  launch the embedded test server (see modbus.simulator)
    tui       interactive console shell (REPL) -- see modbus.tui

All protocol logic (datatype engine, client factory, validation) lives in
:mod:`modbus.core`; rendering lives in :mod:`modbus.theme`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import List, Optional

from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from . import core
from .simulator import main as simulator_main
from .theme import (
    banner,
    connection_header,
    console,
    error_panel,
    success_panel,
    value_bar,
)

PROG = "modbus"

# ---------------------------------------------------------------------------
# argparse helpers
# ---------------------------------------------------------------------------


def _add_connection_args(parser) -> None:
    """Connection/transport options shared by every command."""
    parser.add_argument("--transport", "-T", choices=["tcp", "udp", "rtu", "ascii", "tls"],
                        default="tcp", help="Transport (default: tcp).")
    parser.add_argument("--port", "-p", type=int, default=502,
                        help="TCP port (default: 502).")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="Connection timeout in seconds (default: 3.0).")
    parser.add_argument("--retries", type=int, default=0,
                        help="Extra connection attempts (default: 0).")
    parser.add_argument("--slave", type=int, default=1,
                        help="Slave/unit ID (default: 1).")
    parser.add_argument("--baudrate", "-b", type=int, default=9600,
                        help="Serial baud rate, RTU/ASCII only (default: 9600).")
    parser.add_argument("--parity", type=core.parse_parity, default="none",
                        help="Serial parity: none/even/odd (default: none).")
    parser.add_argument("--stopbits", type=float, default=1,
                        help="Serial stop bits (default: 1).")
    parser.add_argument("--bytesize", type=int, default=8,
                        help="Serial byte size (default: 8).")
    parser.add_argument("--cert", default=None, help="TLS client cert file (PEM).")
    parser.add_argument("--key", default=None, help="TLS client key file (PEM).")
    parser.add_argument("--no-verify", action="store_true",
                        help="TLS: skip certificate/hostname verification.")


def _add_data_args(parser) -> None:
    """Dtype/format options shared by read/watch/scan."""
    parser.add_argument("--dtype", "-d", choices=core.ALL_DTYPES, default="u16",
                        help="Data type (default: u16).")
    parser.add_argument("--format", "-f", choices=["dec", "hex", "bin"], default="dec",
                        help="Value display format (default: dec).")
    parser.add_argument("--byte-order", choices=["big", "little"], default="big",
                        help="Byte order for multi-register dtypes (default: big).")
    parser.add_argument("--word-order", choices=["big", "little"], default="big",
                        help="Word order for multi-register dtypes (default: big).")
    parser.add_argument("--encoding", default="utf-8",
                        help="Encoding for dtype str (default: utf-8).")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="Scale factor: read value *= scale; write raw = value / scale "
                             "(default: 1.0).")


def _add_address_arg(parser) -> None:
    """Positional ADDRESS with standard Modbus notation support."""
    parser.add_argument("address", type=core.parse_int_token,
                        help="Address (40001-49999 holding, 30001-39999 input, "
                             "10001-19999 discrete, 1-9999 coil, or raw 0-based).")


def _resolve_address(args) -> None:
    """Populate args.register_type/args.address (raw) from the ADDRESS token."""
    if getattr(args, "reg_type", None):
        args.register_type = args.reg_type
        # raw address passed through unchanged
    else:
        args.register_type, args.address = core.parse_modbus_address(args.address)


# ---------------------------------------------------------------------------
# client helpers
# ---------------------------------------------------------------------------


def _connect(args):
    """Build + connect a client. Returns client, or exits with EXIT_CONN."""
    target = f"{args.host}:{args.port}"
    if args.transport in ("rtu", "ascii"):
        target = args.host

    try:
        client = core.build_client(args)
    except Exception as exc:
        error_panel(f"Failed to build client: {exc}")
        raise SystemExit(core.EXIT_USAGE)

    if not core.connect_with_retries(client, args.retries, target, verbose=args.verbose):
        error_panel(f"Could not connect to {target} (slave {args.slave})")
        raise SystemExit(core.EXIT_CONN)
    return client


def _read_raw(client, args, raw_address: int, count: int):
    """Read ``count`` raw words/bits using the resolved register type."""
    kw = core.slave_kwarg(args.slave)
    if args.register_type == "holding":
        resp = client.read_holding_registers(raw_address, count=count, **kw)
    elif args.register_type == "input":
        resp = client.read_input_registers(raw_address, count=count, **kw)
    elif args.register_type == "coil":
        resp = client.read_coils(raw_address, count=count, **kw)
    else:  # discrete
        resp = client.read_discrete_inputs(raw_address, count=count, **kw)
    if resp.isError():
        raise core.ModbusException(str(resp))
    if args.register_type in ("coil", "discrete"):
        return list(resp.bits[:count])
    return list(resp.registers)


def _decode_values(raw: List[int], dtype: str, byte_order: str,
                   word_order: str, count: int, encoding: str = "utf-8") -> List:
    """Decode raw words into typed values (dtype-aware)."""
    if dtype == "str":
        # Treat the entire read as one string payload.
        return [core.registers_to_string(raw, byte_order, encoding)]
    width = core.get_dtype_width(dtype)
    values = []
    for i in range(0, count):
        chunk = raw[i * width:(i + 1) * width]
        values.append(core.registers_to_value(chunk, dtype, byte_order, word_order))
    return values


def _format_value(value, fmt: str, scale: float = 1.0) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if scale != 1.0:
        scaled = value * scale
        value = int(scaled) if scaled == int(scaled) else scaled
    if fmt == "hex":
        return f"0x{int(value):04X}"
    if fmt == "bin":
        return f"{int(value):016b}"
    if isinstance(value, float):
        return f"{value:.9g}"
    return str(value)


def _styled(value, style: str) -> str:
    """Wrap ``value`` in rich markup only when a style is given.

    A bare ``f"[]{x}[/]"`` (empty style) raises ``MarkupError: closing tag
    '[/]' has nothing to close`` and aborts the whole ``Live`` table render --
    which is why watch previously cleared the screen and showed nothing.
    """
    text = str(value)
    return f"[{style}]{text}[/]" if style else text


def _scaled_json_value(value, scale: float = 1.0):
    """Machine-readable value with scale applied (bools untouched).
    Integer dtypes stay integer when the scaled result is integral."""
    if isinstance(value, bool) or scale == 1.0:
        return value
    scaled = value * scale
    if isinstance(value, int):
        return int(scaled) if scaled == int(scaled) else scaled
    return scaled


def _bar_max_for_dtype(dtype: str) -> int:
    """Upper bound used to normalize the value bar for a given dtype."""
    if dtype in core.VALUE_RANGES:
        return core.VALUE_RANGES[dtype][1]
    return 65535


def _read_table(values, raw, start_address: int, args) -> Table:
    """Build the rich table for a read result."""
    table = Table(
        show_header=True,
        header_style=f"bold {core_color('primary')}",
        border_style=core_color("muted"),
        row_styles=["", "dim"],
        pad_edge=True,
        expand=False,
    )
    table.add_column("Address", style=f"bold {core_color('secondary')}",
                     justify="right", min_width=8)
    table.add_column("Value", style=f"bold {core_color('primary')}",
                     justify="right", min_width=12)
    table.add_column("Raw", style=core_color("text"), justify="right", min_width=10)
    if args.register_type not in ("coil", "discrete") and args.dtype != "str":
        table.add_column("Bar", min_width=22, no_wrap=True)

    is_bits = args.register_type in ("coil", "discrete")
    for i, val in enumerate(values):
        addr = start_address + i if is_bits else start_address + i
        raw_str = str(raw[i]) if not is_bits else ("1" if raw[i] else "0")
        row = [str(addr), _format_value(val, args.format, args.scale), raw_str]
        if args.register_type not in ("coil", "discrete") and args.dtype != "str":
            row.append(value_bar(int(val), _bar_max_for_dtype(args.dtype)))
        table.add_row(*row)
    return table


def core_color(name: str) -> str:
    """Return a palette color by name (import here to avoid theme->core cycle)."""
    from .theme import COLORS
    return COLORS[name]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_read(args) -> int:
    _resolve_address(args)
    rc = core.validate_args(args)
    if rc:
        return rc

    target = f"{args.host}:{args.port}"
    with console.status(f"[{core_color('primary')}]  Connecting to {target}...[/]", spinner="dots"):
        client = _connect(args)

    connection_header(target, args.register_type, args.slave)
    count = args.count
    is_bits = args.register_type in ("coil", "discrete")
    width = core.get_dtype_width(args.dtype)
    try:
        with console.status(f"[{core_color('primary')}]  Reading...[/]", spinner="dots"):
            if is_bits:
                raw = _read_raw(client, args, args.address, count)
                values = [bool(b) for b in raw]
            else:
                reg_count = count * width if args.dtype != "str" else count
                raw = _read_raw(client, args, args.address, reg_count)
                values = _decode_values(raw, args.dtype, args.byte_order,
                                        args.word_order, count, args.encoding)
    except core.ModbusException as exc:
        error_panel(f"Modbus error: {exc}")
        return core.EXIT_MBEXCEPTION
    finally:
        client.close()

    if args.json:
        payload = {
            "command": "read",
            "register_type": args.register_type,
            "transport": args.transport,
            "host": args.host,
            "target": target,
            "port": args.port,
            "unit_id": args.slave,
            "dtype": args.dtype,
            "byte_order": args.byte_order,
            "word_order": args.word_order,
            "scale": args.scale,
            "address": args.address,
            "count": count,
            "values": [
                {
                    "address": args.address + (i if is_bits else i * width),
                    "value": _scaled_json_value(v, args.scale),
                    "raw_value": v,
                    "registers": (raw[i * width:(i + 1) * width]
                                  if not is_bits and args.dtype != "str" else []),
                }
                for i, v in enumerate(values)
            ],
        }
        print(json.dumps(payload))
        return core.EXIT_SUCCESS

    console.print()
    table = _read_table(values, raw, args.address, args)
    console.print(Panel(
        table,
        border_style=core_color("muted"),
        title=f"[bold {core_color('primary')}]{args.register_type}[/] [dim]registers[/]",
        subtitle=f"[dim]{count} register(s) from {target}[/]",
        padding=(1, 2),
    ))
    console.print()
    return core.EXIT_SUCCESS


def cmd_write(args) -> int:
    _resolve_address(args)
    rc = core.validate_args(args)
    if rc:
        return rc
    if args.register_type not in ("holding", "coil"):
        error_panel(f"Cannot write to {args.register_type} registers (read-only)")
        return core.EXIT_USAGE

    target = f"{args.host}:{args.port}"
    with console.status(f"[{core_color('primary')}]  Connecting to {target}...[/]", spinner="dots"):
        client = _connect(args)

    connection_header(target, args.register_type, args.slave)
    kw = core.slave_kwarg(args.slave)
    try:
        if args.register_type == "coil":
            bits = [bool(int(v)) for v in args.values]
            if len(bits) == 1:
                resp = client.write_coil(args.address, bits[0], **kw)
            else:
                resp = client.write_coils(args.address, bits, **kw)
        else:
            if args.dtype == "str":
                words = core.string_to_registers(" ".join(args.values), args.encoding)
            else:
                words = []
                for v in args.values:
                    token = v
                    if args.scale != 1.0:
                        if args.dtype in core.INT_TYPES:
                            raw_val = int(v, 0) / args.scale
                            if raw_val != int(raw_val):
                                error_panel(
                                    f"Scaled value {v} / {args.scale} = {raw_val} "
                                    f"is not integral for {args.dtype}"
                                )
                                return core.EXIT_USAGE
                            token = int(raw_val)
                        else:
                            token = float(v) / args.scale
                    try:
                        words.extend(core.value_to_registers(token, args.dtype,
                                                             args.byte_order,
                                                             args.word_order))
                    except (ValueError, OverflowError) as exc:
                        error_panel(f"Value {token} cannot be encoded as {args.dtype}: {exc}")
                        return core.EXIT_USAGE
            if len(words) == 1:
                resp = client.write_register(args.address, words[0], **kw)
            else:
                resp = client.write_registers(args.address, words, **kw)

        if resp.isError():
            error_panel(f"Modbus error: {resp}")
            return core.EXIT_MBEXCEPTION
    except core.ModbusException as exc:
        error_panel(f"Modbus error: {exc}")
        return core.EXIT_MBEXCEPTION
    finally:
        client.close()

    vals = ", ".join(f"[bold {core_color('primary')}]{v}[/]" for v in args.values)
    success_panel(
        f"Wrote [{vals}] to {args.register_type} register(s) "
        f"starting at [bold {core_color('secondary')}]{args.address}[/]"
    )
    console.print()
    return core.EXIT_SUCCESS


def cmd_fill(args) -> int:
    _resolve_address(args)
    rc = core.validate_args(args)
    if rc:
        return rc
    if args.register_type not in ("holding", "coil"):
        error_panel(f"Cannot fill {args.register_type} registers (read-only)")
        return core.EXIT_USAGE

    target = f"{args.host}:{args.port}"
    with console.status(f"[{core_color('primary')}]  Connecting to {target}...[/]", spinner="dots"):
        client = _connect(args)

    connection_header(target, args.register_type, args.slave)
    kw = core.slave_kwarg(args.slave)

    # Determine fill count
    if args.all:
        if args.register_type == "holding":
            fill_count = 100
        elif args.register_type == "coil":
            fill_count = 32
        else:
            fill_count = 100
    else:
        fill_count = args.count

    try:
        if args.register_type == "coil":
            bit_val = bool(int(args.value))
            bits = [bit_val] * fill_count
            if fill_count == 1:
                resp = client.write_coil(args.address, bits[0], **kw)
            else:
                resp = client.write_coils(args.address, bits, **kw)
        else:  # holding
            token = args.value
            if args.scale != 1.0:
                if args.dtype in core.INT_TYPES:
                    raw_val = int(token, 0) / args.scale
                    if raw_val != int(raw_val):
                        error_panel(
                            f"Scaled value {token} / {args.scale} = {raw_val} "
                            f"is not integral for {args.dtype}"
                        )
                        return core.EXIT_USAGE
                    token = int(raw_val)
                else:
                    token = float(token) / args.scale
            words_per_value = core.get_dtype_width(args.dtype)
            words = core.value_to_registers(token, args.dtype, args.byte_order, args.word_order)
            # Repeat the word pattern for each count slot
            all_words = words * fill_count
            if len(all_words) == 1:
                resp = client.write_register(args.address, all_words[0], **kw)
            else:
                resp = client.write_registers(args.address, all_words, **kw)

        if resp.isError():
            error_panel(f"Modbus error: {resp}")
            return core.EXIT_MBEXCEPTION
    except core.ModbusException as exc:
        error_panel(f"Modbus error: {exc}")
        return core.EXIT_MBEXCEPTION
    finally:
        client.close()

    scope = "entire block" if args.all else f"{fill_count} register(s)"
    success_panel(
        f"Filled {args.register_type} {scope} starting at "
        f"[bold {core_color('secondary')}]{args.address}[/] with value "
        f"[bold {core_color('primary')}]{args.value}[/]"
    )
    console.print()
    return core.EXIT_SUCCESS


def cmd_save(args) -> int:
    if getattr(args, "reg_type", None):
        args.register_type = args.reg_type
    rc = core.validate_args(args)
    if rc:
        return rc

    target = f"{args.host}:{args.port}"
    with console.status(f"[{core_color('primary')}]  Connecting to {target}...[/]", spinner="dots"):
        client = _connect(args)

    connection_header(target, args.register_type or "all", args.slave)
    kw = core.slave_kwarg(args.slave)

    # Define register types to read
    all_types = ["holding", "input", "coil", "discrete"]
    if args.register_type:
        types_to_read = [args.register_type]
    else:
        types_to_read = all_types

    result = {
        "host": args.host,
        "port": args.port,
        "slave": args.slave,
        "ts": time.time(),
        "format": 1,
        "disabled_types": [],
    }

    MAX_CHUNK = 125

    try:
        for reg_type in types_to_read:
            args.register_type = reg_type
            if reg_type in ("holding", "input"):
                # Read in chunks of up to 125 registers
                total_count = 100  # Default block size
                values = []
                for offset in range(0, total_count, MAX_CHUNK):
                    chunk_size = min(MAX_CHUNK, total_count - offset)
                    raw = _read_raw(client, args, offset, chunk_size)
                    values.extend(raw)
                result[reg_type] = {"start": 0, "count": len(values), "values": values}
            else:  # coil or discrete
                total_count = 32  # Default block size for coils
                values = []
                for offset in range(0, total_count, MAX_CHUNK):
                    chunk_size = min(MAX_CHUNK, total_count - offset)
                    raw = _read_raw(client, args, offset, chunk_size)
                    values.extend([1 if b else 0 for b in raw])
                result[reg_type] = {"start": 0, "count": len(values), "values": values}

        # Write JSON file
        with open(args.path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    except core.ModbusException as exc:
        error_panel(f"Modbus error: {exc}")
        return core.EXIT_MBEXCEPTION
    except OSError as exc:
        error_panel(f"File error: {exc}")
        return core.EXIT_ERROR
    finally:
        client.close()

    # Build summary
    parts = []
    for reg_type in types_to_read:
        if reg_type in result:
            parts.append(f"{reg_type}={result[reg_type]['count']}")
    summary = ", ".join(parts)
    success_panel(f"Saved snapshot to [bold {core_color('secondary')}]{args.path}[/] ({summary})")
    console.print()
    return core.EXIT_SUCCESS


def cmd_restore(args) -> int:
    if getattr(args, "reg_type", None):
        args.register_type = args.reg_type
    # Only writable types allowed
    if args.register_type and args.register_type not in ("holding", "coil"):
        error_panel(f"Cannot restore to {args.register_type} registers (read-only)")
        return core.EXIT_USAGE

    rc = core.validate_args(args)
    if rc:
        return rc

    # Read JSON file
    try:
        with open(args.path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        error_panel(f"Failed to read snapshot: {exc}")
        return core.EXIT_ERROR

    target = f"{args.host}:{args.port}"
    with console.status(f"[{core_color('primary')}]  Connecting to {target}...[/]", spinner="dots"):
        client = _connect(args)

    connection_header(target, args.register_type or "all", args.slave)
    kw = core.slave_kwarg(args.slave)

    MAX_CHUNK = 125
    restored_types = []
    skipped_types = []

    try:
        for reg_type in ("holding", "coil"):
            if args.register_type and args.register_type != reg_type:
                continue
            if reg_type not in data:
                continue
            reg_data = data[reg_type]
            values = reg_data.get("values", [])
            start = reg_data.get("start", 0)
            count = reg_data.get("count", len(values))

            if not values:
                continue

            if reg_type == "holding":
                # Write in chunks
                for offset in range(0, count, MAX_CHUNK):
                    chunk = values[offset:offset + MAX_CHUNK]
                    resp = client.write_registers(start + offset, chunk, **kw)
                    if resp.isError():
                        error_panel(f"Modbus error writing {reg_type}: {resp}")
                        return core.EXIT_MBEXCEPTION
                restored_types.append(f"{reg_type}={count}")
            else:  # coil
                bits = [bool(v) for v in values]
                for offset in range(0, count, MAX_CHUNK):
                    chunk = bits[offset:offset + MAX_CHUNK]
                    resp = client.write_coils(start + offset, chunk, **kw)
                    if resp.isError():
                        error_panel(f"Modbus error writing {reg_type}: {resp}")
                        return core.EXIT_MBEXCEPTION
                restored_types.append(f"{reg_type}={count}")

        # Check for read-only types in snapshot
        for reg_type in ("input", "discrete"):
            if reg_type in data:
                skipped_types.append(reg_type)

    except core.ModbusException as exc:
        error_panel(f"Modbus error: {exc}")
        return core.EXIT_MBEXCEPTION
    finally:
        client.close()

    msg = f"Restored from [bold {core_color('secondary')}]{args.path}[/]"
    if restored_types:
        msg += f" ({', '.join(restored_types)})"
    if skipped_types:
        msg += f" — skipped read-only: {', '.join(skipped_types)}"
    success_panel(msg)
    console.print()
    return core.EXIT_SUCCESS


def cmd_scan(args) -> int:
    if getattr(args, "reg_type", None):
        args.register_type = args.reg_type
    rc = core.validate_args(args)
    if rc:
        return rc

    if args.dtype in core.FLOAT_TYPES and args.format in ("hex", "bin"):
        error_panel(f"Format {args.format} not allowed for float types")
        return core.EXIT_USAGE

    target = f"{args.host}:{args.port}"
    with console.status(f"[{core_color('primary')}]  Connecting to {target}...[/]", spinner="dots"):
        client = _connect(args)

    width = core.get_dtype_width(args.dtype)
    total = args.end - args.start + 1
    chunk_regs = (125 // width) * width or width  # FC3/FC4 max, width-aligned
    found = []
    try:
        with console.status(f"[{core_color('primary')}]  Scanning {args.start}-{args.end}...[/]", spinner="dots"):
            offset = 0
            while offset < total:
                read_regs = min(chunk_regs, total - offset)
                raw = _read_raw(client, args, args.start + offset, read_regs)
                read_count = read_regs // width
                for i in range(read_count):
                    chunk = raw[i * width:(i + 1) * width]
                    if len(chunk) < width:
                        break
                    val = core.registers_to_value(chunk, args.dtype,
                                                  args.byte_order, args.word_order)
                    abs_addr = args.start + offset + i * width
                    if args.all or int(val) != 0:
                        found.append((abs_addr, chunk, val))
                offset += read_regs
    except core.ModbusException as exc:
        error_panel(f"Modbus error: {exc}")
        return core.EXIT_MBEXCEPTION
    finally:
        client.close()

    if args.json:
        print(json.dumps({
            "command": "scan",
            "register_type": args.register_type,
            "transport": args.transport,
            "host": args.host,
            "target": target,
            "port": args.port,
            "unit_id": args.slave,
            "dtype": args.dtype,
            "byte_order": args.byte_order,
            "word_order": args.word_order,
            "scale": args.scale,
            "start": args.start, "end": args.end,
            "found": [
                {"address": a, "value": _scaled_json_value(v, args.scale),
                 "raw_value": v, "registers": regs}
                for a, regs, v in found
            ],
        }))
        return core.EXIT_SUCCESS

    console.print()
    if found:
        table = Table(show_header=True, header_style=f"bold {core_color('primary')}",
                      border_style=core_color("muted"), row_styles=["", "dim"])
        table.add_column("Address", style=f"bold {core_color('secondary')}", justify="right")
        table.add_column("Raw", style=core_color("text"), justify="right")
        table.add_column("Value", style=core_color("primary"), justify="right")
        table.add_column("Bar", min_width=22, no_wrap=True)
        for addr, regs, val in found:
            raw_hex = ", ".join(f"0x{r:04X}" for r in regs)
            table.add_row(str(addr), raw_hex, _format_value(val, args.format, args.scale),
                          value_bar(int(val), _bar_max_for_dtype(args.dtype)))
        console.print(Panel(
            table,
            border_style=core_color("success"),
            title=f"[bold {core_color('success')}]  {len(found)} register(s)[/]",
            subtitle=f"[dim]scanned {target} {args.start}-{args.end}[/]",
            padding=(1, 2),
        ))
    else:
        console.print(Panel(
            f"[{core_color('warning')}]No non-zero registers in range {args.start}-{args.end}[/]",
            border_style=core_color("warning"),
            title=f"[bold {core_color('warning')}]scan complete[/]",
            padding=(0, 1),
        ))
    console.print()
    return core.EXIT_SUCCESS


def _watch_stream(client, args, count: int, reg_count: int, is_bits: bool,
                  width: int, end_addr: int) -> int:
    out_file = None
    if args.output:
        try:
            out_file = open(args.output, "a", encoding="utf-8")
        except OSError as exc:
            error_panel(f"Cannot open output file {args.output}: {exc}")
            return core.EXIT_ERROR

    stop_desc = ("Ctrl-C to stop" if not args.iterations
                 else f"stop after {args.iterations} poll(s)")

    def emit(line: str) -> None:
        print(line, flush=True)
        if out_file is not None:
            print(line, file=out_file, flush=True)

    print(f"[watch] monitoring {args.register_type} range {args.address}..{end_addr} on "
          f"{args.host}:{args.port}, interval {args.interval}s ({stop_desc})",
          file=sys.stderr)

    previous = {}
    poll = 0
    consecutive_errors = 0

    try:
        while True:
            if args.iterations and poll >= args.iterations:
                break
            poll += 1
            stamp = time.strftime('%H:%M:%S')

            try:
                if is_bits:
                    raw = _read_raw(client, args, args.address, count)
                    current = {args.address + i: (bool(b), []) for i, b in enumerate(raw)}
                elif args.dtype == 'str':
                    raw = _read_raw(client, args, args.address, reg_count)
                    text = core.registers_to_string(raw, args.byte_order, args.encoding)
                    current = {args.address: (text, list(raw))}
                else:
                    raw = _read_raw(client, args, args.address, reg_count)
                    current = {}
                    for i in range(count):
                        chunk = raw[i * width:(i + 1) * width]
                        current[args.address + i * width] = (
                            core.registers_to_value(chunk, args.dtype,
                                                    args.byte_order, args.word_order),
                            list(chunk),
                        )
            except core.ModbusIOException as exc:
                consecutive_errors += 1
                print(f"[watch] poll {poll} read error: {exc}", file=sys.stderr)
                if consecutive_errors >= 10:
                    return core.EXIT_IO
                if not (args.iterations and poll >= args.iterations):
                    time.sleep(args.interval)
                continue
            except core.ModbusException as exc:
                consecutive_errors += 1
                print(f"[watch] poll {poll} modbus error: {exc}", file=sys.stderr)
                if consecutive_errors >= 10:
                    return core.EXIT_MBEXCEPTION
                if not (args.iterations and poll >= args.iterations):
                    time.sleep(args.interval)
                continue

            consecutive_errors = 0

            changes = []
            for addr in sorted(current):
                raw_val, regs = current[addr]
                prev_raw = previous.get(addr)
                changed = prev_raw is None or prev_raw != raw_val
                if args.all or changed:
                    changes.append((addr, prev_raw if changed else None, raw_val, regs))
            previous = {addr: raw for addr, (raw, _regs) in current.items()}

            if args.json:
                payload = {
                    "command": "watch",
                    "register_type": args.register_type,
                    "transport": args.transport,
                    "host": args.host,
                    "port": args.port,
                    "unit_id": args.slave,
                    "dtype": args.dtype,
                    "byte_order": args.byte_order,
                    "word_order": args.word_order,
                    "scale": args.scale,
                    "poll": poll,
                    "ts": time.time(),
                }
                json_changes = []
                for addr, prev_raw, raw_val, regs in changes:
                    if is_bits:
                        value = bool(raw_val)
                        previous_val = None if prev_raw is None else bool(prev_raw)
                    elif args.dtype == 'str':
                        value = raw_val
                        previous_val = prev_raw
                    else:
                        value = _scaled_json_value(raw_val, args.scale)
                        previous_val = None if prev_raw is None else _scaled_json_value(prev_raw, args.scale)
                    json_changes.append({
                        "address": addr,
                        "value": value,
                        "previous": previous_val,
                        "raw_value": raw_val,
                        "registers": regs,
                    })
                payload["changes"] = json_changes
                emit(json.dumps(payload))
            else:
                if changes:
                    emit(f"[{stamp}] poll {poll}: {len(changes)} change(s)")
                    for addr, prev_raw, raw_val, regs in changes:
                        if is_bits:
                            label = "Coil" if args.register_type == 'coil' else "Discrete"
                            cur = 'ON' if raw_val else 'OFF'
                            prev_s = '' if prev_raw is None else f"{'ON' if prev_raw else 'OFF'} -> "
                            emit(f"{label} {addr}: {prev_s}{cur}")
                        else:
                            cur_s = _format_value(raw_val, args.format, args.scale)
                            prev_s = '' if prev_raw is None else f"{_format_value(prev_raw, args.format, args.scale)} -> "
                            tag = "" if args.dtype == 'u16' else f" ({args.dtype})"
                            raw_hex = (f" [raw: {', '.join(f'0x{r:04X}' for r in regs)}]"
                                       if (args.dtype == 'str' or width > 1) else "")
                            emit(f"Register {addr}{tag}: {prev_s}{cur_s}{raw_hex}")

            if args.iterations and poll >= args.iterations:
                break
            time.sleep(args.interval)

        return core.EXIT_SUCCESS
    except KeyboardInterrupt:
        print("[watch] stopped (Ctrl-C)", file=sys.stderr)
        return core.EXIT_SUCCESS
    finally:
        if out_file is not None:
            out_file.close()


def cmd_watch(args) -> int:
    _resolve_address(args)
    rc = core.validate_args(args)
    if rc:
        return rc

    target = f"{args.host}:{args.port}"
    with console.status(f"[{core_color('primary')}]  Connecting to {target}...[/]", spinner="dots"):
        client = _connect(args)

    connection_header(target, args.register_type, args.slave)
    count = args.count
    is_bits = args.register_type in ("coil", "discrete")
    width = core.get_dtype_width(args.dtype)
    reg_count = count * width if args.dtype != "str" else count
    end_addr = args.address + (count if is_bits else reg_count) - 1

    if args.json or args.output:
        try:
            return _watch_stream(client, args, count, reg_count, is_bits, width, end_addr)
        finally:
            client.close()

    prev: Optional[List] = None
    iterations = args.iterations
    poll = 0
    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            while iterations == 0 or poll < iterations:
                if args.register_type in ("coil", "discrete"):
                    raw = _read_raw(client, args, args.address, count)
                    values = [bool(b) for b in raw]
                else:
                    width = core.get_dtype_width(args.dtype)
                    reg_count = count * width if args.dtype != "str" else count
                    raw = _read_raw(client, args, args.address, reg_count)
                    values = _decode_values(raw, args.dtype, args.byte_order,
                                            args.word_order, count, args.encoding)

                table = Table(show_header=True,
                              header_style=f"bold {core_color('primary')}",
                              border_style=core_color("muted"),
                              row_styles=["", "dim"])
                table.add_column("Address", style=f"bold {core_color('secondary')}",
                                 justify="right", min_width=8)
                table.add_column("Value", style=f"bold {core_color('primary')}",
                                 justify="right", min_width=12)
                table.add_column("Raw", style=core_color("text"), justify="right")
                table.add_column("Δ", justify="right", min_width=8)
                if not is_bits and args.dtype != "str":
                    table.add_column("Bar", min_width=22, no_wrap=True)

                for i, val in enumerate(values):
                    addr = args.address + i
                    changed = prev is not None and prev[i] != val
                    delta = ""
                    if prev is not None and not isinstance(val, bool):
                        d = int(val) - int(prev[i])
                        delta = f"{d:+d}" if d else ""
                    style = f"bold {core_color('changed')}" if changed else ""
                    row = [_styled(addr, style),
                           _styled(_format_value(val, args.format, args.scale), style),
                           str(raw[i]) if not is_bits else ("1" if raw[i] else "0"),
                           _styled(delta, style)]
                    if not is_bits and args.dtype != "str":
                        row.append(value_bar(int(val), _bar_max_for_dtype(args.dtype)))
                    table.add_row(*row)

                prev = list(values)
                poll += 1
                table.title = (f"[bold {core_color('primary')}]{args.register_type}[/] "
                               f"[dim]watch[/] [dim]{target}[/] [dim]poll {poll}[/]")
                live.update(table)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    except core.ModbusException as exc:
        error_panel(f"Modbus error: {exc}")
        return core.EXIT_MBEXCEPTION
    finally:
        client.close()
    console.print()
    return core.EXIT_SUCCESS


def cmd_simulate(args) -> int:
    argv = ["--host", args.host, "--port", str(args.port)]
    if hasattr(args, "slaves"):
        argv.extend(["--slaves", str(args.slaves)])
    if hasattr(args, "start_unit"):
        argv.extend(["--start-unit", str(args.start_unit)])
    if hasattr(args, "transports"):
        argv.extend(["--transports", args.transports])
    if hasattr(args, "log_connections") and args.log_connections:
        argv.extend(["--log-connections", args.log_connections])
    if hasattr(args, "log_level"):
        argv.extend(["--log-level", args.log_level])
    if getattr(args, "no_banner", False):
        argv.append("--no-banner")
    return simulator_main(argv)


def cmd_tui(args) -> int:
    """Enter the interactive console shell (lazy import keeps cli import light
    and avoids a module-level cycle with modbus.tui)."""
    from .tui import run_shell

    return run_shell(args)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _build_parser():
    parser = argparse_import().ArgumentParser(
        prog=PROG,
        description="MODBUS for dummies. TCP/UDP/RTU/ASCII/TLS + rich terminal UI.",
    )
    parser.add_argument("--no-banner", action="store_true",
                        help="Suppress the ASCII banner.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # read
    p = sub.add_parser("read", help="Read registers or coils.")
    _add_connection_args(p)
    _add_data_args(p)
    p.add_argument("host", help="Host or serial device.")
    _add_address_arg(p)
    p.add_argument("--count", "-c", type=int, default=1, help="Number of values (default: 1).")
    p.add_argument("--type", "-t", dest="reg_type",
                   choices=["holding", "input", "coil", "discrete"], default=None,
                   help="Register type override (default: auto-detect from address).")
    p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")
    p.add_argument("--verbose", action="store_true", help="Verbose connect logging.")
    p.set_defaults(func=cmd_read)

    # write
    p = sub.add_parser("write", help="Write registers or coils.")
    _add_connection_args(p)
    _add_data_args(p)
    p.add_argument("host", help="Host or serial device.")
    _add_address_arg(p)
    p.add_argument("values", type=str, nargs="+", help="Value(s) to write.")
    p.add_argument("--type", "-t", dest="reg_type",
                   choices=["holding", "coil"], default=None,
                   help="Register type override (default: auto-detect from address).")
    p.add_argument("--verbose", action="store_true", help="Verbose connect logging.")
    p.set_defaults(func=cmd_write)

    # fill
    p = sub.add_parser("fill", help="Fill registers or coils with a single value.")
    _add_connection_args(p)
    _add_data_args(p)
    p.add_argument("host", help="Host or serial device.")
    _add_address_arg(p)
    p.add_argument("value", nargs="?", default="0", help="Value to fill (default: 0).")
    p.add_argument("--count", "-c", type=int, default=1, help="Number of values to write (default: 1).")
    p.add_argument("--all", action="store_true", help="Fill entire block (100 regs holding, 32 coils).")
    p.add_argument("--type", "-t", dest="reg_type",
                   choices=["holding", "coil"], default=None,
                   help="Register type override (default: auto-detect from address).")
    p.add_argument("--verbose", action="store_true", help="Verbose connect logging.")
    p.set_defaults(func=cmd_fill)

    # save
    p = sub.add_parser("save", help="Save slave register state to a JSON file.")
    _add_connection_args(p)
    p.add_argument("host", help="Host or serial device.")
    p.add_argument("path", help="Output JSON file path.")
    p.add_argument("--type", "-t", dest="reg_type",
                   choices=["holding", "input", "coil", "discrete"], default=None,
                   help="Register type to save (default: all types).")
    p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")
    p.add_argument("--verbose", action="store_true", help="Verbose connect logging.")
    p.set_defaults(func=cmd_save)

    # restore
    p = sub.add_parser("restore", help="Restore slave register state from a JSON file.")
    _add_connection_args(p)
    p.add_argument("host", help="Host or serial device.")
    p.add_argument("path", help="Input JSON file path.")
    p.add_argument("--type", "-t", dest="reg_type",
                   choices=["holding", "coil"], default=None,
                   help="Register type to restore (default: all writable types).")
    p.add_argument("--verbose", action="store_true", help="Verbose connect logging.")
    p.set_defaults(func=cmd_restore)

    # scan
    p = sub.add_parser("scan", help="Scan an address range for non-zero registers.")
    _add_connection_args(p)
    _add_data_args(p)
    p.add_argument("--start", type=core.parse_int_token, default=0,
                   help="Start address (default: 0, decimal or 0x hex).")
    p.add_argument("--end", type=core.parse_int_token, default=99,
                   help="End address (default: 99, decimal or 0x hex).")
    p.add_argument("--all", action="store_true",
                   help="Show all values including zeros.")
    p.add_argument("--type", "-t", dest="reg_type", default="holding",
                   choices=["holding", "input"], help="Register type (default: holding).")
    p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")
    p.add_argument("--verbose", action="store_true", help="Verbose connect logging.")
    p.add_argument("host", help="Host or serial device.")
    p.set_defaults(func=cmd_scan)

    # watch
    p = sub.add_parser("watch", help="Poll registers at an interval (Ctrl+C to quit).")
    _add_connection_args(p)
    _add_data_args(p)
    p.add_argument("host", help="Host or serial device.")
    _add_address_arg(p)
    p.add_argument("--count", "-c", type=int, default=1, help="Number of values (default: 1).")
    p.add_argument("--type", "-t", dest="reg_type",
                   choices=["holding", "input", "coil", "discrete"], default=None,
                   help="Register type override (default: auto-detect from address).")
    p.add_argument("--interval", "-i", type=float, default=1.0,
                   help="Poll interval in seconds (default: 1.0).")
    p.add_argument("--iterations", type=int, default=0,
                   help="Poll count; 0 = infinite (default: 0).")
    p.add_argument("--all", action="store_true",
                   help="Show every value every poll, not only changes.")
    p.add_argument("--json", action="store_true",
                   help="Stream one JSON object per poll on stdout.")
    p.add_argument("--output", default=None,
                   help="Append each poll's output to this file (implies stream mode).")
    p.add_argument("--verbose", action="store_true", help="Verbose connect logging.")
    p.set_defaults(func=cmd_watch)

    # simulate
    p = sub.add_parser("simulate", help="Launch the embedded test server.")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    p.add_argument("--port", "-p", type=int, default=5021,
                   help="TCP port (default: 5021).")
    p.add_argument("--slaves", type=int, default=3,
                   help="Number of slave units (default: 3).")
    p.add_argument("--start-unit", type=int, default=1,
                   help="Starting unit ID (default: 1).")
    p.add_argument("--transports", default="tcp",
                   help="Comma-separated transports: tcp,udp (default: tcp).")
    p.add_argument("--log-connections", metavar="PATH",
                   help="Optional file to append connect/disconnect events.")
    p.add_argument("--log-level", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                   help="Logging level for pymodbus (default: WARNING).")
    p.add_argument("--no-banner", action="store_true", help="Suppress banner.")
    p.set_defaults(func=cmd_simulate)

    # tui
    p = sub.add_parser(
        "tui", help="Interactive console shell (REPL).")
    _add_connection_args(p)
    _add_data_args(p)
    p.add_argument("host", nargs="?", default="127.0.0.1",
                   help="Initial target host or serial device "
                        "(default: 127.0.0.1; change later with 'set host').")
    p.set_defaults(func=cmd_tui)

    return parser


def argparse_import():
    """Deferred import keeps module importable without argparse at module load
    (argparse is stdlib, this is just an indirection seam for tests)."""
    import argparse
    return argparse


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    banner_suppressed = (
        getattr(args, "no_banner", False)
        or bool(os.environ.get("MODBUS_CLI_NO_BANNER"))
        or bool(os.environ.get("MODBUS_NO_BANNER"))
        or bool(os.environ.get("NO_BANNER"))
    )

    if not getattr(args, "command", None):
        if not banner_suppressed:
            banner()
        parser.print_help()
        return core.EXIT_SUCCESS

    if not banner_suppressed:
        banner()

    # core.build_client() reads args.ip; argparse stored the positional as host.
    if hasattr(args, "host") and not hasattr(args, "ip"):
        args.ip = args.host

    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print()
        return core.EXIT_SUCCESS
    except core.ModbusIOException as exc:
        error_panel(f"I/O error: {exc}")
        return core.EXIT_IO
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        error_panel(f"Unexpected error: {exc}")
        return core.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

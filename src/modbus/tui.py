"""modbus.tui -- interactive console shell (``modbus tui``).

Presents the whole CLI as a live REPL with support for multiple named
connections and slaves.  All protocol/decoding/validation logic is
delegated back to :mod:`modbus.cli` so the shell can never drift from the
one-shot command behaviour.

Shell grammar (``modbus>`` prompt, ``help`` for the full list)::

    connect [name|]host:port       create/switch connection (e.g. plc1|10.0.0.5:502)
    connect <name> host [port]     create/switch with explicit host/port
    use <name>                     switch active connection
    list                          show all connections
    disconnect [name|host[:port]]  close a connection (default: active)
    slave <id>                    set default slave for active connection
    status                        show active connection state
    set <key> <value>             set a session/connection option   (see below)
    read  [type] <addr> [n]       read registers/coils          e.g. read 40001
    write [type] <addr> v...      write registers/coils         e.g. write 16 2
    scan  [start [end]]           scan a range for non-zeros    e.g. scan 0 20
    watch [type] <addr> [n]       live dashboard until Ctrl-C   e.g. watch 0 6
    ls [path]                     list directory contents
    clear | cls                   clear the terminal screen
    pwd                           print working directory
    cd [dir]                      change working directory
    !<command>                    run a shell command
    help                          show this text
    exit | quit | bye             leave the shell (Ctrl-D works too)

  <type> is one of: holding, input, coil, discrete (auto-detect if omitted).
  <addr> accepts standard notation (40001..49999 holding, 30001 input,
  10001 discrete, 1..9999 coil) or a raw 0-based offset.

  Per-command flags override session/connection options for one command only:
    read 0 4 --dtype f32 --format hex --slave 2 --connection backup
    watch 0 4 --interval 0.5 --iterations 20
    scan 0 50 --all

  Global session options (apply to all connections):
    dtype format byte_order word_order encoding scale regtype

  Per-connection options (apply to one connection):
    host port transport timeout retries slave baudrate parity stopbits
    bytesize cert key no_verify
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cli, core
from .theme import console, error_panel, success_panel

# Session persistence
SESSION_FILE = Path.home() / ".modbus_session.json"


def _save_session(session: "Session") -> bool:
    """Save session state to JSON file."""
    try:
        data = {
            "global": {
                "dtype": session.dtype,
                "format": session.format,
                "byte_order": session.byte_order,
                "word_order": session.word_order,
                "encoding": session.encoding,
                "scale": session.scale,
                "regtype": session.regtype,
            },
            "connections": {},
            "active_name": session.active_name,
        }
        for name, conn in session.connections.items():
            data["connections"][name] = {
                "name": conn.name,
                "host": conn.host,
                "port": conn.port,
                "transport": conn.transport,
                "timeout": conn.timeout,
                "retries": conn.retries,
                "slave": conn.slave,
                "baudrate": conn.baudrate,
                "parity": conn.parity,
                "stopbits": conn.stopbits,
                "bytesize": conn.bytesize,
                "cert": conn.cert,
                "key": conn.key,
                "no_verify": conn.no_verify,
            }
        SESSION_FILE.write_text(json.dumps(data, indent=2))
        return True
    except Exception:
        return False


def _load_session() -> Optional[dict]:
    """Load session state from JSON file."""
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return None


# Registered (type-word, canonical register-type) pairs a command accepts.
_REGISTER_TYPES = {
    "holding": "holding",
    "input": "input",
    "coil": "coil",
    "discrete": "discrete",
    "hr": "holding",
    "ir": "input",
}

# Settable *global* session options (not connection-specific).
_GLOBAL_SETTABLE: Dict[str, Tuple[Callable[[str], Any], str]] = {
    "dtype": (lambda v: v if v in core.ALL_DTYPES
              else (_raise(f"dtype must be one of {', '.join(core.ALL_DTYPES)}")),
              "u16/i16/u32/i32/u64/i64/f32/f64/str"),
    "format": (lambda v: v if v in ("dec", "hex", "bin")
               else (_raise("format must be dec/hex/bin")),
               "dec/hex/bin"),
    "byte_order": (lambda v: v if v in ("big", "little")
                   else (_raise("byte_order must be big/little")), "big/little"),
    "word_order": (lambda v: v if v in ("big", "little")
                   else (_raise("word_order must be big/little")), "big/little"),
    "encoding": (lambda v: v, "text encoding for dtype str"),
    "scale": (lambda v: float(v), "scale factor for values"),
    "regtype": (lambda v: v if v in _REGISTER_TYPES
                else (_raise("regtype must be holding/input/coil/discrete")),
                "default register type (holding/input/coil/discrete)"),
}

# Settable *per-connection* options.
_CONNECTION_SETTABLE: Dict[str, Tuple[Callable[[str], Any], str]] = {
    "host": (lambda v: v, "target host or serial device"),
    "port": (lambda v: int(v), "TCP port"),
    "transport": (lambda v: v if v in ("tcp", "udp", "rtu", "ascii", "tls")
                  else (_raise("transport must be one of tcp/udp/rtu/ascii/tls")),
                  "tcp/udp/rtu/ascii/tls"),
    "timeout": (lambda v: float(v), "connection timeout (seconds)"),
    "retries": (lambda v: int(v), "extra connection attempts"),
    "slave": (lambda v: int(v), "default slave/unit ID"),
    "baudrate": (lambda v: int(v), "serial baud rate"),
    "parity": (core.parse_parity, "serial parity: none/even/odd"),
    "stopbits": (lambda v: float(v), "serial stop bits"),
    "bytesize": (lambda v: int(v), "serial byte size"),
    "cert": (lambda v: v, "TLS client cert file (PEM)"),
    "key": (lambda v: v, "TLS client key file (PEM)"),
    "no_verify": (lambda v: v.lower() in ("1", "true", "yes", "on"),
                  "TLS: skip certificate/hostname verification (true/false)"),
}

# Combined settable options for `set` command listing.
_SETTABLE = {**_GLOBAL_SETTABLE, **_CONNECTION_SETTABLE}


def _raise(msg: str) -> Any:
    raise ValueError(msg)


# Per-command long flags shared by read/watch (dtype/format/type) plus the
# command-specific ones.  value None means a store-true switch.
_FLAGS: Dict[str, Dict[str, Tuple[str, Any]]] = {
    "read": {
        "--dtype": ("dtype", None), "--format": ("format", None),
        "--type": ("reg_type", None), "--count": ("count", None),
        "--byte-order": ("byte_order", None), "--word-order": ("word_order", None),
        "--scale": ("scale", None), "--slave": ("slave", None),
        "--connection": ("connection", None),
    },
    "write": {
        "--dtype": ("dtype", None), "--type": ("reg_type", None),
        "--byte-order": ("byte_order", None), "--word-order": ("word_order", None),
        "--scale": ("scale", None), "--slave": ("slave", None),
        "--connection": ("connection", None),
    },
    "scan": {
        "--dtype": ("dtype", None), "--format": ("format", None),
        "--start": ("start", None), "--end": ("end", None),
        "--all": ("all", True), "--slave": ("slave", None),
        "--connection": ("connection", None),
    },
    "watch": {
        "--dtype": ("dtype", None), "--format": ("format", None),
        "--type": ("reg_type", None), "--count": ("count", None),
        "--interval": ("interval", None), "--iterations": ("iterations", None),
        "--all": ("all", True), "--json": ("json", True),
        "--output": ("output", None), "--slave": ("slave", None),
        "--connection": ("connection", None),
    },
    "fill": {
        "--dtype": ("dtype", None), "--format": ("format", None),
        "--type": ("reg_type", None), "--count": ("count", None),
        "--all": ("all", True), "--slave": ("slave", None),
        "--connection": ("connection", None),
        "--byte-order": ("byte_order", None), "--word-order": ("word_order", None),
        "--scale": ("scale", None), "--encoding": ("encoding", None),
    },
    "save": {
        "--slave": ("slave", None), "--type": ("reg_type", None),
        "--connection": ("connection", None),
    },
    "restore": {
        "--slave": ("slave", None), "--type": ("reg_type", None),
        "--connection": ("connection", None),
    },
}

_HELP = """\
Modbus interactive shell commands:

=== Connection Management ===
  connect <name> host [port]     create/switch connection
  use <name>                     switch active connection
  list                          show all connections
  disconnect [name|host[:port]]  close a connection (default: active)
  slave <id>                    set default slave for active connection
  status                        show active connection state

=== Modbus Operations ===
  read  [type] <addr> [n]       read registers/coils          e.g. read 40001
  write [type] <addr> v...      write registers/coils         e.g. write 16 2
  scan  [start [end]]           scan a range for non-zeros    e.g. scan 0 20
  watch [type] <addr> [n]       live dashboard until Ctrl-C   e.g. watch 0 6
  fill  [type] <addr> <val> [n] write a value (default 0), use --all for all regs
  save  <path>                  save register state to JSON file
  restore <path>                restore register state from JSON file

=== Session Options ===
  set <key> <value>             set a session/connection option   (see below)

=== Shell Commands ===
  ls [path]                     list directory contents
  clear | cls                   clear the terminal screen
  pwd                           print working directory
  cd [dir]                      change working directory
  !<command>                    run a shell command

=== Meta ===
  help                          show this text
  exit | quit | bye             leave the shell (Ctrl-D works too)

  <type> is one of: holding, input, coil, discrete (auto-detect if omitted).
  <addr> accepts standard notation (40001..49999 holding, 30001 input,
  10001 discrete, 1..9999 coil) or a raw 0-based offset.

  Per-command flags override session/connection options for one command only:
    read 0 4 --dtype f32 --format hex --slave 2 --connection backup
    watch 0 4 --interval 0.5 --iterations 20
    scan 0 50 --all

  Global session options (apply to all connections):
    dtype format byte_order word_order encoding scale regtype

  Per-connection options (apply to one connection):
    host port transport timeout retries slave baudrate parity stopbits
    bytesize cert key no_verify
"""

_ALIASES = {
    "h": "help", "?": "help", "st": "status", "q": "quit", "bye": "quit",
    "ls": "list", "conn": "connect", "disc": "disconnect",
    "cls": "clear", "dir": "ls", "shell": "!",
}


@dataclass
class Connection:
    """A single Modbus connection with its own transport settings and client."""
    name: str
    host: str = "127.0.0.1"
    port: int = 502
    transport: str = "tcp"
    timeout: float = 3.0
    retries: int = 0
    slave: int = 1
    baudrate: int = 9600
    parity: str = "N"
    stopbits: float = 1
    bytesize: int = 8
    cert: Optional[str] = None
    key: Optional[str] = None
    no_verify: bool = False
    client: Any = None

    def target(self) -> str:
        if self.transport in ("rtu", "ascii"):
            return self.host
        return f"{self.host}:{self.port}"

    def connected(self) -> bool:
        return self.client is not None and getattr(self.client, "connected", False)

    def make_namespace(self, command: str, global_session: "Session",
                       **extra: Any) -> SimpleNamespace:
        """Build a cli-style argparse namespace from connection + global state."""
        ns = SimpleNamespace(
            command=command,
            host=self.host, ip=self.host, port=self.port,
            transport=self.transport, timeout=self.timeout,
            retries=self.retries, slave=self.slave,
            baudrate=self.baudrate, parity=self.parity,
            stopbits=self.stopbits, bytesize=self.bytesize,
            cert=self.cert, key=self.key, no_verify=self.no_verify,
            dtype=global_session.dtype, format=global_session.format,
            byte_order=global_session.byte_order, word_order=global_session.word_order,
            encoding=global_session.encoding, scale=global_session.scale,
            verbose=False, json=False,
        )
        for key, value in extra.items():
            setattr(ns, key, value)
        return ns


class Session:
    """Session state: multiple connections + global options + active connection."""

    def __init__(self, args: Any) -> None:
        # Global (session-wide) options
        self.dtype = args.dtype
        self.format = args.format
        self.byte_order = args.byte_order
        self.word_order = args.word_order
        self.encoding = args.encoding
        self.scale = args.scale
        self.regtype: Optional[str] = None  # None = auto-detect from address

        # Connections (start empty)
        self.connections: Dict[str, Connection] = {}
        self.active_name: Optional[str] = None

        # Template for new connections (from CLI args)
        self._template_args = args

    def _get_template(self) -> Connection:
        """Create a fresh Connection from CLI args template."""
        a = self._template_args
        return Connection(
            name="",
            host=a.host,
            port=a.port,
            transport=a.transport,
            timeout=a.timeout,
            retries=a.retries,
            slave=a.slave,
            baudrate=a.baudrate,
            parity=a.parity,
            stopbits=a.stopbits,
            bytesize=a.bytesize,
            cert=getattr(a, "cert", None),
            key=getattr(a, "key", None),
            no_verify=getattr(a, "no_verify", False),
        )

    @property
    def active(self) -> Optional[Connection]:
        if self.active_name is None or self.active_name not in self.connections:
            return None
        return self.connections[self.active_name]

    def target(self) -> str:
        conn = self.active
        return conn.target() if conn else "no connection"

    def connected(self) -> bool:
        conn = self.active
        return conn is not None and conn.connected()

    def slave(self) -> int:
        conn = self.active
        return conn.slave if conn else 1


# ---------------------------------------------------------------------------
# token parsing
# ---------------------------------------------------------------------------


def _extract_flags(tokens: List[str], command: str) -> Tuple[Dict[str, Any], List[str]]:
    """Pull ``--flag [value]`` tokens (per-command spec) out of a token list.

    Returns (values dict keyed by attribute name, remaining positional tokens).
    """
    spec = _FLAGS[command]
    values: Dict[str, Any] = {}
    positionals: List[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in spec:
            attr, is_switch = spec[token]
            if is_switch:
                values[attr] = True
                i += 1
            else:
                if i + 1 >= len(tokens):
                    raise ValueError(f"Missing value for {token}")
                raw = tokens[i + 1]
                values[attr] = _convert_flag(attr, raw)
                i += 2
            continue
        if token.startswith("--") and token not in spec:
            raise ValueError(f"Unknown option: {token}")
        positionals.append(token)
        i += 1
    return values, positionals


def _convert_flag(attr: str, raw: str) -> Any:
    """Convert a raw flag value to its intended Python type."""
    if attr in ("count", "iterations", "start", "end", "slave"):
        return core.parse_int_token(raw)
    if attr == "interval":
        return float(raw)
    if attr == "scale":
        return float(raw)
    return raw


def _split_type_token(positionals: List[str]) -> Tuple[Optional[str], List[str]]:
    """Return (register type, remaining positionals) if the first token is a
    type word, else (None, positionals)."""
    if positionals and positionals[0].lower() in _REGISTER_TYPES:
        return _REGISTER_TYPES[positionals[0].lower()], positionals[1:]
    return None, positionals


def _parse_address(token: str) -> int:
    """Parse an address token (decimal or 0x hex); raises ValueError."""
    try:
        return core.parse_int_token(token)
    except Exception as exc:
        raise ValueError(f"Invalid address: {token} ({exc})")


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------


def _resolve_connection(session: Session, conn_name: Optional[str]) -> Connection:
    if conn_name is None:
        conn = session.active
        if conn is None:
            raise ValueError("No active connection. Use 'connect <host:port>' or specify --connection")
        return conn
    if conn_name not in session.connections:
        raise ValueError(f"Unknown connection: {conn_name} (use 'list' to see)")
    return session.connections[conn_name]


def _cmd_read(session: Session, flags: Dict[str, Any], positionals: List[str]) -> int:
    conn = _resolve_connection(session, flags.get("connection"))
    reg_type, rest = _split_type_token(positionals)
    if not rest:
        raise ValueError("Usage: read [type] <address> [count]")
    address = _parse_address(rest[0])
    count = int(rest[1]) if len(rest) > 1 else int(flags.get("count", 1))
    if len(rest) > 2:
        raise ValueError("Too many arguments for read")
    slave = int(flags["slave"]) if "slave" in flags else conn.slave

    ns = conn.make_namespace(
        "read",
        session,
        reg_type=flags.get("reg_type", reg_type if reg_type else session.regtype),
        address=address,
        count=count,
        dtype=flags.get("dtype", session.dtype),
        format=flags.get("format", session.format),
        byte_order=flags.get("byte_order", session.byte_order),
        word_order=flags.get("word_order", session.word_order),
        scale=flags.get("scale", session.scale),
        slave=slave,
    )
    return cli.cmd_read(ns)


def _cmd_write(session: Session, flags: Dict[str, Any], positionals: List[str]) -> int:
    conn = _resolve_connection(session, flags.get("connection"))
    reg_type, rest = _split_type_token(positionals)
    if len(rest) < 2:
        raise ValueError("Usage: write [type] <address> <values...>")
    address = _parse_address(rest[0])
    values = rest[1:]
    slave = int(flags["slave"]) if "slave" in flags else conn.slave

    ns = conn.make_namespace(
        "write",
        session,
        reg_type=flags.get("reg_type", reg_type if reg_type else session.regtype),
        address=address,
        values=values,
        dtype=flags.get("dtype", session.dtype),
        byte_order=flags.get("byte_order", session.byte_order),
        word_order=flags.get("word_order", session.word_order),
        scale=flags.get("scale", session.scale),
        slave=slave,
    )
    return cli.cmd_write(ns)


def _cmd_fill(session: Session, flags: Dict[str, Any], positionals: List[str]) -> int:
    conn = _resolve_connection(session, flags.get("connection"))
    reg_type, rest = _split_type_token(positionals)
    if not rest:
        raise ValueError("Usage: fill [type] <address> <value> [count]")
    address = _parse_address(rest[0])
    value = rest[1] if len(rest) > 1 else "0"
    count = int(flags.get("count", 1))
    all_regs = bool(flags.get("all", False))
    slave = int(flags["slave"]) if "slave" in flags else conn.slave

    ns = conn.make_namespace(
        "fill",
        session,
        reg_type=flags.get("reg_type", reg_type if reg_type else session.regtype),
        address=address,
        value=value,
        count=count,
        all=all_regs,
        slave=slave,
    )
    return cli.cmd_fill(ns)


def _cmd_save(session: Session, flags: Dict[str, Any], positionals: List[str]) -> int:
    conn = _resolve_connection(session, flags.get("connection"))
    if not positionals:
        raise ValueError("Usage: save <path>")
    path = positionals[0]
    slave = int(flags["slave"]) if "slave" in flags else conn.slave

    ns = conn.make_namespace(
        "save",
        session,
        path=path,
        reg_type=flags.get("reg_type", session.regtype),
        slave=slave,
    )
    return cli.cmd_save(ns)


def _cmd_restore(session: Session, flags: Dict[str, Any], positionals: List[str]) -> int:
    conn = _resolve_connection(session, flags.get("connection"))
    if not positionals:
        raise ValueError("Usage: restore <path>")
    path = positionals[0]
    slave = int(flags["slave"]) if "slave" in flags else conn.slave

    ns = conn.make_namespace(
        "restore",
        session,
        path=path,
        reg_type=flags.get("reg_type", session.regtype),
        slave=slave,
    )
    return cli.cmd_restore(ns)


def _cmd_scan(session: Session, flags: Dict[str, Any], positionals: List[str]) -> int:
    conn = _resolve_connection(session, flags.get("connection"))
    if len(positionals) > 2:
        raise ValueError("Usage: scan [start [end]]")
    start = int(flags["start"]) if "start" in flags else (
        _parse_address(positionals[0]) if positionals else 0)
    end = int(flags["end"]) if "end" in flags else (
        _parse_address(positionals[1]) if len(positionals) > 1 else 99)
    if start > end:
        raise ValueError(f"scan start {start} must be <= end {end}")
    slave = int(flags["slave"]) if "slave" in flags else conn.slave

    ns = conn.make_namespace(
        "scan",
        session,
        reg_type="holding",
        start=start,
        end=end,
        all=bool(flags.get("all", False)),
        dtype=flags.get("dtype", session.dtype),
        format=flags.get("format", session.format),
        slave=slave,
    )
    return cli.cmd_scan(ns)


def _cmd_watch(session: Session, flags: Dict[str, Any], positionals: List[str]) -> int:
    conn = _resolve_connection(session, flags.get("connection"))
    reg_type, rest = _split_type_token(positionals)
    if not rest:
        raise ValueError("Usage: watch [type] <address> [count]")
    address = _parse_address(rest[0])
    count = int(rest[1]) if len(rest) > 1 else int(flags.get("count", 1))
    if len(rest) > 2:
        raise ValueError("Too many arguments for watch")
    slave = int(flags["slave"]) if "slave" in flags else conn.slave

    ns = conn.make_namespace(
        "watch",
        session,
        reg_type=flags.get("reg_type", reg_type if reg_type else session.regtype),
        address=address,
        count=count,
        dtype=flags.get("dtype", session.dtype),
        format=flags.get("format", session.format),
        interval=float(flags.get("interval", 1.0)),
        iterations=int(flags.get("iterations", 0)),
        all=bool(flags.get("all", False)),
        json=bool(flags.get("json", False)),
        output=flags.get("output"),
        slave=slave,
    )
    return cli.cmd_watch(ns)


def _cmd_connect(session: Session, positionals: List[str]) -> int:
    """connect <name|host:port> [host [port]] -- create or switch to a named connection."""
    if len(positionals) < 1 or len(positionals) > 3:
        raise ValueError("Usage: connect <name|host:port> [host [port]]")

    first = positionals[0]

    def parse_hostport(s: str) -> Optional[Tuple[str, int]]:
        if ":" in s:
            try:
                h, p = s.split(":", 1)
                return h, int(p)
            except ValueError:
                return None
        return None

    def parse_name_hostport(s: str) -> Optional[Tuple[str, str, int]]:
        if "|" in s:
            name_part, hp_part = s.split("|", 1)
            hp = parse_hostport(hp_part)
            if hp:
                return name_part, hp[0], hp[1]
        return None

    def is_port(s: str) -> bool:
        try:
            p = int(s)
            return 1 <= p <= 65535
        except ValueError:
            return False

    name: str
    host_arg: Optional[str] = None
    port_arg: Optional[int] = None

    if len(positionals) == 1:
        nhp = parse_name_hostport(first)
        if nhp:
            name, host_arg, port_arg = nhp
        else:
            hp = parse_hostport(first)
            if hp:
                host_arg, port_arg = hp
                name = f"{host_arg}:{port_arg}"
            else:
                name = first
                host_arg = first
                port_arg = 502
    elif len(positionals) == 2:
        hp1 = parse_hostport(first)
        hp2 = parse_hostport(positionals[1])

        if hp1 and not hp2 and is_port(positionals[1]):
            raise ValueError(f"Ambiguous: use 'connect <name> {first} {positionals[1]}' or 'connect {first} <name>'")
        elif hp1 and hp2:
            raise ValueError("Too many host:port arguments")
        elif hp1:
            host_arg, port_arg = hp1
            name = positionals[1]
        elif hp2:
            name = first
            host_arg, port_arg = hp2
        elif is_port(positionals[1]):
            name = first
            host_arg = None
            port_arg = int(positionals[1])
        else:
            name = first
            host_arg = positionals[1]
            port_arg = 502
    else:
        name = first
        host_arg = positionals[1]
        if not is_port(positionals[2]):
            raise ValueError(f"Invalid port: {positionals[2]}")
        port_arg = int(positionals[2])

    target = f"{host_arg or (session.active.host if session.active else session._get_template().host)}:{port_arg or (session.active.port if session.active else session._get_template().port)}"

    existing_name = None
    for n, c in session.connections.items():
        if c.target() == target:
            existing_name = n
            break

    if existing_name:
        conn = session.connections[existing_name]
        session.active_name = existing_name
        name = existing_name
    elif name in session.connections:
        conn = session.connections[name]
        if host_arg is not None:
            conn.host = host_arg
        if port_arg is not None:
            conn.port = port_arg
    else:
        tmpl = session.active if session.active else session._get_template()
        conn = Connection(
            name=name,
            host=host_arg if host_arg is not None else tmpl.host,
            port=port_arg if port_arg is not None else tmpl.port,
            transport=tmpl.transport,
            timeout=tmpl.timeout,
            retries=tmpl.retries,
            slave=tmpl.slave,
            baudrate=tmpl.baudrate,
            parity=tmpl.parity,
            stopbits=tmpl.stopbits,
            bytesize=tmpl.bytesize,
            cert=tmpl.cert,
            key=tmpl.key,
            no_verify=tmpl.no_verify,
        )
        session.connections[name] = conn

    session.active_name = name

    if not conn.connected():
        ns = conn.make_namespace("connect", session)
        try:
            client = core.build_client(ns)
        except Exception as exc:
            error_panel(f"Failed to build client: {exc}")
            return core.EXIT_USAGE
        if not core.connect_with_retries(client, conn.retries, conn.target(),
                                         verbose=False):
            error_panel(f"Could not connect to {conn.target()} (slave {conn.slave})")
            return core.EXIT_CONN
        conn.client = client
        success_panel(f"Connected {name} ({conn.target()}, slave {conn.slave})")
    else:
        success_panel(f"Active connection: {name} ({conn.target()}, slave {conn.slave})")
    return core.EXIT_SUCCESS


def _cmd_use(session: Session, positionals: List[str]) -> int:
    """use <name> -- switch active connection."""
    if len(positionals) != 1:
        raise ValueError("Usage: use <name>")
    name = positionals[0]
    if name not in session.connections:
        raise ValueError(f"Unknown connection: {name} (use 'list' to see)")
    session.active_name = name
    conn = session.active
    success_panel(f"Switched to {name} ({conn.target()}, slave {conn.slave})")
    return core.EXIT_SUCCESS


def _cmd_list(session: Session, positionals: List[str]) -> int:
    """list -- show all connections."""
    if positionals:
        raise ValueError("Usage: list")
    if not session.connections:
        console.print("No connections. Use 'connect <host:port>' to create one.")
        console.print()
        return core.EXIT_SUCCESS
    for name, conn in session.connections.items():
        marker = "[bold green]*[/] " if name == session.active_name else "  "
        state = "[bold green]connected[/]" if conn.connected() else "[dim]disconnected[/]"
        console.print(f"{marker}{name}: {conn.target()} (slave {conn.slave}) {state}")
    console.print()
    return core.EXIT_SUCCESS


def _cmd_disconnect(session: Session, positionals: List[str]) -> int:
    """disconnect [name|host [port]] -- close a connection (default: active)."""
    if not positionals:
        name = session.active_name
    elif len(positionals) == 1:
        name = positionals[0]
        if name not in session.connections:
            for n, c in session.connections.items():
                if c.target() == name:
                    name = n
                    break
    else:
        host = positionals[0]
        port = int(positionals[1])
        target = f"{host}:{port}"
        name = None
        for n, c in session.connections.items():
            if c.target() == target:
                name = n
                break
        if name is None:
            raise ValueError(f"No connection matching {target}")

    if name is None:
        raise ValueError("No active connection to disconnect")
    if name not in session.connections:
        raise ValueError(f"Unknown connection: {name}")
    conn = session.connections[name]
    if conn.client is not None:
        try:
            conn.client.close()
        except Exception:
            pass
        conn.client = None
        success_panel(f"Disconnected {name}")
    else:
        error_panel(f"Connection {name} not connected")
        return core.EXIT_ERROR
    if name == session.active_name:
        remaining = [n for n in session.connections if n != name]
        if remaining:
            session.active_name = remaining[0]
            success_panel(f"Switched to {session.active_name}")
        else:
            session.connections["default"] = Connection(name="default")
            session.active_name = "default"
    return core.EXIT_SUCCESS


def _cmd_slave(session: Session, positionals: List[str]) -> int:
    """slave <id> -- set default slave for active connection."""
    if len(positionals) != 1:
        raise ValueError("Usage: slave <id>")
    session.active.slave = int(positionals[0])
    success_panel(f"Slave = {session.active.slave}")
    return core.EXIT_SUCCESS


def _cmd_status(session: Session) -> int:
    conn = session.active
    if conn is None:
        console.print("No active connection. Use 'connect <host:port>' to create one.")
        console.print()
        return core.EXIT_SUCCESS
    conn_state = ("[bold {0}]connected[/]".format(cli.core_color('success'))
                  if conn.connected()
                  else "[{0}]disconnected[/]".format(cli.core_color('muted')))
    state = (
        f"  connection:  [bold {cli.core_color('secondary')}]{session.active_name}[/] {conn_state}\n"
        f"  target:      [bold {cli.core_color('primary')}]{conn.target()}[/]\n"
        f"  transport:   {conn.transport}   slave: {conn.slave}\n"
        f"  timeout:     {conn.timeout}s   retries: {conn.retries}\n"
        f"  dtype:       {session.dtype}   format: {session.format}\n"
        f"  byte_order:  {session.byte_order}   word_order: {session.word_order}\n"
        f"  encoding:    {session.encoding}   scale: {session.scale}\n"
        f"  regtype:     {session.regtype or 'auto-detect'}"
    )
    console.print(state)
    console.print()
    return core.EXIT_SUCCESS


def _cmd_set(session: Session, positionals: List[str]) -> int:
    if not positionals or len(positionals) > 2:
        console.print("Settable options (global apply to all connections):")
        for key, (_conv, desc) in _GLOBAL_SETTABLE.items():
            console.print(f"  [bold {cli.core_color('secondary')}]{key}[/]   {desc}")
        console.print("\nPer-connection options (apply to active connection):")
        for key, (_conv, desc) in _CONNECTION_SETTABLE.items():
            console.print(f"  [bold {cli.core_color('secondary')}]{key}[/]   {desc}")
        return core.EXIT_SUCCESS

    key = positionals[0].lower().replace("-", "_")
    if key not in _SETTABLE:
        error_panel(f"Unknown option: {key} (use 'set' to list them)")
        return core.EXIT_USAGE

    if len(positionals) == 1:
        if key in _GLOBAL_SETTABLE:
            current = getattr(session, key, "")
        else:
            conn = session.active
            current = getattr(conn, key, "") if conn else "(no active connection)"
        console.print(f"  {key} = [bold {cli.core_color('primary')}]{current}[/]")
        return core.EXIT_SUCCESS

    try:
        value = _SETTABLE[key][0](positionals[1])
    except ValueError as exc:
        error_panel(f"{exc}")
        return core.EXIT_USAGE

    if key in _GLOBAL_SETTABLE:
        setattr(session, key, value)
    else:
        conn = session.active
        if conn is None:
            error_panel("No active connection. Use 'connect' first.")
            return core.EXIT_USAGE
        setattr(conn, key, value)
    success_panel(f"{key} = {value}")
    return core.EXIT_SUCCESS


def _cmd_ls(session: Session, positionals: List[str]) -> int:
    """ls [path] -- list directory contents."""
    path = positionals[0] if positionals else "."
    try:
        entries = os.listdir(path)
    except OSError as exc:
        error_panel(f"ls: {exc}")
        return core.EXIT_ERROR
    for entry in sorted(entries):
        full = os.path.join(path, entry)
        if os.path.isdir(full):
            console.print(f"[bold {cli.core_color('secondary')}]{entry}/[/]")
        elif os.path.islink(full):
            console.print(f"[bold {cli.core_color('accent')}]{entry}@[/]")
        elif os.access(full, os.X_OK):
            console.print(f"[bold {cli.core_color('success')}]{entry}*[/]")
        else:
            console.print(entry)
    return core.EXIT_SUCCESS


def _cmd_clear(session: Session, positionals: List[str]) -> int:
    """clear -- clear the terminal screen."""
    console.clear()
    return core.EXIT_SUCCESS


def _cmd_pwd(session: Session, positionals: List[str]) -> int:
    """pwd -- print working directory."""
    console.print(os.getcwd())
    return core.EXIT_SUCCESS


def _cmd_cd(session: Session, positionals: List[str]) -> int:
    """cd [dir] -- change working directory."""
    path = positionals[0] if positionals else os.path.expanduser("~")
    try:
        os.chdir(path)
    except OSError as exc:
        error_panel(f"cd: {exc}")
        return core.EXIT_ERROR
    return core.EXIT_SUCCESS


def _cmd_shell(session: Session, positionals: List[str]) -> int:
    """!<command> -- run a shell command."""
    if not positionals:
        raise ValueError("Usage: !<command>")
    cmd = " ".join(positionals)
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.stdout:
            console.print(result.stdout.rstrip())
        if result.stderr:
            console.print(f"[dim]{result.stderr.rstrip()}[/]")
        if result.returncode != 0:
            console.print(f"[dim](exit code {result.returncode})[/]")
    except Exception as exc:
        error_panel(f"Shell error: {exc}")
        return core.EXIT_ERROR
    return core.EXIT_SUCCESS


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_COMMANDS: Dict[str, Callable[[Session, List[str]], int]] = {
    "read": lambda s, p: _cmd_read(s, *_split_command(p, "read")),
    "write": lambda s, p: _cmd_write(s, *_split_command(p, "write")),
    "scan": lambda s, p: _cmd_scan(s, *_split_command(p, "scan")),
    "watch": lambda s, p: _cmd_watch(s, *_split_command(p, "watch")),
    "fill": lambda s, p: _cmd_fill(s, *_split_command(p, "fill")),
    "save": lambda s, p: _cmd_save(s, *_split_command(p, "save")),
    "restore": lambda s, p: _cmd_restore(s, *_split_command(p, "restore")),
    "connect": lambda s, p: _cmd_connect(s, p),
    "use": lambda s, p: _cmd_use(s, p),
    "list": lambda s, p: _cmd_list(s, p),
    "disconnect": lambda s, p: _cmd_disconnect(s, p),
    "slave": lambda s, p: _cmd_slave(s, p),
    "status": lambda s, p: _cmd_status(s),
    "set": lambda s, p: _cmd_set(s, p),
    "ls": lambda s, p: _cmd_ls(s, p),
    "clear": lambda s, p: _cmd_clear(s, p),
    "pwd": lambda s, p: _cmd_pwd(s, p),
    "cd": lambda s, p: _cmd_cd(s, p),
    "!": lambda s, p: _cmd_shell(s, p),
    "help": lambda s, p: _cmd_help(),
    "version": lambda s, p: _cmd_version(),
}


def _split_command(positionals: List[str], command: str):
    """Split a command's tokens into (flags dict, positional list)."""
    flags, rest = _extract_flags(positionals, command)
    return flags, rest


def _cmd_help() -> int:
    console.print(_HELP, markup=False)
    return core.EXIT_SUCCESS


def _cmd_version() -> int:
    import pymodbus
    from . import __version__ as modbus_version
    console.print(f"modbus {modbus_version} (pymodbus {pymodbus.__version__})")
    return core.EXIT_SUCCESS


def _dispatch(session: Session, command: str, positionals: List[str]) -> int:
    """Run one command; returns an exit code (shell keeps running)."""
    handler = _COMMANDS.get(command)
    if handler is None:
        error_panel(f"Unknown command: {command} (try 'help')")
        return core.EXIT_USAGE
    try:
        return handler(session, positionals)
    except SystemExit:
        return core.EXIT_ERROR


# ---------------------------------------------------------------------------
# session persistence
# ---------------------------------------------------------------------------


def _apply_saved_session(session: "Session", data: dict, args: Any) -> None:
    """Apply loaded session data to a Session instance."""
    g = data.get("global", {})
    session.dtype = g.get("dtype", session.dtype)
    session.format = g.get("format", session.format)
    session.byte_order = g.get("byte_order", session.byte_order)
    session.word_order = g.get("word_order", session.word_order)
    session.encoding = g.get("encoding", session.encoding)
    session.scale = g.get("scale", session.scale)
    session.regtype = g.get("regtype", session.regtype)

    connections = data.get("connections", {})
    for name, cdata in connections.items():
        tmpl = session._get_template()
        conn = Connection(
            name=cdata.get("name", name),
            host=cdata.get("host", tmpl.host),
            port=cdata.get("port", tmpl.port),
            transport=cdata.get("transport", tmpl.transport),
            timeout=cdata.get("timeout", tmpl.timeout),
            retries=cdata.get("retries", tmpl.retries),
            slave=cdata.get("slave", tmpl.slave),
            baudrate=cdata.get("baudrate", tmpl.baudrate),
            parity=cdata.get("parity", tmpl.parity),
            stopbits=cdata.get("stopbits", tmpl.stopbits),
            bytesize=cdata.get("bytesize", tmpl.bytesize),
            cert=cdata.get("cert", tmpl.cert),
            key=cdata.get("key", tmpl.key),
            no_verify=cdata.get("no_verify", tmpl.no_verify),
        )
        session.connections[name] = conn

    active_name = data.get("active_name")
    if active_name and active_name in session.connections:
        session.active_name = active_name


# ---------------------------------------------------------------------------
# REPL entry point
# ---------------------------------------------------------------------------

# Double-Ctrl+C exit behaviour: a single Ctrl+C prints ^C and continues; a
# second Ctrl+C within this window exits the shell (so connection cleanup runs).
_SIGINT_EXIT_WINDOW = 0.5
_LAST_SIGINT = 0.0


def _should_exit_on_sigint() -> bool:
    """Return True when a second Ctrl+C arrives within the exit window.

    First press records the timestamp and returns False; a second press within
    ``_SIGINT_EXIT_WINDOW`` seconds returns True (and resets the window).
    """
    global _LAST_SIGINT
    now = time.monotonic()
    if now - _LAST_SIGINT <= _SIGINT_EXIT_WINDOW:
        _LAST_SIGINT = 0.0
        return True
    _LAST_SIGINT = now
    return False


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for yes/no answer."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            ans = console.input(f"{question} {suffix} ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        console.print("[dim]Please answer y or n[/]")


def run_shell(args: Any) -> int:
    """Run the interactive console shell until the user exits."""
    try:
        import readline  # noqa: F401  (enables line editing + history)
    except ImportError:
        pass

    saved = _load_session()
    session = Session(args)

    if saved:
        console.print(f"[dim]Found saved session ({SESSION_FILE})[/]")
        if _prompt_yes_no("Load previous session?", default=True):
            _apply_saved_session(session, saved, args)
            console.print("[green]Session loaded[/]")
        else:
            console.print("[dim]Starting fresh[/]")
        console.print()

    if session.active:
        intro = (
            f"Interactive Modbus shell -- connection [bold {cli.core_color('secondary')}]"
            f"{session.active_name}[/] at [bold {cli.core_color('primary')}]"
            f"{session.target()}[/] (slave {session.active.slave}). "
            f"Type [bold {cli.core_color('secondary')}]help[/] for commands, "
            f"[bold {cli.core_color('secondary')}]exit[/] to quit."
        )
    else:
        intro = (
            f"Interactive Modbus shell -- no active connection. "
            f"Type [bold {cli.core_color('secondary')}]help[/] for commands, "
            f"[bold {cli.core_color('secondary')}]connect <host:port>[/] to start."
        )
    console.print(intro)
    console.print()

    try:
        while True:
            conn = session.active
            if conn:
                conn_marker = "[bold green]*[/]" if conn.connected() else "[dim]·[/]"
                prompt = (f"[bold {cli.core_color('primary')}]modbus[/]"
                          f"[{cli.core_color('muted')}] {session.active_name}{conn_marker} "
                          f"{conn.target()} [/]"
                          f"[bold {cli.core_color('secondary')}]> [/]")
            else:
                prompt = (f"[bold {cli.core_color('primary')}]modbus[/]"
                          f"[{cli.core_color('muted')}] no connection [/]"
                          f"[bold {cli.core_color('secondary')}]> [/]")
            try:
                line = console.input(prompt)
            except EOFError:
                console.print()
                break
            except KeyboardInterrupt:
                if _should_exit_on_sigint():
                    console.print("[dim]^C[/]\n")
                    break
                console.print("[dim]^C[/]")
                continue

            line = line.strip()
            if not line:
                continue

            if line.startswith("!"):
                tokens = ["!"] + shlex.split(line[1:])
            else:
                try:
                    tokens = shlex.split(line)
                except ValueError as exc:
                    error_panel(f"Bad quoting: {exc}")
                    continue

            command = tokens[0].lower()
            command = _ALIASES.get(command, command)
            if command in ("exit", "quit"):
                break
            if command.startswith("#") or command.startswith("//"):
                continue

            try:
                rc = _dispatch(session, command, tokens[1:])
            except KeyboardInterrupt:
                if _should_exit_on_sigint():
                    console.print("[dim]^C[/]\n")
                    break
                console.print("[dim]^C[/]")
                continue
            except core.ModbusException as exc:
                error_panel(f"Modbus error: {exc}")
                continue
            except ValueError as exc:
                error_panel(str(exc))
                continue

            if rc not in (core.EXIT_SUCCESS,):
                console.print(f"[dim](command returned {rc})[/]")

    finally:
        for conn in session.connections.values():
            if conn.client is not None:
                try:
                    conn.client.close()
                except Exception:
                    pass

        if session.connections:
            if _prompt_yes_no("Save session for next time?", default=True):
                if _save_session(session):
                    console.print("[green]Session saved[/]")
                else:
                    console.print("[dim]Failed to save session[/]")
            else:
                try:
                    SESSION_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

        console.print("[dim]bye[/]")
    return core.EXIT_SUCCESS


# Preserved entry point name for cli.cmd_tui.
cmd_tui = run_shell
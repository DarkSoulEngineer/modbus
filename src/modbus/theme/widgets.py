"""Shared rendering widgets for modbus: connection header, status panels,
and the value bar.  Ported from the original tool's ``theme.py``."""

from rich.panel import Panel
from rich.text import Text

from .palette import COLORS, console


def connection_header(target: str, reg_type: str, slave: int) -> None:
    """Print a styled connection info bar."""
    conn = Text()
    conn.append("  ", style="bold green")
    conn.append("connected", style=f"bold {COLORS['success']}")
    conn.append("  ", style="dim")
    conn.append(target, style=f"bold {COLORS['primary']}")
    conn.append("  ", style="dim")
    conn.append(f"slave {slave}", style=COLORS["secondary"])
    conn.append("  ", style="dim")
    conn.append(reg_type, style=COLORS["accent"])
    console.print(Panel(conn, border_style=COLORS["muted"], padding=(0, 1)))


def error_panel(message: str) -> None:
    """Print an error in a styled panel."""
    console.print(Panel(
        f"[bold {COLORS['error']}]{message}[/]",
        title=f"[bold {COLORS['error']}]error[/]",
        border_style=COLORS["error"],
        padding=(0, 1),
    ))


def success_panel(message: str) -> None:
    """Print a success message in a styled panel."""
    console.print(Panel(
        f"[bold {COLORS['success']}]{message}[/]",
        title=f"[bold {COLORS['success']}]done[/]",
        border_style=COLORS["success"],
        padding=(0, 1),
    ))


def value_bar(value: int, max_val: int = 65535, width: int = 20) -> str:
    """Create a mini bar visualization for a register value.

    ``max_val`` is the datatype's upper bound, so the fill is proportional to
    the value's *own* range (a u16 fills against 65535, an i16 against 32767,
    etc.).  Binary/flag registers (value 0 or 1) are treated as a 0..1 range
    so a ``1`` renders as a full bar and a ``0`` as empty.
    """
    # Binary/flag semantics: 0 or 1 -> full/empty relative to a max of 1.
    eff_max = 1 if value in (0, 1) else max_val
    if eff_max:
        ratio = max(0.0, min(value / eff_max, 1.0))
    else:
        ratio = 0.0
    filled = int(ratio * width)
    empty = width - filled

    # Gradient from teal to purple based on value
    if ratio < 0.33:
        color = COLORS["primary"]
    elif ratio < 0.66:
        color = COLORS["secondary"]
    else:
        color = COLORS["accent"]

    return f"[{color}]{'━' * filled}[/][dim {COLORS['muted']}]{'─' * empty}[/]"

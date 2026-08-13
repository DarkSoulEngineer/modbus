"""Banner display for modbus."""

from rich.panel import Panel

from .. import __version__
from .palette import COLORS, console

_AMBER = COLORS["warning"]        # gold:  #ffd93d
_DIM = COLORS["muted"]            # gray:  #636e72

_ROW_COLORS = [
    "#2F3437",  # charcoal
    "#4A5560",  # gunmetal
    "#6B6F6A",  # industrial gray
    "#7A6855",  # weathered brown
    "#9A7B4F",  # aged brass
    "#56626B",  # blue steel
]

_BANNER_ROWS = [
    "    ███╗   ███╗  ██████╗███████╗ ███████╗  ██╗   ██╗  ███████╗",
    "   ████╗ ████║ ██╔═══██╗ ██╔  ██╗ ██╔══██╗ ██║   ██║  ██╔════╝",
    "  ██╔████╔██║  ██║   ██║ ██║  ██║ ██████╔╝ ██║   ██║  ███████╗",
    "  ██║╚██╔██║  ██║   ██║ ██║  ██║ ██╔══██╗  ██║   ██║  ╚════██║",
    " ██║ ╚═╝ ██║   ██████╔╝ ██████╔╝ ██████╔╝  ███████╔╝  ███████║",
    " ╚═╝     ╚═╝   ╚═════╝  ╚═════╝ ╚══════╝  ╚═══════╝   ╚══════╝",
]

def banner() -> None:
    """Print the banner with DarkSoulEngineer attribution."""
    console.print()
    console.print(Panel(
        "\n".join(f"[{c}]{row}[/]" for (row, c) in zip(_BANNER_ROWS, _ROW_COLORS)),
        border_style=_DIM,
        padding=(0, 4),
        expand=False,
    ))
    console.print(f"  [bold {_AMBER}]Crafted by DarkSoulEngineer[/]")
    console.print(f"  [{_DIM}]TCP · UDP · RTU · ASCII · TLS[/]"
                 f"  [{_DIM}]Registers · Coils · Discrete · Scan · Watch[/]")
    console.print()

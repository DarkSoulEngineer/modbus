"""Color palette and base theme for modbus."""

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

# Color palette -- industrial meets modern
COLORS = {
    "primary": "#00d4aa",      # teal/mint
    "secondary": "#7c6ff7",    # purple
    "accent": "#ff6b6b",       # coral red
    "warning": "#ffd93d",      # gold
    "success": "#6bcb77",      # green
    "muted": "#636e72",        # gray
    "surface": "#2d3436",      # dark bg
    "text": "#dfe6e9",         # light text
    "highlight": "#00cec9",    # bright teal
    "changed": "#fdcb6e",      # amber
    "error": "#e17055",        # warm red
}

custom_theme = Theme({
    "info": Style(color=COLORS["primary"]),
    "warning": Style(color=COLORS["warning"]),
    "error": Style(color=COLORS["error"], bold=True),
    "success": Style(color=COLORS["success"], bold=True),
    "muted": Style(color=COLORS["muted"]),
    "address": Style(color=COLORS["secondary"], bold=True),
    "value": Style(color=COLORS["primary"]),
    "changed": Style(color=COLORS["changed"], bold=True),
    "header": Style(color=COLORS["primary"], bold=True),
    "register": Style(color=COLORS["highlight"]),
})

console = Console(theme=custom_theme, soft_wrap=True, stderr=True)
"""Application palettes shared by preference validation and the theme picker."""

from textual.theme import BUILTIN_THEMES, Theme

DEFAULT_THEME = "nord"

COZY_PASTEL = Theme(
    name="cozy-pastel",
    primary="#90709E",
    secondary="#79B9A5",
    accent="#A77790",
    foreground="#49364F",
    background="#FAE7EE",
    surface="#FFF4F5",
    panel="#E8D5ED",
    success="#4F8975",
    warning="#B07546",
    error="#B74769",
    dark=False,
    luminosity_spread=0.08,
    variables={
        "text": "#49364F",
        "text-muted": "#78647E",
        "text-disabled": "#A493AA",
        "border": "#C4A8CC",
        "border-blurred": "#DDC5DF",
        "block-cursor-background": "#D9C0E5",
        "block-cursor-foreground": "#49364F",
        "block-cursor-text-style": "bold",
        "input-selection-background": "#D9C0E5 65%",
        "button-color-foreground": "#49364F",
    },
)

THEMES = {COZY_PASTEL.name: COZY_PASTEL, **BUILTIN_THEMES}


def theme_label(name: str) -> str:
    """Readable picker labels without changing Textual's stable theme names."""
    return name.replace("-", " ").title()

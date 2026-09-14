"""GrokNight palette — same tokens as Grok Build's default theme."""

from textual.theme import Theme

BG = "#141414"
BG_DARK = "#0c0c0c"
BG_TERMINAL = "#0a0a0a"
SURFACE = "#1c1c1c"
HIGHLIGHT = "#242424"
BORDER = "#333333"
FG = "#e1e1e1"
FG_SECONDARY = "#c8c8c8"
MUTED = "#6c6c6c"
GUTTER = "#414141"
MAGENTA = "#bb9af7"
BLUE = "#7aa2f7"
GREEN = "#9ece6a"
RED = "#f7768e"
YELLOW = "#e0af68"
ORANGE = "#ff9e64"

GROK_NIGHT = Theme(
    name="groknight",
    primary=MAGENTA,
    secondary=BLUE,
    accent=YELLOW,
    foreground=FG,
    background=BG,
    surface=SURFACE,
    panel=SURFACE,
    success=GREEN,
    warning=YELLOW,
    error=RED,
    dark=True,
    ansi=False,
    variables={
        "block-cursor-background": HIGHLIGHT,
        "block-cursor-foreground": FG,
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": SURFACE,
        "block-cursor-blurred-foreground": FG,
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": HIGHLIGHT,
        "footer-background": BG_DARK,
        "footer-key-foreground": MAGENTA,
        "footer-description-foreground": FG_SECONDARY,
        "button-color-foreground": BG,
    },
)

"""Constants, keymaps, CP437 translation, and VGA palette for FreeDOS target."""
import re
from typing import Optional

# Standard 16-color VGA palette RGB definitions
VGA_PALETTE_RGB: list[tuple[int, int, int]] = [
    (0, 0, 0),        # 0: Black
    (0, 0, 170),      # 1: Blue
    (0, 170, 0),      # 2: Green
    (0, 170, 170),    # 3: Cyan
    (170, 0, 0),      # 4: Red
    (170, 0, 170),    # 5: Magenta
    (170, 85, 0),     # 6: Brown
    (170, 170, 170),  # 7: Light Gray
    (85, 85, 85),     # 8: Dark Gray
    (85, 85, 255),    # 9: Light Blue
    (85, 255, 85),    # 10: Light Green
    (85, 255, 255),   # 11: Light Cyan
    (255, 85, 85),    # 12: Light Red
    (255, 85, 255),   # 13: Light Magenta
    (255, 255, 85),   # 14: Yellow
    (255, 255, 255),  # 15: Bright White
]

# Standard CP437 character decoding table to Unicode
# Note: In accordance with Project SENTINEL design invariants:
# 1. 0x7C maps strictly to ASCII '|' (code 0x7C, not Unicode broken bar '\u00a6')
# 2. Blank character codes (0x00, 0x20, 0xFF) map canonically to ASCII space ' '
CP437_TO_UNICODE: list[str] = [
    # 0x00 - 0x0F
    " ", "☺", "☻", "♥", "♦", "♣", "♠", "•", "◘", "○", "◙", "♂", "♀", "♪", "♫", "☼",
    # 0x10 - 0x1F
    "►", "◄", "↕", "‼", "¶", "§", "▬", "↨", "↑", "↓", "→", "←", "∟", "↔", "▲", "▼",
    # 0x20 - 0x2F (Standard ASCII 32 - 47)
    " ", "!", '"', "#", "$", "%", "&", "'", "(", ")", "*", "+", ",", "-", ".", "/",
    # 0x30 - 0x39 (Digits '0'-'9')
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    # 0x3A - 0x40 (Punctuation)
    ":", ";", "<", "=", ">", "?", "@",
    # 0x41 - 0x5A (Uppercase 'A'-'Z')
    "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
    "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
    # 0x5B - 0x60
    "[", "\\", "]", "^", "_", "`",
    # 0x61 - 0x7A (Lowercase 'a'-'z')
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
    "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
    # 0x7B - 0x7F (Note: 0x7C is ASCII '|', NOT broken bar)
    "{", "|", "}", "~", "⌂",
    # 0x80 - 0x8F
    "Ç", "ü", "é", "â", "ä", "à", "å", "ç", "ê", "ë", "è", "ï", "î", "ì", "Ä", "Å",
    # 0x90 - 0x9F
    "É", "æ", "Æ", "ô", "ö", "ò", "û", "ù", "ÿ", "Ö", "Ü", "¢", "£", "¥", "₧", "ƒ",
    # 0xA0 - 0xAF
    "á", "í", "ó", "ú", "ñ", "Ñ", "ª", "º", "¿", "⌐", "¬", "½", "¼", "¡", "«", "»",
    # 0xB0 - 0xBF (Box drawing: single, double, shaded)
    "░", "▒", "▓", "│", "┤", "╡", "╢", "╖", "╕", "╣", "║", "╗", "╝", "╜", "╛", "┐",
    # 0xC0 - 0xCF
    "└", "┴", "┬", "├", "─", "┼", "╞", "╟", "╚", "╔", "╩", "╦", "╠", "═", "╬", "╧",
    # 0xD0 - 0xDF
    "╨", "╤", "╥", "╙", "╘", "╒", "╓", "╫", "╪", "┘", "┌", "█", "▄", "▌", "▐", "▀",
    # 0xE0 - 0xEF (Greek / Math)
    "α", "ß", "Γ", "π", "Σ", "σ", "µ", "τ", "Φ", "Θ", "Ω", "δ", "∞", "φ", "ε", "∩",
    # 0xF0 - 0xFF
    "≡", "±", "≥", "≤", "⌠", "⌡", "÷", "≈", "°", "∙", "·", "√", "ⁿ", "²", "■", " ",
]

# Single source of truth for DOS keys and their X11 keysym identifiers
# Keys normalized to canonical names (e.g. PAGE_UP, PAGE_DOWN, ESCAPE, ENTER)
DOS_KEYMAP: dict[str, str] = {
    # Navigation & control
    "ENTER": "Return",
    "ESC": "Escape",
    "TAB": "Tab",
    "BACKSPACE": "BackSpace",
    "SPACE": "space",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "PAGE_UP": "Page_Up",
    "PAGE_DOWN": "Page_Down",
    "HOME": "Home",
    "END": "End",
    "INSERT": "Insert",
    "DELETE": "Delete",
    # Function keys F1 - F12
    "F1": "F1",
    "F2": "F2",
    "F3": "F3",
    "F4": "F4",
    "F5": "F5",
    "F6": "F6",
    "F7": "F7",
    "F8": "F8",
    "F9": "F9",
    "F10": "F10",
    "F11": "F11",
    "F12": "F12",
}

# Add standard printable alphanumeric keys (for combo bases e.g. ALT+F)
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    DOS_KEYMAP[c] = c

# Input aliases normalized to canonical DOS_KEYMAP keys
KEY_ALIASES: dict[str, str] = {
    "ESCAPE": "ESC",
    "RETURN": "ENTER",
    "PGUP": "PAGE_UP",
    "PGDN": "PAGE_DOWN",
    "BKSP": "BACKSPACE",
    "DEL": "DELETE",
    "INS": "INSERT",
}

# Modifiers supported by the grammar
VALID_MODIFIERS: dict[str, str] = {
    "ALT": "Alt_L",
    "CTRL": "Control_L",
    "CONTROL": "Control_L",
    "SHIFT": "Shift_L",
}

COMBO_REGEX = re.compile(r"^(?:(ALT|CTRL|CONTROL|SHIFT)\+)+([A-Za-z0-9_]+)$", re.IGNORECASE)


def normalize_key_name(key: str) -> str:
    """Normalize input key name (e.g. 'escape' -> 'ESC', 'PgUp' -> 'PAGE_UP')."""
    clean = key.strip().upper()
    return KEY_ALIASES.get(clean, clean)


def parse_key_combo(combo_str: str) -> tuple[list[str], str]:
    """Parse and validate a modifier combo string (e.g. 'ALT+F', 'CTRL+C', 'SHIFT+TAB').
    
    Returns (modifier_keysyms, base_keysym).
    Raises ValueError with a helpful message listing allowed keys if invalid.
    """
    parts = [p.strip().upper() for p in combo_str.split("+")]
    if len(parts) == 1:
        canonical = normalize_key_name(parts[0])
        if canonical not in DOS_KEYMAP:
            allowed = sorted(list(DOS_KEYMAP.keys()) + list(KEY_ALIASES.keys()))
            raise ValueError(f"Unknown key '{combo_str}'. Allowed keys: {', '.join(allowed)}")
        return [], DOS_KEYMAP[canonical]

    # Multiple parts: all except the last must be valid modifiers
    mods: list[str] = []
    mod_set: set[str] = set()
    for mod_part in parts[:-1]:
        if mod_part not in VALID_MODIFIERS:
            raise ValueError(f"Unknown modifier '{mod_part}' in '{combo_str}'. Allowed modifiers: ALT, CTRL, SHIFT.")
        mod_keysym = VALID_MODIFIERS[mod_part]
        if mod_keysym not in mods:
            mods.append(mod_keysym)
            mod_set.add(mod_part)

    base_raw = parts[-1]
    base_canonical = normalize_key_name(base_raw)

    # Security check: explicitly block CTRL+ALT+DELETE to prevent unintended guest reboot
    if ("CTRL" in mod_set or "CONTROL" in mod_set) and "ALT" in mod_set and base_canonical == "DELETE":
        raise ValueError("Action 'CTRL+ALT+DELETE' is blocked by security policy to prevent guest reboot.")

    # Unsupported check: explicitly reject CTRL+BREAK with guidance to use CTRL+C
    if ("CTRL" in mod_set or "CONTROL" in mod_set) and base_raw == "BREAK":
        raise ValueError("Action 'CTRL+BREAK' is unsupported over VNC. Use 'CTRL+C' to interrupt running DOS processes.")

    if base_canonical not in DOS_KEYMAP:
        allowed_bases = sorted(list(DOS_KEYMAP.keys()) + list(KEY_ALIASES.keys()))
        raise ValueError(
            f"Invalid base key '{base_raw}' in combo '{combo_str}'. "
            f"Allowed base keys: {', '.join(allowed_bases[:30])}..."
        )

    return mods, DOS_KEYMAP[base_canonical]

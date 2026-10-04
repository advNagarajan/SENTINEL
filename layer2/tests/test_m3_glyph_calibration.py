"""M3 Milestone Acceptance Tests: Calibration table, glyph matching, blank derivation, and key grammar."""
from pathlib import Path
import numpy as np
from PIL import Image
import pytest

from layer2.dos.constants import CP437_TO_UNICODE, DOS_KEYMAP, normalize_key_name, parse_key_combo
from layer2.dos.glyph_table import GlyphTable


@pytest.fixture
def glyph_table() -> GlyphTable:
    return GlyphTable()


@pytest.fixture
def calibration_frame() -> np.ndarray:
    path = Path("logs/m3_calibration_screen.png")
    if not path.exists():
        pytest.skip("Calibration frame logs/m3_calibration_screen.png not found")
    img = Image.open(path).convert("RGB")
    return np.array(img)


def test_m3_glyph_table_structure(glyph_table: GlyphTable) -> None:
    """Verify glyph table contains exactly 256 templates of shape (16, 9)."""
    assert glyph_table.templates.shape == (256, 16, 9)
    assert glyph_table.templates.dtype == np.uint8


def test_m3_derived_blank_codes(glyph_table: GlyphTable) -> None:
    """Verify blank codes are derived dynamically from calibration data."""
    # In CP437, 0x00 (NUL), 0x20 (Space), and 0xFF (blank) have 0 foreground pixels
    assert 0x00 in glyph_table.blank_codes
    assert 0x20 in glyph_table.blank_codes
    assert 0xFF in glyph_table.blank_codes
    assert sorted(glyph_table.blank_codes) == [0x00, 0x20, 0xFF]


def test_m3_blank_cell_deterministic_resolution(glyph_table: GlyphTable) -> None:
    """Verify any cell with 0 foreground pixels canonically resolves to 0x20 (Space)."""
    empty_mask = np.zeros((16, 9), dtype=np.uint8)
    code, char_str, err, conf = glyph_table.match_mask(empty_mask)
    assert code == 0x20
    assert char_str == " "
    assert err == 0
    assert conf == 1.0


def test_m3_full_block_resolution(glyph_table: GlyphTable) -> None:
    """Verify full block (all foreground pixels) resolves to 0xDB (█)."""
    full_mask = np.ones((16, 9), dtype=np.uint8)
    code, char_str, err, conf = glyph_table.match_mask(full_mask)
    assert code == 0xDB
    assert char_str == "█"
    assert err == 0
    assert conf == 1.0


def test_m3_pipe_character_code_mapping(glyph_table: GlyphTable) -> None:
    """Verify character code 0x7C maps strictly to ASCII '|', not Unicode broken bar."""
    assert CP437_TO_UNICODE[0x7C] == "|"
    mask_7c = glyph_table.get_template(0x7C)
    code, char_str, err, conf = glyph_table.match_mask(mask_7c)
    assert code == 0x7C
    assert char_str == "|"
    assert err == 0


def test_m3_9th_column_duplication_range(glyph_table: GlyphTable) -> None:
    """Verify QEMU 9th-column duplication applies to range 0xB0 to 0xDF."""
    for code in range(0xB0, 0xE0):
        t = glyph_table.get_template(code)
        col7 = t[:, 7]
        col8 = t[:, 8]
        # In line graphics, col 8 must duplicate col 7
        assert np.array_equal(col7, col8), f"Code {hex(code)} 9th column not duplicated from 8th column"

    # For standard letters (e.g. 'A' 0x41), 9th column must be empty background
    t_a = glyph_table.get_template(ord("A"))
    assert np.all(t_a[:, 8] == 0), "Character 'A' should have blank 9th column"


def test_m3_acceptance_all_256_characters_recovery(
    glyph_table: GlyphTable, calibration_frame: np.ndarray
) -> None:
    """M3 Acceptance Criteria: Reading the known 256-character screen recovers all 256 characters."""
    total_chars = 256
    exact_matches = 0

    for code in range(256):
        r = code // 16
        c = code % 16
        R = 4 + r
        C = 16 + c * 2

        y0 = R * 16
        y1 = y0 + 16
        x0 = C * 9
        x1 = x0 + 9

        cell = calibration_frame[y0:y1, x0:x1]
        fg_mask = (cell[:, :, 0] > 50).astype(np.uint8)

        matched_code, unicode_char, err, conf = glyph_table.match_mask(fg_mask)

        # For blank codes (0x00, 0x20, 0xFF), canonical choice is 0x20
        if code in glyph_table.blank_codes:
            assert matched_code == 0x20
            exact_matches += 1
        else:
            assert matched_code == code, f"Mismatch for code {hex(code)}: got {hex(matched_code)}"
            assert err == 0, f"Non-zero error for code {hex(code)}: {err} differing pixels"
            exact_matches += 1

    assert exact_matches == total_chars


def test_m3_exhaustive_identical_and_complement_glyph_pairs(glyph_table: GlyphTable) -> None:
    """Verify mathematically that no pair of character codes shares an identical or complement
    bitmap beyond the known blank codes and complement pairs."""
    templates = glyph_table.templates  # (256, 16, 9)

    identical_pairs: list[tuple[int, int]] = []
    for i in range(256):
        for j in range(i + 1, 256):
            if np.array_equal(templates[i], templates[j]):
                identical_pairs.append((i, j))

    # The ONLY identical pairs among all 256 characters are the 3 blank glyphs
    expected_identical = [(0x00, 0x20), (0x00, 0xFF), (0x20, 0xFF)]
    assert identical_pairs == expected_identical, f"Unexpected identical glyph pairs: {identical_pairs}"

    complement_pairs: list[tuple[int, int]] = []
    for i in range(256):
        for j in range(i + 1, 256):
            if np.array_equal(templates[i], 1 - templates[j]):
                complement_pairs.append((i, j))

    # The ONLY complement pairs among all 256 characters are the 5 known pairs
    expected_complements = [
        (0x00, 0xDB),  # NUL <-> Full Block
        (0x20, 0xDB),  # Space <-> Full Block
        (0xDB, 0xFF),  # Full Block <-> NBSP
        (0xDC, 0xDF),  # Lower half <-> Upper half
        (0xDD, 0xDE),  # Left half <-> Right half
    ]
    assert complement_pairs == expected_complements, f"Unexpected complement pairs: {complement_pairs}"


def test_m3_key_normalization() -> None:
    """Verify single source of truth key normalization and aliases."""
    assert normalize_key_name("pgup") == "PAGE_UP"
    assert normalize_key_name("pgdn") == "PAGE_DOWN"
    assert normalize_key_name("escape") == "ESC"
    assert normalize_key_name("esc") == "ESC"
    assert normalize_key_name("return") == "ENTER"
    assert normalize_key_name("ENTER") == "ENTER"


def test_m3_key_combo_grammar() -> None:
    """Verify combo grammar parsing and whitelist validation."""
    mods, base = parse_key_combo("ALT+F")
    assert mods == ["Alt_L"]
    assert base == "F"

    mods, base = parse_key_combo("CTRL+C")
    assert mods == ["Control_L"]
    assert base == "C"

    mods, base = parse_key_combo("SHIFT+TAB")
    assert mods == ["Shift_L"]
    assert base == "Tab"

    # Single key via parse_key_combo
    mods, base = parse_key_combo("PAGE_UP")
    assert mods == []
    assert base == "Page_Up"

    mods, base = parse_key_combo("PGDN")
    assert mods == []
    assert base == "Page_Down"

    # Security check: CTRL+ALT+DELETE blocked
    with pytest.raises(ValueError, match="Action 'CTRL\\+ALT\\+DELETE' is blocked by security policy"):
        parse_key_combo("CTRL+ALT+DELETE")

    # Unsupported check: CTRL+BREAK rejected
    with pytest.raises(ValueError, match="Action 'CTRL\\+BREAK' is unsupported over VNC"):
        parse_key_combo("CTRL+BREAK")

    # Whitelist rejection for invalid bases
    with pytest.raises(ValueError, match="Invalid base key 'FOO'"):
        parse_key_combo("ALT+FOO")

    with pytest.raises(ValueError, match="Unknown modifier 'INVALID'"):
        parse_key_combo("INVALID+F")

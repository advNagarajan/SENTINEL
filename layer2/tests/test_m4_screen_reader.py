"""M4 Screen Reader Unit & Acceptance Tests: Slicing, matching, tie-breaking, and ColorGrid."""
from pathlib import Path
import numpy as np
from PIL import Image
import pytest

from layer2.dos.screen_reader import DOSScreenReader


@pytest.fixture
def screen_reader() -> DOSScreenReader:
    return DOSScreenReader()


def test_m4_screen_reader_initialization(screen_reader: DOSScreenReader) -> None:
    """Verify screen reader initializes with 16 VGA colors and complement pairs."""
    assert screen_reader.palette.shape == (16, 3)
    assert len(screen_reader.complement_pairs) >= 4


def test_m4_single_color_cell_deterministic(screen_reader: DOSScreenReader) -> None:
    """Verify solid single-colour cell resolves to Space with fg == bg."""
    # Blue background frame (720x400, RGB: 0, 0, 170)
    solid_blue_frame = np.full((400, 720, 3), (0, 0, 170), dtype=np.uint8)
    res = screen_reader.read_frame(solid_blue_frame)

    assert res.screen_size == {"rows": 25, "cols": 80}
    assert res.confidence_score == 1.0
    assert not res.is_graphics

    # Every cell must be space ' '
    for row in res.raw_grid:
        assert row == " " * 80

    # Every cell must have fg == bg == '1' (Blue VGA index 1)
    for fg_row in res.color_grid.fg:
        assert fg_row == "1" * 80
    for bg_row in res.color_grid.bg:
        assert bg_row == "1" * 80


def test_m4_complement_pair_tie_breaking(screen_reader: DOSScreenReader) -> None:
    """Verify tie-breaking on complement pairs (0xDC vs 0xDF) uses deterministic code-only tie-breaker.
    
    Fixed tie-breaker by character code ensures decoding is completely local to the cell
    and invariant to screen background changes (preventing settle timer resets on scroll).
    """
    # Test case 1: Cell on black background (dominant bg = 0)
    frame_black = np.zeros((400, 720, 3), dtype=np.uint8)
    frame_black[8:16, 0:9] = (255, 255, 255)

    res_black = screen_reader.read_frame(frame_black)
    assert res_black.dominant_bg == 0
    # Lower half block ▄ (0xDC = 220) has lower code than ▀ (0xDF = 223), so ▄ is chosen
    assert res_black.raw_grid[0][0] == "▄"

    # Test case 2: Identical cell on white background (dominant bg = 15)
    frame_white = np.full((400, 720, 3), (255, 255, 255), dtype=np.uint8)
    frame_white[0:8, 0:9] = (0, 0, 0)
    frame_white[8:16, 0:9] = (255, 255, 255)

    res_white = screen_reader.read_frame(frame_white)
    # The cell's decoded character must remain identical regardless of dominant bg!
    assert res_white.raw_grid[0][0] == "▄"


def test_m4_recoverable_graphics_mode(screen_reader: DOSScreenReader) -> None:
    """Verify unsupported / graphics frame resolution returns low confidence without crashing."""
    graphics_frame = np.zeros((600, 800, 3), dtype=np.uint8)
    res = screen_reader.read_frame(graphics_frame)

    assert res.is_graphics is True
    assert res.confidence_score < 0.2
    assert res.screen_size == {"rows": 25, "cols": 80}


def test_m4_color_grid_dimensions(screen_reader: DOSScreenReader) -> None:
    """Verify ColorGrid produces exactly 25 rows of 80 hex characters."""
    black_frame = np.zeros((400, 720, 3), dtype=np.uint8)
    res = screen_reader.read_frame(black_frame)

    assert len(res.color_grid.fg) == 25
    assert len(res.color_grid.bg) == 25
    for r in range(25):
        assert len(res.color_grid.fg[r]) == 80
        assert len(res.color_grid.bg[r]) == 80


@pytest.mark.parametrize(
    "fixture_name",
    [
        "logs/m4_scenario_boot_prompt.png",
        "logs/m4_scenario_dir_output.png",
        "logs/m4_scenario_mem_output.png",
        "logs/m4_scenario_help_output.png",
        "logs/m4_scenario_edit_main.png",
        "logs/m4_scenario_edit_menu.png",
    ],
)
def test_m4_acceptance_live_screen_fixtures(
    screen_reader: DOSScreenReader, fixture_name: str
) -> None:
    """M4 Acceptance Criterion: Screen reader achieves high confidence on real FreeDOS screen fixtures."""
    path = Path(fixture_name)
    if not path.exists():
        pytest.skip(f"Fixture {fixture_name} not found")

    img = Image.open(path).convert("RGB")
    arr = np.array(img)

    res = screen_reader.read_frame(arr)
    assert res.screen_size == {"rows": 25, "cols": 80}
    assert res.confidence_score >= 0.99
    assert len(res.raw_grid) == 25
    for row in res.raw_grid:
        assert len(row) == 80

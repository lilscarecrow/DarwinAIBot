"""read_director_points() against real captured colored-digit numerators
(tests/fixtures/director_points — see its README).

2026-09-15: the original grayscale+Otsu preprocessing failed on a large
fraction of live-captured frames because the numerator digits render in
different fill colors at different times (white in one state, saturated pink
in another, as part of the same pulse-idle animation the pip row already
accounts for) — a weighted-luminance grayscale conversion doesn't reliably
separate two saturated colors. Rewritten to detect ink by color DISTANCE from
the crop's own background-corner sample instead, color-blind by construction.
These fixtures are real live captures that proved the old approach broken;
this test is a hard regression guard, not just a smoke test.
"""
import os

import cv2

from game.ocr import read_director_points

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "director_points")
_REGION = (0, 0, 20, 24)  # fixtures are already cropped to _DIRECTOR_POINTS_REGION's size


def _read(filename):
    img = cv2.imread(os.path.join(FIX, filename))
    assert img is not None, f"missing fixture: {filename}"
    return read_director_points(img, _REGION)


def test_pink_on_blue_06():
    assert _read("pink_on_blue_06.png") == 6


def test_pink_on_blue_07():
    assert _read("pink_on_blue_07.png") == 7


def test_white_on_purple_10():
    """A third, unrelated background color (light purple, not blue) with
    white digits -- confirms the fix generalizes by background color
    DISTANCE rather than assuming any particular background hue."""
    assert _read("white_on_purple_10.png") == 10

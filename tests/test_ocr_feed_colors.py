"""ocr_feed_text() against real captured colored-name banners
(tests/fixtures/feed_first_blood — see its README).

2026-09-10: the original min-channel-invert preprocessing could only tell
white text apart from a colored background — it went blind the moment a
player name itself was colored (every player name in this feed renders in
the same one fixed orange/red color, not per-player, distinct from the
white action words). Rewritten to detect the glyph OUTLINE instead (a
consistent dark near-black stroke around every letter regardless of fill
color) rather than the fill. These two fixtures are the real live captures
that proved the old approach broken and the new one fixed — this test is
a hard regression guard, not just a smoke test.
"""
import os

import cv2

from game.ocr import ocr_feed_text

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "feed_first_blood")
_REGION = (0, 0, 410, 200)  # fixtures are already cropped to _CROP_REGION's size


def _read(filename):
    img = cv2.imread(os.path.join(FIX, filename))
    assert img is not None, f"missing fixture: {filename}"
    return ocr_feed_text(img, _REGION)


def test_multi_line_damage_feed_reads_both_colored_names():
    text = _read("feed_first_blood_1.png")
    assert "PEFISS" in text
    assert "TWO" in text
    assert "DREW FIRST BLOOD FROM" in text


def test_single_line_first_blood_banner_reads_both_colored_names():
    text = _read("feed_first_blood_2.png")
    assert "TWO" in text
    assert "PEFISS" in text
    assert "DREW FIRST BLOOD FROM" in text

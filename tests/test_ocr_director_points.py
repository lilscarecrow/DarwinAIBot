"""Director points OCR (game/ocr.py::read_director_points), 2026-09-13 fix.

A single OCR misread on the 2-digit numerator crop could come back with a
spurious extra digit (e.g. "179" instead of a true single-digit value) --
the function had no plausibility bound even though the game caps points at
10. With director_points_use_pips disabled (no independent cross-check) and
match_runner.py's ratchet-up guard only rejecting reads that are too LOW,
one overshooting misread got permanently latched as the match's confirmed
points baseline -- confirmed live via logs/darwin_bot.log, where a match's
"Points ready" baseline sat at 179 and decayed only by each card's own cost
(179 -> 174 -> 171 -> ...), never correcting back down to reality. Beach
Party (cost 5) and everything after it then fired instantly all match,
since the fabricated balance always looked sufficient.

read_director_points() now rejects any parsed value above 10 outright,
returning None instead -- an out-of-range read isn't evidence of 10 points
either, so it's disregarded as a misread and folds into the same "keep the
last known value" path _update_points_reading() already takes for any other
failed read, rather than becoming a new (still-wrong) confirmed baseline.

These tests mock game.ocr._ocr_region_img directly (rather than feeding a
real screenshot through Tesseract) so they run headlessly.
"""
from unittest.mock import patch

import numpy as np

from game.ocr import read_director_points

_SCREENSHOT = np.zeros((1080, 1920, 3), dtype=np.uint8)
_REGION = (808, 1002, 20, 24)


def _read_with_ocr_text(raw_text: str):
    with patch("game.ocr._ocr_region_img", return_value=raw_text):
        return read_director_points(_SCREENSHOT, _REGION)


def test_normal_single_digit_read_is_used_as_is():
    assert _read_with_ocr_text("6") == 6


def test_normal_two_digit_read_is_used_as_is():
    assert _read_with_ocr_text("10") == 10


def test_the_actual_live_misread_is_rejected_not_trusted():
    # Real corrupted baseline from logs/darwin_bot.log, 2026-09-11 08:01:34 --
    # a spurious extra digit produced 179 instead of a true value <= 10.
    assert _read_with_ocr_text("179") is None


def test_other_live_overshoot_values_are_also_rejected():
    for raw in ("106", "77", "64", "56", "53", "50", "45", "40"):
        assert _read_with_ocr_text(raw) is None


def test_returns_none_when_no_digits_found():
    assert _read_with_ocr_text("") is None

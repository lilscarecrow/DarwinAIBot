"""Lobby-countdown OCR (game/ocr.py::read_lobby_countdown), 2026-09-10 fix.

A single unverified read used to drive the whole auto-start watcher, and
one bad read fired a match roughly 10 minutes early — raw OCR text
"(9:47" instead of "19:47", the leading "1" misread as a stray "("
character, exactly halving the real remaining time. A grep of months of
logs/darwin_bot.log showed every legitimate read is "19:XX" (hundreds of
them, never lower) with this exact single-digit-minutes failure recurring
intermittently — so read_lobby_countdown() now reconstructs a captured
single-digit minutes value as 10 + that digit instead of trusting it, and
bot/discord_bot.py no longer needs a second confirming read on top of it.

These tests mock pytesseract.image_to_string directly (rather than feeding
a real screenshot through Tesseract) so they run headlessly and pin down
the exact real-log strings this fix is for.
"""
from unittest.mock import patch

import numpy as np

from game.ocr import read_lobby_countdown

_SCREENSHOT = np.zeros((1080, 1920, 3), dtype=np.uint8)


def _read_with_ocr_text(raw_text: str):
    with patch("game.ocr.pytesseract.image_to_string", return_value=raw_text):
        return read_lobby_countdown(_SCREENSHOT)


def test_normal_two_digit_minutes_read_is_used_as_is():
    # Real log line, 2026-09-08 10:40:48.
    assert _read_with_ocr_text("T SHIT CUSTOM MATCH EXPIRES IN 19:38 Ad") == 19 * 60 + 38


def test_the_actual_live_misread_is_reconstructed():
    # Real log line, 2026-09-10 12:44:19 — the leading "1" of "19:47" read as "(".
    # Must reconstruct to 19m47s (1187s), not the literal 9m47s (587s) it used to
    # return, which is what fired the match roughly 10 minutes early.
    assert _read_with_ocr_text("(SHIT CUS TOM MATCH EXPIRES IN (9:47 |") == 19 * 60 + 47


def test_a_different_dropped_leading_glyph_is_also_reconstructed():
    # Same failure, different stray glyph in place of the "1" — regex-wise it's
    # identical (the glyph just isn't a digit), so this should behave the same.
    assert _read_with_ocr_text("SHIFT CUSTOM MATCH EXPIRES IN [9:39") == 19 * 60 + 39


def test_character_confusion_normalization_still_applies_before_reconstruction():
    # I/l/|/O -> digit normalization runs first; here it turns "O9:38" into "09:38",
    # a genuine two-digit capture ("09"), so no reconstruction should be applied.
    assert _read_with_ocr_text("CUSTOM MATCH EXPIRES IN O9:38") == 9 * 60 + 38


def test_returns_none_when_no_time_pattern_is_found():
    assert _read_with_ocr_text("CUSTOM MATCH EXPIRES IN") is None


def test_returns_none_on_empty_ocr_text():
    assert _read_with_ocr_text("") is None

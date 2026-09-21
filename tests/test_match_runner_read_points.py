"""_read_points()'s pip-only reading, with OCR logged but not trusted
(2026-09-17, explicitly temporary -- for live testing only).

Replaces the previous cross-validated design (OCR and pips both sampled,
agreement required only when both succeeded, either trusted alone when the
other was unavailable) with a simpler one: when director_points_use_pips is
on, the return value comes from pips alone. OCR is still read every call
(never skipped) so it can be logged alongside pips for later per-match
analysis via game/match_runner.py's "Points signals: ..." INFO line, but it
never influences the returned value or blocks/vetoes a pip read.

This is a deliberate downgrade, not a bugfix -- the explicit goal is to
gather enough live per-match data to judge whether pips alone are reliable
enough to drop OCR from this function permanently. See CLAUDE.md's Director
points reading section and game/match_runner.py::_read_points()'s own
docstring for the live comparisons (two separate live sessions, 12 frames and
74 frames) that motivated trying this.

director_points_use_pips is true in this repo's config.json as of this
change, but these tests patch runner._config directly rather than relying on
the file default, matching this file's existing convention.
"""
from unittest.mock import patch

from game.match_runner import MatchRunner
from session.state import SessionState


def make_runner(use_pips):
    return MatchRunner({"director_points_use_pips": use_pips}, SessionState(), lambda *a: None, draft_lifecycle=None)


# ---- director_points_use_pips: False -- pure OCR passthrough, unchanged -----

def test_pips_disabled_returns_ocr_alone():
    runner = make_runner(use_pips=False)
    with patch("game.ocr.read_director_points", return_value=7) as mock_ocr, \
         patch("game.ocr.read_director_point_pips") as mock_pips:
        assert runner._read_points(None) == 7
    mock_ocr.assert_called_once()
    mock_pips.assert_not_called()


def test_pips_disabled_passes_through_a_none_ocr_read():
    runner = make_runner(use_pips=False)
    with patch("game.ocr.read_director_points", return_value=None), \
         patch("game.ocr.read_director_point_pips") as mock_pips:
        assert runner._read_points(None) is None
    mock_pips.assert_not_called()


# ---- director_points_use_pips: True -- pip value always wins ---------------

def test_pips_enabled_returns_the_pip_value_when_signals_agree():
    runner = make_runner(use_pips=True)
    with patch("game.ocr.read_director_points", return_value=5), \
         patch("game.ocr.read_director_point_pips", return_value=5):
        assert runner._read_points(None) == 5


def test_pips_enabled_returns_the_pip_value_even_when_ocr_disagrees():
    """The actual behavioral change from the cross-validated design: a
    disagreeing OCR value used to force a None (distrust the frame). Now it
    doesn't get a vote at all -- pips alone decide."""
    runner = make_runner(use_pips=True)
    with patch("game.ocr.read_director_points", return_value=99), \
         patch("game.ocr.read_director_point_pips", return_value=6):
        assert runner._read_points(None) == 6


def test_pips_enabled_returns_the_pip_value_when_ocr_missed():
    runner = make_runner(use_pips=True)
    with patch("game.ocr.read_director_points", return_value=None), \
         patch("game.ocr.read_director_point_pips", return_value=5):
        assert runner._read_points(None) == 5


def test_pips_enabled_returns_none_when_pips_miss_even_if_ocr_succeeded():
    """OCR succeeding is no longer enough on its own -- it's logged, not
    trusted. A missing pip read means no confirmed value this poll, full
    stop, regardless of what OCR saw."""
    runner = make_runner(use_pips=True)
    with patch("game.ocr.read_director_points", return_value=7), \
         patch("game.ocr.read_director_point_pips", return_value=None):
        assert runner._read_points(None) is None


def test_pips_enabled_both_missing_returns_none():
    runner = make_runner(use_pips=True)
    with patch("game.ocr.read_director_points", return_value=None), \
         patch("game.ocr.read_director_point_pips", return_value=None):
        assert runner._read_points(None) is None


def test_pips_enabled_still_samples_ocr_every_call_for_logging():
    """OCR must never be skipped just because pips are the ones deciding --
    it's needed every call for the "Points signals: ..." log line that later
    per-match analysis depends on."""
    runner = make_runner(use_pips=True)
    with patch("game.ocr.read_director_points", return_value=None) as mock_ocr, \
         patch("game.ocr.read_director_point_pips", return_value=3):
        runner._read_points(None)
    mock_ocr.assert_called_once()


def test_pips_enabled_logs_both_signals(caplog):
    """The whole point of this temporary design: enough per-match data to
    later judge pip accuracy against OCR. Both raw values must appear in a
    single INFO-level line (not DEBUG -- this repo's log level is INFO, so a
    DEBUG line would be invisible in logs/darwin_bot.log, same lesson as the
    cross-validated design's own debug lines before it)."""
    import logging
    runner = make_runner(use_pips=True)
    with caplog.at_level(logging.INFO, logger="game.match_runner"):
        with patch("game.ocr.read_director_points", return_value=4), \
             patch("game.ocr.read_director_point_pips", return_value=6):
            runner._read_points(None)
    messages = [r.message for r in caplog.records]
    assert any("4" in m and "6" in m and "DISAGREE" in m for m in messages)

"""_read_points()'s pip-only reading (2026-09-23) — OCR dropped entirely.

From 2026-09-17 through this change, OCR (game.ocr.read_director_points) was
still sampled every call purely to log alongside pips for later comparison,
without ever influencing the returned value. That comparison data (~43,000
logged polls across 6 days of real matches) settled the question it was
gathered to answer: pips had a higher hit rate, far higher internal
consistency between consecutive reads, and were corroborated over OCR by
roughly 31:1 in disagreements with a clear signal — 86% of all disagreements
were specifically OCR failing to read a capped 10/10 bar (wrong or silent 71%
of the time in that state). See CLAUDE.md's Director points reading section
for the full writeup. `read_director_points()` and its dedicated
preprocessing were deleted from game/ocr.py along with this change.

_read_points() is now a thin one-line wrapper around
game.ocr.read_director_point_pips() — these tests exist mainly to pin that
down and to guard against OCR ever being silently reintroduced here.
"""
from unittest.mock import patch

from game.match_runner import MatchRunner
from session.state import SessionState


def make_runner():
    return MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=None)


def test_returns_the_pip_value():
    runner = make_runner()
    with patch("game.ocr.read_director_point_pips", return_value=6):
        assert runner._read_points(None) == 6


def test_returns_none_when_pips_miss():
    runner = make_runner()
    with patch("game.ocr.read_director_point_pips", return_value=None):
        assert runner._read_points(None) is None


def test_never_calls_ocr():
    """OCR was fully removed from this path -- read_director_points() no
    longer even exists in game.ocr, so a caller that somehow tried to import
    or call it here would fail; this pins down that _read_points() doesn't
    try."""
    import game.ocr
    assert not hasattr(game.ocr, "read_director_points")

    runner = make_runner()
    with patch("game.ocr.read_director_point_pips", return_value=6) as mock_pips:
        runner._read_points(None)
    mock_pips.assert_called_once()

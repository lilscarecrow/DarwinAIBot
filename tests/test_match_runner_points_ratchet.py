"""Director points confirmation-by-agreement (_update_points_reading), 2026-09-14.

Replaces a same-day fix that bounded the ratchet to a flat "+0 to +2" jump cap
(with a 30s stuck-timeout escape hatch). Found live within the hour of shipping
that fix: real regen over the ~12s background-sampling gap routinely exceeds
+2, so a legitimate jump got rejected, which left the baseline stale, which
made the next read's required jump even bigger -- in practice nearly every
background-poll update was routing through the 30s stuck-timeout instead of
ever landing cleanly (five separate stuck-timeout escapes inside 40 minutes of
one live match).

A flat cap can't fix this without knowing the true regen rate, which isn't
actually constant. Confirmation-by-agreement doesn't need to know it at all --
a read that disagrees with the confirmed value (higher OR lower) is only
trusted once an immediate second, independently-captured read lands on that
same value. This is symmetric by construction, so it also subsumes the old
stuck-timeout's job: nothing can stay stuck on a wrong baseline once two
consecutive real reads agree on the truth.
"""
from unittest.mock import patch

from game.match_runner import MatchRunner
from session.state import SessionState


def make_runner(confirmed=None):
    runner = MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=None)
    runner._last_confirmed_points = confirmed
    return runner


def _update_with_confirm(runner, current, confirm_value, context="test"):
    """Call _update_points_reading(current, ...) with the immediate second
    read (self._read_points(take_screenshot())) mocked to return confirm_value."""
    with patch.object(runner, "_read_points", return_value=confirm_value), \
         patch("game.screen_detection.take_screenshot", return_value=None):
        return runner._update_points_reading(current, context)


# ---- basic shape --------------------------------------------------------------

def test_first_ever_read_is_trusted_with_no_confirmation_needed():
    runner = make_runner(confirmed=None)
    with patch.object(runner, "_read_points") as read_points:
        result = runner._update_points_reading(3, "test")
    read_points.assert_not_called()  # nothing to confirm against yet
    assert result == 3


def test_a_failed_read_keeps_the_last_known_value():
    runner = make_runner(confirmed=5)
    with patch.object(runner, "_read_points") as read_points:
        result = runner._update_points_reading(None, "test")
    read_points.assert_not_called()
    assert result == 5


def test_a_read_matching_the_confirmed_value_needs_no_confirmation():
    runner = make_runner(confirmed=5)
    with patch.object(runner, "_read_points") as read_points:
        result = runner._update_points_reading(5, "test")
    read_points.assert_not_called()  # already agrees with what we believe -- nothing to check
    assert result == 5


# ---- disagreement requires a confirming second read ----------------------------

def test_a_higher_read_is_accepted_once_a_second_read_confirms_it():
    runner = make_runner(confirmed=2)
    result = _update_with_confirm(runner, current=7, confirm_value=7)
    assert result == 7


def test_a_higher_read_is_rejected_if_the_second_read_disagrees():
    """The live incident this fix is for: a single-frame jump that a second,
    independent read does not reproduce."""
    runner = make_runner(confirmed=2)
    result = _update_with_confirm(runner, current=7, confirm_value=2)
    assert result == 2  # unchanged


def test_a_lower_read_is_accepted_once_a_second_read_confirms_it():
    """Symmetric by construction -- a confirmed baseline that was itself
    wrong (too high) can now self-correct downward, unlike the old
    one-sided ratchet-up guard."""
    runner = make_runner(confirmed=9)
    result = _update_with_confirm(runner, current=2, confirm_value=2)
    assert result == 2


def test_a_lower_read_is_rejected_if_the_second_read_disagrees():
    runner = make_runner(confirmed=9)
    result = _update_with_confirm(runner, current=2, confirm_value=9)
    assert result == 9


def test_the_second_read_must_match_exactly_not_just_be_close():
    """A near-miss (e.g. the second read landing one tick further along from
    real regen) is not treated as agreement -- exact match only."""
    runner = make_runner(confirmed=2)
    result = _update_with_confirm(runner, current=7, confirm_value=8)
    assert result == 2


def test_a_failed_second_read_does_not_confirm_anything():
    runner = make_runner(confirmed=2)
    result = _update_with_confirm(runner, current=7, confirm_value=None)
    assert result == 2


# ---- a confirmed change actually takes effect (not just returned) --------------

def test_an_accepted_change_updates_last_confirmed_points():
    runner = make_runner(confirmed=2)
    _update_with_confirm(runner, current=7, confirm_value=7)
    assert runner._last_confirmed_points == 7


def test_a_rejected_change_leaves_last_confirmed_points_untouched():
    runner = make_runner(confirmed=2)
    _update_with_confirm(runner, current=7, confirm_value=3)
    assert runner._last_confirmed_points == 2

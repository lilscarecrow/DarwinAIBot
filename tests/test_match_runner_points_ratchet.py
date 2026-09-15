"""Director points confirmation-by-agreement (_update_points_reading), revised
2026-09-14.

A read that disagrees with the confirmed value is only trusted once it
matches self._last_valid_points_read -- the previous call's non-None read,
from whichever poll (background sampling or _wait_for_points()'s loop)
supplied it. A failed (None) read never touches _last_valid_points_read and
never counts as a "vote" on either side of the agreement check.

This replaced a same-day version that, on disagreement, forced an immediate
brand-new confirmation read right then. Found live: that confirmation
attempt could itself return None (an ordinary OCR miss, same as any other
read) -- and a None confirmation was being treated as "doesn't match",
rejecting an otherwise perfectly good `current` read for no reason other
than the confirmation attempt itself happening to whiff. Comparing against
the last already-successful read instead means a None never gets a vote at
all, on either side -- see test_a_null_confirmation_attempt_no_longer_causes_
a_false_rejection below for the exact scenario this fixes.

That in turn replaced an even earlier same-day fix that bounded the ratchet
to a flat "+0 to +2" jump cap (with a 30s stuck-timeout escape hatch) --
found live to reject nearly every real jump over the ~12s
background-sampling gap, since actual regen routinely exceeds +2 over that
span.
"""
from game.match_runner import MatchRunner
from session.state import SessionState


def make_runner(confirmed=None):
    runner = MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=None)
    runner._last_confirmed_points = confirmed
    return runner


# ---- basic shape --------------------------------------------------------------

def test_first_ever_read_is_trusted_with_no_agreement_needed():
    runner = make_runner(confirmed=None)
    result = runner._update_points_reading(3, "test")
    assert result == 3


def test_a_failed_read_keeps_the_last_known_value():
    runner = make_runner(confirmed=5)
    result = runner._update_points_reading(None, "test")
    assert result == 5


def test_a_read_matching_the_confirmed_value_needs_no_agreement():
    runner = make_runner(confirmed=5)
    result = runner._update_points_reading(5, "test")
    assert result == 5


# ---- disagreement requires the PREVIOUS valid read to agree --------------------

def test_a_lone_disagreeing_read_is_not_trusted_yet():
    runner = make_runner(confirmed=2)
    result = runner._update_points_reading(7, "test")
    assert result == 2  # unchanged -- nothing to agree with yet


def test_two_consecutive_agreeing_reads_are_accepted():
    """The live incident this whole mechanism is for: a single-frame jump
    isn't trusted, but the same value read twice in a row is."""
    runner = make_runner(confirmed=2)
    runner._update_points_reading(7, "test")   # first look — not yet trusted
    result = runner._update_points_reading(7, "test")  # agrees with the previous read
    assert result == 7


def test_symmetric_for_a_lower_read_too():
    """A confirmed baseline that was itself wrong (too high) can now
    self-correct downward, unlike the old one-sided ratchet-up guard."""
    runner = make_runner(confirmed=9)
    runner._update_points_reading(2, "test")
    result = runner._update_points_reading(2, "test")
    assert result == 2


def test_two_different_disagreeing_reads_in_a_row_confirm_nothing():
    """Pure noise -- neither read matches the other, so nothing is trusted;
    the second becomes the new candidate for whatever comes next."""
    runner = make_runner(confirmed=2)
    runner._update_points_reading(7, "test")
    result = runner._update_points_reading(8, "test")
    assert result == 2  # still unchanged


def test_agreement_can_come_from_a_read_several_calls_later():
    """The candidate persists across intervening failed reads -- it doesn't
    need to be the VERY next call, just the next VALID one."""
    runner = make_runner(confirmed=2)
    runner._update_points_reading(7, "test")
    runner._update_points_reading(None, "test")  # OCR miss in between
    result = runner._update_points_reading(7, "test")
    assert result == 7


# ---- the actual fix: a None confirmation no longer poisons agreement ----------

def test_a_null_confirmation_attempt_no_longer_causes_a_false_rejection():
    """The bug in the previous same-day version: forcing an immediate second
    read meant that read could itself return None (an ordinary OCR miss) and
    get treated as "doesn't match", falsely rejecting a perfectly good
    `current` read. Now a None read simply doesn't participate at all -- it
    neither confirms nor denies, and the real read from before it is still
    what the next real read gets checked against."""
    runner = make_runner(confirmed=2)
    runner._update_points_reading(7, "test")   # a real read, not yet trusted
    runner._update_points_reading(None, "test")  # the "confirmation" that used to whiff
    result = runner._update_points_reading(7, "test")  # the real second look
    assert result == 7  # confirmed anyway -- the None never got a vote


# ---- last_valid_points_read bookkeeping ---------------------------------------

def test_last_valid_points_read_updates_even_on_a_rejected_read():
    runner = make_runner(confirmed=2)
    runner._update_points_reading(7, "test")
    assert runner._last_valid_points_read == 7


def test_last_valid_points_read_is_untouched_by_a_failed_read():
    runner = make_runner(confirmed=2)
    runner._update_points_reading(7, "test")
    runner._update_points_reading(None, "test")
    assert runner._last_valid_points_read == 7


def test_last_valid_points_read_updates_on_a_read_matching_confirmed_too():
    runner = make_runner(confirmed=5)
    runner._update_points_reading(5, "test")
    assert runner._last_valid_points_read == 5

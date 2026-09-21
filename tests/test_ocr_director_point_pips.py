"""Director point pips via a self-referential per-frame split search
(game/ocr.py::read_director_point_pips), 2026-09-15 — replacing
count_director_point_pips()'s single-pixel brightness check.

The old approach couldn't survive two things confirmed live:
1. A same-colored background element at a pip's exact screen position reads
   as "filled" no matter how many pixels you sample there, if the whole
   sampled area agrees with the wrong answer -- averaging a bigger patch
   doesn't help against uniform contamination.
2. Each pip fills via a radial wipe (not an instant color swap), and the
   WHOLE ROW pulses together (grows/shrinks, shifts color) once points sit
   banked for a while -- so there's no fixed "what does filled look like"
   reference that stays valid over time.

The fix compares the row to itself each frame: since points only ever fill
left-to-right, the true state is always "first K filled, rest empty" (one of
count+1 possibilities) -- find whichever K best explains the observed
per-pip fill fractions, and only trust it if it clearly beats the next-best
candidate. These tests mock _pip_fill_fraction() directly (in the order
read_director_point_pips() samples pips, index 0..count-1) with real
fractions measured live, rather than synthesizing pixel-perfect images.
"""
from unittest.mock import patch

from game.ocr import read_director_point_pips, _PIP_SPLIT_CONFIDENCE_RATIO

PIPS_CFG = {"x_start": 862, "y": 1012, "spacing": 26, "count": 10}


def read_with_fractions(fractions):
    with patch("game.ocr._pip_fill_fraction", side_effect=list(fractions)):
        return read_director_point_pips(None, PIPS_CFG)


# ---- config / bounds handling ---------------------------------------------

def test_invalid_config_returns_none():
    assert read_director_point_pips(None, {}) is None


def test_a_single_out_of_bounds_pip_fails_the_whole_read():
    """_pip_fill_fraction() returning None (out of bounds) for even one pip
    must not be silently ignored -- the whole row read fails."""
    fractions = [0.9] * 4 + [None] + [0.9] * 5
    assert read_with_fractions(fractions) is None


# ---- the real live capture: 05/10, pip 5 mid-radial-fill -------------------

def test_the_actual_live_05_of_10_frame_reads_as_5():
    """Real fractions measured live: 5 confirmed filled (~0.92 each), the 6th
    pip mid-radial-wipe (~0.30 -- clearly short of "filled"), 4 genuinely
    empty (~0.07 each). Must read 5, not 6 -- the partially-filled pip should
    not be counted as earned yet, with no separate "-1 conservative" hack
    needed: it's numerically far closer to the empty cluster than the filled
    one, so the split search puts it on the empty side on its own."""
    fractions = [0.917, 0.917, 0.917, 0.917, 0.917, 0.300, 0.071, 0.071, 0.071, 0.071]
    assert read_with_fractions(fractions) == 5


def test_a_pip_at_075_no_longer_counts_once_the_floor_was_raised():
    """Was originally an unverified guess (== 6) documenting the split
    search's pre-floor-raise behavior (nearest-cluster wins, no separate
    "-1 always" hack) for a pip at 0.75 -- explicitly flagged in this test's
    own prior docstring as not confirmed against real ground truth. Once
    live monitoring confirmed 0.70-0.83 is where a pip actually still
    mid-wipe shows up (see _PIP_FILLED_GROUP_FLOOR's 2026-09-17 comment in
    game/ocr.py), the honest answer for 0.75 became None, same as the rest
    of that band -- this is a corrected expectation, not a regression."""
    fractions = [0.92] * 5 + [0.75] + [0.07] * 4
    assert read_with_fractions(fractions) is None


# ---- whole-row extremes: no second group to compare against ---------------

def test_all_ten_uniformly_high_reads_as_the_full_count():
    """The exact failure case a live capture caught: a fully-capped (10/10)
    meter pulsing as one synchronized group has no internal filled/empty
    contrast at all -- the interior split search isn't equipped to judge
    this confidently (there's no real "other side"), so the uniform-high
    floor check must catch it before the split search ever runs."""
    fractions = [0.91, 0.91, 0.91, 0.91, 0.92, 0.92, 0.93, 0.93, 0.92, 0.92]
    assert read_with_fractions(fractions) == 10


def test_all_ten_uniformly_low_reads_as_zero():
    fractions = [0.05, 0.06, 0.04, 0.05, 0.03, 0.06, 0.05, 0.04, 0.05, 0.06]
    assert read_with_fractions(fractions) == 0


# ---- ambiguous ripple: split search must refuse, not guess -----------------

def test_a_dim_moment_in_the_pulse_cycle_still_reads_as_the_full_count():
    """Real fractions measured live: a genuinely-10/10, mid-idle-pulse row
    caught at the DIM end of its ~1s brightness cycle (not the bright end
    the other "all high" test uses) -- a slight left-right ripple too, since
    the pulse isn't perfectly synchronized pixel-for-pixel. Every value still
    clears _PIP_ALL_HIGH_FLOOR, so this must read as the full count, not get
    mistaken for an ambiguous interior split -- this is the exact live
    capture that first exposed the naive "always trust the best split"
    failure mode this design's confidence gate exists to close."""
    fractions = [0.65, 0.63, 0.63, 0.64, 0.68, 0.70, 0.73, 0.73, 0.73, 0.73]
    assert read_with_fractions(fractions) == 10


def test_a_weak_split_below_the_confidence_bar_is_rejected():
    """A gentle hump entirely within the mid-range (never clearing the
    all-high floor or dropping below the all-low ceiling) with no decisive
    gap anywhere -- every candidate split is about as good (or bad) as the
    others, so nothing should be trusted."""
    fractions = [0.20, 0.25, 0.30, 0.35, 0.40, 0.42, 0.38, 0.33, 0.28, 0.22]
    assert read_with_fractions(fractions) is None


# ---- a genuine interior split still needs a real margin, not just "best" ---

def test_a_clear_boundary_with_a_wide_margin_is_trusted():
    """Sanity check on the confidence-ratio constant itself: a boundary this
    clean must clear the bar."""
    fractions = [0.90] * 3 + [0.08] * 7
    result = read_with_fractions(fractions)
    assert result == 3


def test_confidence_ratio_constant_is_a_real_gate_not_a_no_op():
    """Guards against a future edit accidentally setting the ratio to >= 1.0
    (which would make the gate meaningless, accepting every split)."""
    assert 0 < _PIP_SPLIT_CONFIDENCE_RATIO < 1


# ---- absolute filled-group floor (2026-09-15 fix) --------------------------

def test_a_half_filled_pip_is_not_smuggled_into_the_filled_group():
    """The actual live misread this fix closes: read_director_point_pips()
    wrongly returned 2 for the game's own '01/10' state. The genuinely-filled
    pip measured only ~0.74 this frame (a dimmer, non-pulsing render state,
    well below the ~0.92 typical of the bright pulse state) -- close enough
    to the half-filled second pip's ~0.61 that the relative confidence-ratio
    gate alone found a clean, well-separated {0.74, 0.61} vs {empty} split
    and trusted it. The absolute floor added on top of that gate must reject
    this specific split, even though it clears the confidence-ratio check."""
    fractions = [0.743, 0.609, 0.103, 0.02, 0.043, 0.039, 0.032, 0.016, 0.024, 0.055]
    assert read_with_fractions(fractions) is None


def test_a_dim_pip_below_the_raised_floor_is_now_rejected_rather_than_guessed():
    """2026-09-17: the floor was raised 0.65 -> 0.80 (see its own comment in
    game/ocr.py for the live monitoring data behind this), specifically
    because a live session found the 0.65 floor still let a still-filling
    pip (0.70-0.72, visibly climbing frame to frame) get counted as done. A
    side effect: the single historical ~0.74 "genuinely filled but dim" case
    that originally justified 0.65 no longer clears the bar either -- that's
    an accepted trade-off, not an oversight (see the comment for why): that
    exact state never recurred across ~4 minutes of fresh live monitoring,
    while every genuinely-settled filled pip in that same session read
    0.84-0.96. If it turns out to be a real, recurring render state, the
    floor should come back down and this test's expectation would need to
    flip again -- but the honest current answer is None, not a guess."""
    fractions = [0.74] * 3 + [0.08] * 7
    assert read_with_fractions(fractions) is None


def test_a_still_filling_pip_in_the_high_0_70s_is_rejected_not_counted():
    """The actual live overcount this floor raise closes: a pip mid-radial-
    wipe measured 0.72 then 0.83 across two consecutive live polls a few
    seconds apart (still visibly climbing, not yet settled) while OCR
    steadily read one lower for the same span -- read_director_point_pips()
    wrongly counted it as filled (5) at the OLD 0.65 floor. Pinned here at
    the earlier, more clear-cut of the two captured moments (0.72). The
    split search still picks K=5 as its best SSE fit (that part is
    unaffected by the floor), so the corrected behavior is a flat rejection
    (None), not a fallback down to K=4 -- there's no such fallback in the
    design; a failed floor check distrusts the whole frame, same as a
    failed confidence-ratio check does elsewhere in this function."""
    fractions = [0.94, 0.94, 0.94, 0.94, 0.72, 0.09, 0.09, 0.09, 0.09, 0.09]
    assert read_with_fractions(fractions) is None


def test_a_settled_filled_pip_at_the_low_end_of_the_observed_range_still_counts():
    """The lowest genuinely-correct filled-group minimum seen in the same
    live session that motivated the floor raise (0.84, alongside 0.88 and
    0.92 -- all three settled, not climbing) -- confirms 0.80 doesn't
    overcorrect into rejecting real, already-complete fills."""
    fractions = [0.84, 0.88, 0.92, 0.06, 0.0, 0.0, 0.06, 0.09, 0.11, 0.11]
    assert read_with_fractions(fractions) == 3

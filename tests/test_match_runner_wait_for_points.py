"""_wait_for_points()'s settle delay (2026-09-20, found live) — a pip crossing
_PIP_FILLED_GROUP_FLOOR can still be visibly climbing toward its settled
fraction, and _update_points_reading()'s agreement ratchet only needs two
polls landing on the same resolved pip count to confirm a change — which can
happen within a second or two of first crossing the floor, well before the
fill animation (and, it turned out, the game's own authoritative point count)
has actually finished. Confirmed live: Electromania at 6:30 fired the instant
a 2->3 jump was confirmed by two close-together reads, but the recording
showed the 3rd pip not yet fully filled at the moment the card played — the
game rejected it, and every card after it then computed the wrong tray
position (nothing verifies zone_close/tray removal by design), cascading into
every remaining card that match.

Fix: once the threshold is first met, wait _POINTS_SETTLE_SECONDS before
actually declaring ready. This is a plain delay, not a re-verification —
_update_points_reading()'s ratchet can't be un-confirmed by a single later
read either way, so a recheck read couldn't catch a wrong-but-already-
confirmed value; the delay is what does the real work, giving the animation
(and whatever server tick backs it) a moment to actually finish.
"""
from unittest.mock import MagicMock, patch

from game.match_runner import MatchRunner, _POINTS_SETTLE_SECONDS
from session.state import SessionState


def make_runner(confirmed=None):
    runner = MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=None)
    runner._last_confirmed_points = confirmed
    runner._stop = MagicMock()
    runner._stop.is_set.return_value = False
    runner._stop.wait.return_value = False  # not force-stopped during any wait
    return runner


def patch_screenshot():
    return patch("game.screen_detection.take_screenshot", return_value="frame")


def test_returns_immediately_when_needed_is_zero():
    runner = make_runner()
    with patch.object(runner, "_read_points") as read_points:
        assert runner._wait_for_points(0, "test") is False
    read_points.assert_not_called()


def test_waits_out_the_settle_delay_once_the_threshold_is_first_met():
    """Already at/above the threshold on the very first read (e.g. the
    background sampler already confirmed it before this call started) —
    must not return ready without waiting out the settle delay first."""
    runner = make_runner(confirmed=3)
    with patch_screenshot(), patch.object(runner, "_read_points", return_value=3):
        result = runner._wait_for_points(3, "test")
    assert result is False  # closed_broadcast — no "waiting" branch was needed
    runner._stop.wait.assert_called_once_with(_POINTS_SETTLE_SECONDS)


def test_only_one_read_per_poll_even_though_the_threshold_is_met():
    """The settle delay does not trigger a second read -- see the module
    docstring for why a recheck read couldn't meaningfully veto an
    already-confirmed value anyway; this is a plain delay, not a re-poll."""
    runner = make_runner(confirmed=3)
    with patch_screenshot(), patch.object(runner, "_read_points", return_value=3) as read_points:
        runner._wait_for_points(3, "test")
    read_points.assert_called_once()


def test_force_stop_during_the_settle_delay_returns_without_declaring_ready():
    runner = make_runner(confirmed=3)
    runner._stop.wait.return_value = True  # force-stop fires during the settle delay
    with patch_screenshot(), patch.object(runner, "_read_points", return_value=3):
        result = runner._wait_for_points(3, "test")
    assert result is False


def test_still_waits_normally_when_below_the_threshold():
    """The settle delay only applies once the threshold is actually met --
    below it, this is the ordinary 2s poll wait, unchanged."""
    runner = make_runner(confirmed=0)
    runner._stop.is_set.side_effect = [False, True]  # stop the loop after one poll
    with patch_screenshot(), patch.object(runner, "_read_points", return_value=0):
        result = runner._wait_for_points(3, "test")
    assert result is False
    runner._stop.wait.assert_called_once_with(2.0)


def test_reaching_the_threshold_after_waiting_still_settles_before_returning():
    """Confirmed via agreement partway through the loop (not on the very
    first read) -- the settle delay still applies to that transition."""
    runner = make_runner(confirmed=2)
    # First read (3) disagrees and isn't yet trusted; second read (3) agrees
    # with the first and gets confirmed -- that's the moment settling kicks in.
    with patch_screenshot(), patch.object(runner, "_read_points", side_effect=[3, 3]):
        result = runner._wait_for_points(3, "test")
    assert result is False
    assert runner._stop.wait.call_args_list[-1][0][0] == _POINTS_SETTLE_SECONDS

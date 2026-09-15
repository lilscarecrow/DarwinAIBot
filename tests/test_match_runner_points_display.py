"""OBS "current points" text source (2026-09-14) — MatchRunner._update_points_display()
pushes self._last_confirmed_points to obs_points_source (config) whenever it actually
changes, so the streamer can watch what the bot currently believes its director points
balance is live on the OBS layout, without tailing the log.

Only fires on an actual value change (see the call sites in _update_points_reading()/
_debit_points()) — not on every poll — since a websocket round trip on every ~2s
_wait_for_points() tick would add real, avoidable latency to card-timing precision.
"""
from unittest.mock import patch

from game.match_runner import MatchRunner
from session.state import SessionState


def make_runner(confirmed=None, source="Director Points"):
    config = {"obs_points_source": source} if source else {}
    runner = MatchRunner(config, SessionState(), lambda *a: None, draft_lifecycle=None)
    runner._last_confirmed_points = confirmed
    return runner


# ---- format -------------------------------------------------------------------

def test_shows_value_over_ten():
    runner = make_runner(confirmed=7)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._update_points_display()
    set_text.assert_called_once_with("Director Points", "7/10")


def test_shows_a_placeholder_when_nothing_confirmed_yet():
    runner = make_runner(confirmed=None)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._update_points_display()
    set_text.assert_called_once_with("Director Points", "?/10")


# ---- no-ops -------------------------------------------------------------------

def test_noops_when_obs_streaming_is_disabled():
    runner = make_runner(confirmed=5)
    with patch("game.obs_control.is_enabled", return_value=False), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._update_points_display()
    set_text.assert_not_called()


def test_noops_when_no_source_is_configured():
    """Opt-in, unlike obs_game_number_source — empty by default since it
    needs an OBS source the streamer hasn't necessarily set up yet."""
    runner = make_runner(confirmed=5, source=None)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._update_points_display()
    set_text.assert_not_called()


# ---- only pushes on an actual change -------------------------------------------

def test_update_points_reading_pushes_on_a_confirmed_change():
    runner = make_runner(confirmed=2)
    runner._update_points_reading(3, "test")  # first look — not yet trusted, no push
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._update_points_reading(3, "test")  # a second read agrees — confirmed
    set_text.assert_called_once_with("Director Points", "3/10")


def test_update_points_reading_does_not_push_when_the_value_is_unchanged():
    runner = make_runner(confirmed=5)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._update_points_reading(5, "test")  # same value — trusted, but no real change
    set_text.assert_not_called()


def test_update_points_reading_does_not_push_on_an_unconfirmed_read():
    runner = make_runner(confirmed=2)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._update_points_reading(7, "test")  # a lone disagreeing read — not yet trusted
    set_text.assert_not_called()


def test_debit_points_pushes_on_a_real_change():
    runner = make_runner(confirmed=5)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._debit_points(1)
    set_text.assert_called_once_with("Director Points", "4/10")


def test_debit_points_does_not_push_for_a_zero_cost_card():
    """favorite_player costs 0 — a debit that changes nothing shouldn't push."""
    runner = make_runner(confirmed=5)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._debit_points(0)
    set_text.assert_not_called()


def test_debit_points_pushes_the_placeholder_when_cost_is_unknown():
    runner = make_runner(confirmed=5)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._debit_points(None)
    set_text.assert_called_once_with("Director Points", "?/10")


def test_debit_points_does_not_push_when_already_unconfirmed():
    runner = make_runner(confirmed=None)
    with patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._debit_points(None)
    set_text.assert_not_called()

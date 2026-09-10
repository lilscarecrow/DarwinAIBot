"""Director points bookkeeping after a card plays (2026-09-10 fix).

A played card's cost is always known (CARD_POINT_COSTS/CardEvent.points_cost),
so _debit_points() subtracts it directly from self._last_confirmed_points
instead of the old approach — invalidating the whole value to None and
waiting on a fresh OCR read to re-establish a baseline. That old approach
made back-to-back cards each pay for a full re-read even though the bot
already knew exactly what the new value should be.
"""
from unittest.mock import patch

import pytest

from game.match_runner import MatchRunner, CardEvent
from session.state import SessionState


def make_runner(confirmed=10, deck_layout=None, verify_plays=False, config=None):
    runner = MatchRunner(config or {}, SessionState(), lambda *a: None, draft_lifecycle=None)
    runner._last_confirmed_points = confirmed
    runner._deck_layout = list(deck_layout if deck_layout is not None else ["give_wood"])
    runner._verify_plays = verify_plays
    return runner


def make_event(card_type="give_wood", points_cost=1, deck_position=0):
    return CardEvent(
        name=card_type, card_type=card_type, trigger_seconds=0, play_time_seconds=0,
        deck_position=deck_position, drop_target=(960, 540), points_cost=points_cost,
    )


# ---- _debit_points (pure logic) ---------------------------------------------

def test_subtracts_cost_from_the_known_baseline():
    runner = make_runner(confirmed=10)
    runner._debit_points(1)
    assert runner._last_confirmed_points == 9


def test_clamps_at_zero_rather_than_going_negative():
    runner = make_runner(confirmed=2)
    runner._debit_points(5)
    assert runner._last_confirmed_points == 0


def test_falls_back_to_invalidating_when_cost_is_unknown():
    runner = make_runner(confirmed=10)
    runner._debit_points(None)
    assert runner._last_confirmed_points is None


def test_falls_back_to_invalidating_when_there_is_no_confirmed_baseline():
    runner = make_runner(confirmed=None)
    runner._debit_points(3)
    assert runner._last_confirmed_points is None


# ---- _play_tray_card(): the actual call site ---------------------------------

def test_a_successful_play_debits_the_cards_cost_not_a_full_invalidate():
    runner = make_runner(confirmed=10, verify_plays=False)  # trust-without-verification path
    event = make_event(card_type="give_wood", points_cost=1)
    with patch("game.card_actions.play_card"), patch("game.tts.speak_cable"), patch("game.tts.try_open_broadcast"):
        runner._play_tray_card(event, (960, 540), "Give Wood", None, broadcast_open=False)
    assert runner._last_confirmed_points == 9


def test_a_failed_verified_play_leaves_the_known_points_untouched():
    """verify_card_plays: true path — a play that never verifies in 2
    attempts spent nothing, so the known baseline must be left exactly as
    it was, not invalidated and not debited."""
    runner = make_runner(confirmed=10, verify_plays=True)
    event = make_event(card_type="give_wood", points_cost=1)
    with patch("game.card_actions.play_card"), \
         patch("game.card_actions.shift_down"), patch("game.card_actions.shift_up"), \
         patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.screen_detection.save_error_screenshot"), \
         patch.object(runner, "_verify_card_removed", return_value=False), \
         patch("game.tts.speak_cable"):
        runner._play_tray_card(event, (960, 540), "Give Wood", None, broadcast_open=False)
    assert runner._last_confirmed_points == 10


def test_bypass_mode_still_debits_since_the_play_is_trusted():
    runner = make_runner(confirmed=10, verify_plays=True)
    runner._bypass = True
    event = make_event(card_type="give_wood", points_cost=1)
    with patch("game.card_actions.play_card"), patch("game.tts.speak_cable"), \
         patch("builtins.input", return_value=""):
        runner._play_tray_card(event, (960, 540), "Give Wood", None, broadcast_open=False)
    assert runner._last_confirmed_points == 9


def test_unknown_card_cost_falls_back_to_invalidating():
    runner = make_runner(confirmed=10, verify_plays=False)
    event = make_event(card_type="not_a_real_card_type", points_cost=None)
    with patch("game.card_actions.play_card"), patch("game.tts.speak_cable"):
        runner._play_tray_card(event, (960, 540), "Mystery Card", None, broadcast_open=False)
    assert runner._last_confirmed_points is None


# ---- _attempt_zone_close(): the other call site, no verification at all ----

def test_zone_close_always_debits_its_cost():
    from game.deck_utils import CARD_POINT_COSTS
    runner = make_runner(confirmed=10, deck_layout=["zone_close"], verify_plays=True)
    with patch("game.card_actions.play_card"), patch("game.tts.speak_cable"):
        assert runner._attempt_zone_close() is True
    assert runner._last_confirmed_points == 10 - CARD_POINT_COSTS["zone_close"]

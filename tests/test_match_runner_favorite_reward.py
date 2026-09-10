"""Crowd Favorite channel-points reward (2026-09-10) — queued via
try_queue_favorite_reward() (called cross-thread from bot/twitch_bot.py),
fired by the main loop's _maybe_fire_favorite_reward() once it's safe to.

Same queue/fire/cancel shape as the first-blood reward
(tests/test_match_runner_first_blood_reward.py), minus the points check
(favorite_player costs 0) and with "only one pending at a time" instead of
a permanent one-shot latch.
"""
from unittest.mock import patch

import pytest

from game.match_runner import MatchRunner, CardEvent
from session.state import SessionState


@pytest.fixture(autouse=True)
def _no_pov_settle_delay(monkeypatch):
    """_give_reward_card() waits _POV_SWITCH_SETTLE_SECONDS (real time,
    2026-09-10) after the POV switch before dragging the card — zeroed here
    so these tests stay fast; the delay itself isn't what's under test."""
    monkeypatch.setattr("game.match_runner._POV_SWITCH_SETTLE_SECONDS", 0)


def make_runner(names=None, alive=None, deck_layout=None, config=None):
    runner = MatchRunner(config or {}, SessionState(), lambda *a: None, draft_lifecycle=None)
    runner._session.unlock_pov()  # see test_match_runner_first_blood_reward.py's make_runner
    runner._player_names = list(names or ["SteffKnight", "Hellcrying", "Guts"])
    runner._player_alive = list(alive) if alive is not None else [True] * len(runner._player_names)
    runner._deck_layout = list(deck_layout if deck_layout is not None else ["favorite_player"])
    return runner


def make_schedule(*trigger_seconds):
    return [
        CardEvent(name=f"card{i}", card_type="electromania", trigger_seconds=t,
                  play_time_seconds=t, deck_position=None, drop_target=(960, 540))
        for i, t in enumerate(trigger_seconds)
    ]


# ---- try_queue_favorite_reward ----------------------------------------------

def test_accepts_a_valid_redemption():
    runner = make_runner()
    assert runner.try_queue_favorite_reward(1) is True
    assert runner._favorite_reward_target == 1


def test_advanced_cards_off_rejects_every_redemption():
    runner = make_runner(config={"advanced_cards": False})
    assert runner.try_queue_favorite_reward(1) is False
    assert runner._favorite_reward_target is None


def test_rejects_a_second_redemption_while_one_is_already_queued():
    runner = make_runner()
    assert runner.try_queue_favorite_reward(1) is True
    assert runner.try_queue_favorite_reward(2) is False
    assert runner._favorite_reward_target == 1  # unchanged


def test_rejects_an_out_of_range_index():
    runner = make_runner()  # 3 players, indices 0-2
    assert runner.try_queue_favorite_reward(5) is False
    assert runner._favorite_reward_target is None


def test_accepts_an_index_with_no_captured_name_if_still_alive():
    """2026-09-10 fix: unlike first blood, this flow never matches a name —
    the viewer names the slot directly. A real, alive player whose nameplate
    simply failed to OCR at match start must not be rejected just because
    their specific name didn't resolve; self._slot_name() falls back to
    "slot N" for logging/TTS, so nothing needs the name to be present."""
    runner = make_runner(names=["SteffKnight", "", "Guts"])
    assert runner.try_queue_favorite_reward(1) is True
    assert runner._favorite_reward_target == 1


def test_rejects_an_already_eliminated_target():
    runner = make_runner(alive=[True, False, True])
    assert runner.try_queue_favorite_reward(1) is False


def test_rejects_when_the_deck_has_no_favorite_player_card():
    runner = make_runner(deck_layout=["electromania", "beach_party"])
    assert runner.try_queue_favorite_reward(1) is False


def test_rejects_once_all_favorite_player_copies_are_already_played():
    runner = make_runner(deck_layout=["favorite_player"])
    runner._deck_played.add(0)
    assert runner.try_queue_favorite_reward(1) is False


# ---- _maybe_fire_favorite_reward ---------------------------------------------

def test_noop_when_nothing_is_queued():
    runner = make_runner()
    with patch.object(runner, "_give_favorite_reward") as give:
        runner._maybe_fire_favorite_reward(elapsed=100.0, card_schedule=make_schedule(200))
    give.assert_not_called()


def test_cancels_if_the_target_died_before_the_reward_could_be_given():
    runner = make_runner()
    runner._favorite_reward_target = 1
    runner._player_alive[1] = False
    with patch.object(runner, "_give_favorite_reward") as give:
        runner._maybe_fire_favorite_reward(elapsed=100.0, card_schedule=make_schedule(200))
    give.assert_not_called()
    assert runner._favorite_reward_target is None  # released, not stuck


def test_waits_when_a_scheduled_card_is_due_soon():
    runner = make_runner()
    runner._favorite_reward_target = 1
    with patch.object(runner, "_give_favorite_reward") as give:
        runner._maybe_fire_favorite_reward(elapsed=100.0, card_schedule=make_schedule(103))
    give.assert_not_called()
    assert runner._favorite_reward_target == 1  # still queued, try again later


def test_fires_once_a_scheduled_card_is_far_enough_away():
    runner = make_runner()
    runner._favorite_reward_target = 1
    with patch.object(runner, "_give_favorite_reward") as give:
        runner._maybe_fire_favorite_reward(elapsed=100.0, card_schedule=make_schedule(110))
    give.assert_called_once_with(1, 0)
    assert runner._favorite_reward_target is None


def test_fires_with_no_points_check_at_all():
    """favorite_player costs 0 — unlike first blood's give_wood reward,
    self._last_confirmed_points must never gate this."""
    runner = make_runner()
    runner._favorite_reward_target = 1
    runner._last_confirmed_points = 0
    with patch.object(runner, "_give_favorite_reward") as give:
        runner._maybe_fire_favorite_reward(elapsed=100.0, card_schedule=[])
    give.assert_called_once()


def test_cancels_when_the_deck_has_no_favorite_player_card_left_at_fire_time():
    """Defensive: shouldn't normally happen since try_queue already checked
    availability, but a card could be played in between (see the deck-shift
    tests in test_match_runner_first_blood_reward.py) — must not crash."""
    runner = make_runner(deck_layout=["favorite_player"])
    runner._favorite_reward_target = 1
    runner._deck_played.add(0)  # the only copy got played while queued
    with patch.object(runner, "_give_favorite_reward") as give:
        runner._maybe_fire_favorite_reward(elapsed=100.0, card_schedule=[])
    give.assert_not_called()
    assert runner._favorite_reward_target is None


def test_releases_the_slot_before_firing_so_a_new_redemption_can_queue_immediately():
    """Unlike first blood (a permanent one-shot latch), Crowd Favorite must
    accept a brand new redemption right after this one resolves — the slot
    is reset to None, not a separate 'resolved forever' flag."""
    runner = make_runner(deck_layout=["favorite_player", "favorite_player"])
    runner._favorite_reward_target = 1

    def check_slot_is_free_during_call(*a, **k):
        assert runner._favorite_reward_target is None
        assert runner.try_queue_favorite_reward(2) is True

    with patch.object(runner, "_give_favorite_reward", side_effect=check_slot_is_free_during_call):
        runner._maybe_fire_favorite_reward(elapsed=100.0, card_schedule=[])
    assert runner._favorite_reward_target == 2  # the new redemption queued during the call


# ---- _give_favorite_reward (thin wrapper around _give_reward_card) ---------

def test_give_favorite_reward_plays_favorite_player_at_center():
    runner = make_runner()
    with patch.object(runner, "_press") as press, \
         patch.object(runner, "_play_tray_card") as play:
        runner._give_favorite_reward(player_index=1, deck_pos=0)
    press.assert_called_once_with("2")
    play.assert_called_once()
    event, target, *_ = play.call_args[0]
    assert target == (960, 540)
    assert event.card_type == "favorite_player"
    assert event.deck_position == 0
    assert runner._session.is_pov_locked() is False

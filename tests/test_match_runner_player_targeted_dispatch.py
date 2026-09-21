"""Player-targeted card dispatch (_fire_card_event, the _PLAYER_TARGETED_CARDS
branch) — rewritten 2026-09-14 to target a random currently-alive player's own
portrait via _player_portrait_target(), replacing the never-calibrated
player_target_coordinates config path (which always skipped the play). No
profile currently schedules a card in this set (only give_wood/favorite_player
fire today, both through _give_reward_card() instead — see
test_match_runner_first_blood_reward.py / test_match_runner_favorite_reward.py)
— this covers the dispatch path itself so it works out of the box whenever
one is.
"""
from unittest.mock import patch

from game.match_runner import (
    MatchRunner, CardEvent, _PLAYER_PORTRAIT_TARGET_Y, _PLAYER_TARGETED_DRAG_Y_OFFSET,
    _PORTRAIT_DROP_HOLD_SECONDS,
)
from session.state import SessionState


def make_runner(alive, config=None):
    runner = MatchRunner(config or {}, SessionState(), lambda *a: None, draft_lifecycle=None)
    runner._player_alive = list(alive)
    runner._player_slot_xs = [100 * (i + 1) for i in range(len(alive))]
    return runner


def make_event(card_type="give_leather", points_cost=None):
    return CardEvent(
        name=card_type, card_type=card_type, trigger_seconds=0, play_time_seconds=0,
        deck_position=0, drop_target=None, points_cost=points_cost,
    )


def test_targets_the_one_alive_players_own_portrait():
    runner = make_runner(alive=[False, True, False])
    with patch.object(runner, "_play_tray_card") as play:
        runner._fire_card_event(make_event(), all_events=[])
    play.assert_called_once()
    event, target, *_ = play.call_args[0]
    assert target == (runner._player_slot_xs[1], _PLAYER_PORTRAIT_TARGET_Y + _PLAYER_TARGETED_DRAG_Y_OFFSET)


def test_holds_at_the_portrait_before_releasing():
    """2026-09-15 fix: portrait drops need a brief hold before release — see
    test_match_runner_first_blood_reward.py::test_give_holds_at_the_portrait_before_releasing
    for the live miss that motivated this. This dispatch path must forward
    the same _PORTRAIT_DROP_HOLD_SECONDS constant."""
    runner = make_runner(alive=[False, True, False])
    with patch.object(runner, "_play_tray_card") as play:
        runner._fire_card_event(make_event(), all_events=[])
    assert play.call_args.kwargs["hold_at_target_seconds"] == _PORTRAIT_DROP_HOLD_SECONDS


def test_skips_when_no_alive_player_is_tracked():
    runner = make_runner(alive=[False, False])
    with patch.object(runner, "_play_tray_card") as play:
        runner._fire_card_event(make_event(), all_events=[])
    play.assert_not_called()


def test_skips_when_the_player_bar_was_never_tracked_at_all():
    runner = make_runner(alive=[])
    with patch.object(runner, "_play_tray_card") as play:
        runner._fire_card_event(make_event(), all_events=[])
    play.assert_not_called()

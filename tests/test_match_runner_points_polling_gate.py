"""_points_still_needed() (2026-09-23) — gates run()'s main-loop background
director-points sampling. Once the card schedule is exhausted, nothing left
in a match needs a fresh points read except a still-pending first-blood
give_wood reward (favorite_player costs 0 and is refused outright once the
schedule is exhausted anyway — see _past_last_scheduled_card()), so sampling
should stop rather than keep paying a screenshot+OCR read every poll interval
for the rest of the match.
"""
from game.match_runner import MatchRunner, CardEvent
from session.state import SessionState


def make_runner():
    return MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=None)


def make_schedule(*trigger_seconds):
    return [
        CardEvent(name=f"card{i}", card_type="electromania", trigger_seconds=t,
                  play_time_seconds=t, deck_position=None, drop_target=(960, 540))
        for i, t in enumerate(trigger_seconds)
    ]


def test_needed_when_no_schedule_has_been_set_yet():
    """Same 'unknown is not the same as exhausted' rule as
    _past_last_scheduled_card() itself."""
    runner = make_runner()
    assert runner._card_schedule == []
    assert runner._points_still_needed() is True


def test_needed_while_the_schedule_still_has_pending_cards():
    runner = make_runner()
    runner._card_schedule = make_schedule(200, 400)
    assert runner._points_still_needed() is True


def test_not_needed_once_the_schedule_is_exhausted_and_nothing_else_pending():
    runner = make_runner()
    schedule = make_schedule(200, 400)
    for e in schedule:
        e.done = True
    runner._card_schedule = schedule
    assert runner._points_still_needed() is False


def test_still_needed_once_exhausted_if_a_first_blood_reward_is_still_pending():
    runner = make_runner()
    schedule = make_schedule(200)
    schedule[0].done = True
    runner._card_schedule = schedule
    runner._first_blood_reward_slot = 3
    runner._first_blood_reward_resolved = False
    assert runner._points_still_needed() is True


def test_not_needed_once_exhausted_if_the_first_blood_reward_already_resolved():
    runner = make_runner()
    schedule = make_schedule(200)
    schedule[0].done = True
    runner._card_schedule = schedule
    runner._first_blood_reward_slot = 3
    runner._first_blood_reward_resolved = True
    assert runner._points_still_needed() is False


def test_not_needed_once_exhausted_if_no_first_blood_reward_was_ever_queued():
    runner = make_runner()
    schedule = make_schedule(200)
    schedule[0].done = True
    runner._card_schedule = schedule
    assert runner._first_blood_reward_slot is None
    assert runner._points_still_needed() is False

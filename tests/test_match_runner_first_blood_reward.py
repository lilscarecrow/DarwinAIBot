"""First-blood reward (give_wood) — queued when a killer is confirmed via
the damage feed, fired by the main loop once it's safe to (2026-09-10).
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


class FakeDraftLifecycle:
    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))


def make_runner(names=None, deck_layout=None, ds=None, config=None):
    runner = MatchRunner(config or {}, SessionState(), lambda *a: None, draft_lifecycle=ds)
    # MatchRunner.__init__ locks POV itself (belt-and-suspenders on top of
    # /custom's own lock — see SessionState._pov_locked's docstring),
    # unlocked later in run() right after the forced default-POV-1 press.
    # First blood can only happen well after that point, so every one of
    # these tests represents mid-match state with that initial lock already
    # released — matching real state, not a happy accident.
    runner._session.unlock_pov()
    runner._player_names = list(names or ["SteffKnight", "Hellcrying", "Guts"])
    runner._player_alive = [True] * len(runner._player_names)
    runner._deck_layout = list(deck_layout if deck_layout is not None else ["give_wood"])
    runner._last_confirmed_points = 10  # plenty — give_wood costs 1
    return runner


def make_schedule(*trigger_seconds):
    return [
        CardEvent(name=f"card{i}", card_type="electromania", trigger_seconds=t,
                  play_time_seconds=t, deck_position=None, drop_target=(960, 540))
        for i, t in enumerate(trigger_seconds)
    ]


# ---- _maybe_queue_first_blood_reward ---------------------------------------

def test_queues_the_resolved_killer_slot():
    runner = make_runner()
    runner._maybe_queue_first_blood_reward("1")
    assert runner._first_blood_reward_slot == 1
    assert runner._first_blood_reward_resolved is False


def test_advanced_cards_defaults_to_enabled():
    assert make_runner()._advanced_cards_enabled is True


def test_advanced_cards_off_disables_the_reward_entirely():
    runner = make_runner(config={"advanced_cards": False})
    runner._maybe_queue_first_blood_reward("1")
    assert runner._first_blood_reward_slot is None


def test_ignores_an_unresolved_killer():
    runner = make_runner()
    runner._maybe_queue_first_blood_reward(None)
    assert runner._first_blood_reward_slot is None


def test_does_not_overwrite_an_already_queued_reward():
    runner = make_runner()
    runner._maybe_queue_first_blood_reward("1")
    runner._maybe_queue_first_blood_reward("2")
    assert runner._first_blood_reward_slot == 1


def test_does_not_queue_once_already_resolved():
    runner = make_runner()
    runner._first_blood_reward_resolved = True
    runner._maybe_queue_first_blood_reward("1")
    assert runner._first_blood_reward_slot is None


# ---- _maybe_fire_first_blood_reward -----------------------------------------

def test_noop_when_nothing_is_queued():
    runner = make_runner()
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=make_schedule(200))
    give.assert_not_called()


def test_cancels_if_the_target_died_before_the_reward_could_be_given():
    runner = make_runner()
    runner._first_blood_reward_slot = 1
    runner._player_alive[1] = False
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=make_schedule(200))
    give.assert_not_called()
    assert runner._first_blood_reward_resolved is True


def test_waits_when_a_scheduled_card_is_due_soon():
    """Never start the reward's own drag within the buffer of a real card."""
    runner = make_runner()
    runner._first_blood_reward_slot = 1
    schedule = make_schedule(103)  # 3s away — inside the 5s buffer
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=schedule)
    give.assert_not_called()
    assert runner._first_blood_reward_resolved is False  # still queued, try again later


def test_fires_once_a_scheduled_card_is_far_enough_away():
    runner = make_runner()
    runner._first_blood_reward_slot = 1
    schedule = make_schedule(110)  # 10s away — outside the 5s buffer
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=schedule)
    give.assert_called_once_with(1, 0)  # slot=1, deck_pos=0 (only card in the fake deck)
    assert runner._first_blood_reward_resolved is True


def test_deck_pos_reflects_cards_played_while_the_reward_sat_queued():
    """If a real scheduled card plays (shrinking the tray) during the polls
    where give_wood's reward was still waiting on points/timing, the deck
    position it finally fires with must reflect the CURRENT tray, not
    whatever it would have been at queue time — deck_pos is looked up fresh
    on the call that actually fires (see _maybe_fire_first_blood_reward),
    never cached earlier."""
    runner = make_runner(deck_layout=["electromania", "give_wood", "beach_party"])
    runner._first_blood_reward_slot = 1

    # First check: a scheduled card is imminent, so the reward just waits.
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=95.0, card_schedule=make_schedule(98))
    give.assert_not_called()

    # Between that check and the next one, electromania (deck position 0,
    # to give_wood's left in the tray) actually gets played for real.
    runner._deck_played.add(0)

    # Second check: clear to fire. deck_pos must be re-resolved now, with
    # position 0 already gone from the tray — still 1 (give_wood's own
    # logical deck position never changes; what changes is how many cards
    # to its left remain, which _deck_pos_to_screen — not this method —
    # accounts for when it converts deck_pos to an actual screen x).
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=make_schedule(200))
    give.assert_called_once_with(1, 1)


def test_deck_pos_to_screen_shifts_correctly_once_a_card_to_its_left_is_played():
    """Confirms the actual screen-coordinate math (not just which logical
    deck_pos is picked) accounts for a card already played to give_wood's
    left in the tray — this is what _play_tray_card calls internally right
    before dragging, so it's what actually determines where the card gets
    dropped from."""
    runner = make_runner(deck_layout=["electromania", "give_wood"])
    before = runner._deck_pos_to_screen(1)  # both cards still in the tray
    runner._deck_played.add(0)  # electromania played and removed
    after = runner._deck_pos_to_screen(1)  # give_wood is now the only card left
    assert after != before  # tray recentered around the one remaining card


def test_fires_when_no_scheduled_cards_remain():
    runner = make_runner()
    runner._first_blood_reward_slot = 1
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=[])
    give.assert_called_once()


def test_waits_for_enough_points():
    runner = make_runner()
    runner._first_blood_reward_slot = 1
    runner._last_confirmed_points = 0  # give_wood costs 1
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=[])
    give.assert_not_called()
    assert runner._first_blood_reward_resolved is False


def test_cancels_when_the_deck_has_no_give_wood_card():
    runner = make_runner(deck_layout=["electromania", "beach_party"])
    runner._first_blood_reward_slot = 1
    with patch.object(runner, "_give_first_blood_reward") as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=[])
    give.assert_not_called()
    assert runner._first_blood_reward_resolved is True


def test_claims_the_reward_before_calling_give_so_it_cannot_double_fire():
    runner = make_runner()
    runner._first_blood_reward_slot = 1

    def check_resolved_flag_during_call(*a, **k):
        assert runner._first_blood_reward_resolved is True

    with patch.object(runner, "_give_first_blood_reward", side_effect=check_resolved_flag_during_call) as give:
        runner._maybe_fire_first_blood_reward(elapsed=100.0, card_schedule=[])
    give.assert_called_once()


# ---- _give_first_blood_reward -----------------------------------------------

def test_give_locks_pov_only_for_the_duration_of_this_action():
    """POV must be unlocked before the call (nothing else should have left
    it locked), actively locked WHILE the POV-switch + card play are
    happening (the actual risky window — a viewer's own /pov during this
    exact moment is what corrupts the reward), and unlocked again right
    after — not locked for the rest of the match."""
    runner = make_runner()
    assert runner._session.is_pov_locked() is False  # nothing else locked it beforehand

    locked_during_press = None
    locked_during_play = None

    def check_locked_during_press(*a, **k):
        nonlocal locked_during_press
        locked_during_press = runner._session.is_pov_locked()

    def check_locked_during_play(*a, **k):
        nonlocal locked_during_play
        locked_during_play = runner._session.is_pov_locked()

    with patch.object(runner, "_press", side_effect=check_locked_during_press), \
         patch.object(runner, "_play_tray_card", side_effect=check_locked_during_play):
        runner._give_first_blood_reward(killer_index=1, deck_pos=0)

    assert locked_during_press is True
    assert locked_during_play is True
    assert runner._session.is_pov_locked() is False  # unlocked again once the action is done


def test_give_locks_pov_switches_camera_and_plays_the_card_at_center():
    ds = FakeDraftLifecycle()
    runner = make_runner(ds=ds)
    with patch.object(runner, "_press") as press, \
         patch.object(runner, "_play_tray_card") as play:
        runner._give_first_blood_reward(killer_index=1, deck_pos=0)
    press.assert_called_once_with("2")  # slot_number_for_index(1) == "2"
    play.assert_called_once()
    event, target, *_ = play.call_args[0]
    assert target == (960, 540)
    assert event.card_type == "give_wood"
    assert event.deck_position == 0
    assert runner._session.is_pov_locked() is False  # unlocked again afterward


def test_give_emits_a_card_play_event_like_any_other_card():
    """_fire_card_event() normally emits this — bypassed here (a synthetic
    reward, not a scheduled card), so _give_first_blood_reward() must emit
    it itself, or this reward would be invisible on the ladder's live feed."""
    ds = FakeDraftLifecycle()
    runner = make_runner(ds=ds)
    with patch.object(runner, "_press"), patch.object(runner, "_play_tray_card"):
        runner._give_first_blood_reward(killer_index=1, deck_pos=0)
    assert ("card_play", {"card": "give_wood", "name": "First Blood Reward (Give wood)", "elapsed_ms": 0}) in ds.events


def test_give_unlocks_pov_even_if_the_card_play_raises():
    runner = make_runner()
    with patch.object(runner, "_press"), \
         patch.object(runner, "_play_tray_card", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            runner._give_first_blood_reward(killer_index=1, deck_pos=0)
    assert runner._session.is_pov_locked() is False


def test_give_aborts_without_playing_if_force_stopped_during_the_settle_wait():
    """A force-stop landing during the POV-switch settle wait (see
    _POV_SWITCH_SETTLE_SECONDS) must skip the card drag entirely rather than
    dragging into a match that's being torn down — and still release the
    POV lock via the finally block."""
    runner = make_runner()
    runner._stop.set()  # Event.wait() returns immediately (True) once set
    with patch.object(runner, "_press"), \
         patch.object(runner, "_play_tray_card") as play:
        runner._give_first_blood_reward(killer_index=1, deck_pos=0)
    play.assert_not_called()
    assert runner._session.is_pov_locked() is False

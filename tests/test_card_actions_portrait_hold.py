"""card_actions.play_card()'s hold_at_target_seconds (2026-09-15) -- see
game/match_runner.py's _PORTRAIT_DROP_HOLD_SECONDS for the live miss that
motivated this: a give_wood reward's drop onto a player's portrait missed,
happening "too fast to tell where it landed." pyautogui.dragTo() releases
the mouse button the instant its move animation ends, with no dwell time at
all for the game to register where the cursor actually is -- the same class
of problem the hover_click convention elsewhere in this codebase exists to
avoid for clicks. hold_at_target_seconds splits the drag into an explicit
mouseDown -> moveTo -> sleep -> mouseUp instead, so the button stays down at
the destination for a moment before releasing.
"""
from unittest.mock import patch

from game import card_actions


def test_default_hold_uses_plain_dragto():
    """hold_at_target_seconds=0.0 (the default) must be a no-op change --
    every existing caller (zone/fixed drop targets) keeps using dragTo() with
    no behavior change."""
    with patch.object(card_actions.pyautogui, "dragTo") as drag_to, \
         patch.object(card_actions.pyautogui, "mouseDown") as mouse_down, \
         patch.object(card_actions, "focus_darwin_window"), \
         patch.object(card_actions, "shift_down"), \
         patch.object(card_actions, "shift_up"), \
         patch.object(card_actions.time, "sleep"):
        card_actions.play_card((100, 100), (200, 200), "test_card")
    drag_to.assert_called_once()
    mouse_down.assert_not_called()


def test_hold_at_target_holds_the_button_down_before_releasing():
    with patch.object(card_actions.pyautogui, "dragTo") as drag_to, \
         patch.object(card_actions.pyautogui, "mouseDown") as mouse_down, \
         patch.object(card_actions.pyautogui, "mouseUp") as mouse_up, \
         patch.object(card_actions.pyautogui, "moveTo") as move_to, \
         patch.object(card_actions, "focus_darwin_window"), \
         patch.object(card_actions, "shift_down"), \
         patch.object(card_actions, "shift_up"), \
         patch.object(card_actions.time, "sleep") as sleep:
        card_actions.play_card((100, 100), (200, 200), "test_card", hold_at_target_seconds=0.15)
    drag_to.assert_not_called()
    mouse_down.assert_called_once()
    mouse_up.assert_called_once()
    move_to.assert_any_call(200, 200, duration=card_actions.DRAG_DURATION)
    sleep.assert_any_call(0.15)


def test_hold_releases_after_moving_and_sleeping_in_the_right_order():
    """The button must go down before the move, and up only after the hold
    -- not, say, released as part of the move itself."""
    calls = []
    with patch.object(card_actions.pyautogui, "dragTo"), \
         patch.object(card_actions.pyautogui, "mouseDown", side_effect=lambda: calls.append("down")), \
         patch.object(card_actions.pyautogui, "mouseUp", side_effect=lambda: calls.append("up")), \
         patch.object(card_actions.pyautogui, "moveTo",
                      side_effect=lambda *a, **k: calls.append(("move", a))), \
         patch.object(card_actions, "focus_darwin_window"), \
         patch.object(card_actions, "shift_down"), \
         patch.object(card_actions, "shift_up"), \
         patch.object(card_actions.time, "sleep", side_effect=lambda s: calls.append(("sleep", s))):
        card_actions.play_card((100, 100), (200, 200), "test_card", hold_at_target_seconds=0.15)
    # From the button going down, the very next three actions must be: move
    # to the target, sleep (the hold), then release -- in that order. (A
    # trailing time.sleep(0.3) after shift_up() follows "up" too, which is
    # fine -- only the relative order up to the release matters here.)
    down_index = calls.index("down")
    assert calls[down_index + 1 : down_index + 4] == [("move", (200, 200)), ("sleep", 0.15), "up"]


def test_bypass_mode_is_unaffected_by_the_new_parameter():
    with patch("builtins.input", return_value=""):
        result = card_actions.play_card((100, 100), (200, 200), "test_card",
                                         bypass_mode=True, hold_at_target_seconds=0.15)
    assert result is True

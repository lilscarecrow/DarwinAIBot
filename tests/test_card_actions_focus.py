"""focus_darwin_window() (game/card_actions.py) — no direct test coverage
existed before 2026-09-20. Added alongside a live-found fix: SetForegroundWindow
started raising a pywintypes error (winerror 0, "No error message is
available") at match end even with AttachThreadInput already applied, causing
the post-match MAIN MENU click to land nowhere (the OS cursor moved there, but
the click itself went to whichever window was actually focused) and the bot to
time out waiting for the main menu.

Fix: a synthetic Alt key tap (a standard workaround for this exact Windows
foreground-lock quirk) before a single retry of SetForegroundWindow, tried
only when the first attempt actually raises.
"""
from unittest.mock import patch

from game import card_actions


def _patch_common(hwnd=123, is_iconic=False):
    return [
        patch.object(card_actions, "_get_darwin_hwnd", return_value=hwnd),
        patch.object(card_actions.win32gui, "IsIconic", return_value=is_iconic),
        patch.object(card_actions.win32gui, "ShowWindow"),
        patch.object(card_actions._user32, "GetWindowThreadProcessId", return_value=999),
        patch.object(card_actions._kernel32, "GetCurrentThreadId", return_value=111),
        patch.object(card_actions._user32, "AttachThreadInput", return_value=True),
        patch.object(card_actions._user32, "BringWindowToTop"),
        patch.object(card_actions.time, "sleep"),
    ]


def _apply(patches):
    started = [p.start() for p in patches]
    return started


def _stop(patches):
    for p in patches:
        p.stop()


def test_returns_false_immediately_when_hwnd_is_not_found():
    with patch.object(card_actions, "_get_darwin_hwnd", return_value=None), \
         patch.object(card_actions.win32gui, "SetForegroundWindow") as sfw:
        assert card_actions.focus_darwin_window() is False
    sfw.assert_not_called()


def test_succeeds_on_the_first_attempt_with_no_alt_nudge():
    patches = _patch_common()
    with patch.object(card_actions.win32gui, "SetForegroundWindow") as sfw, \
         patch.object(card_actions.win32api, "keybd_event") as keybd:
        _apply(patches)
        try:
            assert card_actions.focus_darwin_window() is True
        finally:
            _stop(patches)
    sfw.assert_called_once()
    keybd.assert_not_called()


def test_nudges_with_alt_and_retries_once_after_the_first_failure():
    patches = _patch_common()
    with patch.object(
        card_actions.win32gui, "SetForegroundWindow",
        side_effect=[Exception("(0, 'SetForegroundWindow', 'No error message is available')"), None],
    ) as sfw, patch.object(card_actions.win32api, "keybd_event") as keybd:
        _apply(patches)
        try:
            assert card_actions.focus_darwin_window() is True
        finally:
            _stop(patches)
    assert sfw.call_count == 2
    # Alt down then Alt up, in that order, between the two SetForegroundWindow calls.
    assert keybd.call_count == 2
    assert keybd.call_args_list[0][0][0] == card_actions.win32con.VK_MENU
    assert keybd.call_args_list[1][0][2] == card_actions.win32con.KEYEVENTF_KEYUP


def test_returns_false_when_both_attempts_fail():
    patches = _patch_common()
    with patch.object(
        card_actions.win32gui, "SetForegroundWindow",
        side_effect=Exception("still failing"),
    ), patch.object(card_actions.win32api, "keybd_event"):
        _apply(patches)
        try:
            assert card_actions.focus_darwin_window() is False
        finally:
            _stop(patches)

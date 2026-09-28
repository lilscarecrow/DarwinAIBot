"""Crash reporter auto-close (2026-09-28) — found live: the game process can
crash mid-match, popping up Unreal's own CrashReportClient.exe dialog with no
real Win32 button controls to click (Slate UI draws its own widgets). Closing
the process itself dismisses the dialog just as well as clicking "Close
Without Sending" would, mirroring close_game()'s existing pattern exactly.
"""
from unittest.mock import MagicMock, patch

from game.launcher import (
    CRASH_REPORTER_PROCESS_NAME,
    is_crash_reporter_open,
    close_crash_reporter,
)


def _proc(name, pid=1234):
    p = MagicMock()
    p.info = {"name": name}
    p.pid = pid
    return p


def test_is_crash_reporter_open_true_when_process_present():
    with patch("game.launcher.psutil.process_iter", return_value=[_proc(CRASH_REPORTER_PROCESS_NAME)]):
        assert is_crash_reporter_open() is True


def test_is_crash_reporter_open_false_when_absent():
    with patch("game.launcher.psutil.process_iter", return_value=[_proc("Darwin-Win64-Shipping.exe")]):
        assert is_crash_reporter_open() is False


def test_is_crash_reporter_open_case_insensitive():
    with patch("game.launcher.psutil.process_iter", return_value=[_proc(CRASH_REPORTER_PROCESS_NAME.lower())]):
        assert is_crash_reporter_open() is True


def test_close_crash_reporter_terminates_matching_process():
    proc = _proc(CRASH_REPORTER_PROCESS_NAME, pid=999)
    other = _proc("Darwin-Win64-Shipping.exe")
    with patch("game.launcher.psutil.process_iter", return_value=[other, proc]):
        close_crash_reporter()
    proc.terminate.assert_called_once()
    other.terminate.assert_not_called()


def test_close_crash_reporter_noop_when_not_found():
    with patch("game.launcher.psutil.process_iter", return_value=[_proc("Darwin-Win64-Shipping.exe")]):
        close_crash_reporter()  # must not raise

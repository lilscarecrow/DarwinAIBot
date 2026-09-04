"""
Restores and tracks this process's console window position/size across restarts,
so start_bot.bat's window reopens wherever it was last left instead of Windows'
default spawn position.

Only meaningful for a classic conhost-hosted console (cmd.exe/PowerShell, which is
what start_bot.bat launches) — GetConsoleWindow() returns None or a non-visible
backing handle in some other hosts (e.g. Windows Terminal's pseudo-console), so
this quietly no-ops there rather than doing anything wrong.

Position is saved continuously via a background poller rather than on shutdown —
same lesson as the Twitch OAuth token-save fix in bot/twitch_bot.py (see CLAUDE.md):
a console window can be closed in ways that never let a clean-shutdown hook run
(the X button, taskkill, a crash, Windows update forcing a reboot), so relying on
"save on close" would lose the position most of the time. Polling and only writing
when the rect actually changed sidesteps that entirely.
"""
import ctypes
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_PATH = Path("window_pos.json")
_POLL_INTERVAL_SECONDS = 3

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _get_console_hwnd():
    hwnd = _kernel32.GetConsoleWindow()
    return hwnd if hwnd else None


def _current_rect(hwnd):
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def restore_position() -> None:
    """Best-effort: move/resize the console window to its last saved position.
    No-ops if there's no console window or no saved position yet."""
    hwnd = _get_console_hwnd()
    if not hwnd:
        return
    try:
        if not _STATE_PATH.exists():
            return
        with _STATE_PATH.open("r", encoding="utf-8") as f:
            pos = json.load(f)
        x, y, w, h = pos["x"], pos["y"], pos["w"], pos["h"]
        _user32.MoveWindow(hwnd, x, y, w, h, True)
        logger.info("Console window restored to (%d, %d) %dx%d", x, y, w, h)
    except Exception as e:
        logger.warning("Could not restore console window position: %s", e)


def _poll_loop(hwnd) -> None:
    last = None
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            current = _current_rect(hwnd)
            if current is None or current == last:
                continue
            x, y, w, h = current
            with _STATE_PATH.open("w", encoding="utf-8") as f:
                json.dump({"x": x, "y": y, "w": w, "h": h}, f)
            last = current
        except Exception:
            logger.debug("Console window position poll failed", exc_info=True)


def start_position_tracker() -> None:
    """Best-effort: start a daemon thread that saves the console window's
    position/size whenever it changes, so restore_position() can put it back on
    the next launch. No-ops if there's no console window to track."""
    hwnd = _get_console_hwnd()
    if not hwnd:
        return
    threading.Thread(target=_poll_loop, args=(hwnd,), daemon=True).start()

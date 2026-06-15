import ctypes
import logging
import time

import pyautogui
import win32api
import win32con
import win32gui

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

from game.screen_detection import take_screenshot, find_template, save_error_screenshot

logger = logging.getLogger(__name__)

DRAG_DURATION = 0.5

# SHIFT key constants for WM_KEYDOWN / WM_KEYUP
_VK_SHIFT = 0x10
_SCAN_SHIFT = 0x2A
_LPARAM_KEYDOWN = (_SCAN_SHIFT << 16) | 1
_LPARAM_KEYUP   = (_SCAN_SHIFT << 16) | 0xC0000001

_darwin_hwnd: int | None = None

# VK and scan codes for keys sent via PostMessage
# Single letters derive VK from ord(); special keys need explicit entries
_SCAN_CODES: dict[str, int] = {
    "b":      0x30,
    "g":      0x22,
    "escape": 0x01,
    "shift":  0x2A,
}
_VK_CODES: dict[str, int] = {
    "escape": 0x1B,
    "shift":  0x10,
}


def _get_darwin_hwnd() -> int | None:
    """Find and cache the Darwin Project main window handle."""
    global _darwin_hwnd
    if _darwin_hwnd and win32gui.IsWindow(_darwin_hwnd):
        return _darwin_hwnd
    results = []
    def cb(hwnd, _):
        try:
            if win32gui.GetWindowText(hwnd).strip() == "Darwin":
                results.append(hwnd)
        except Exception:
            pass
    win32gui.EnumWindows(cb, None)
    _darwin_hwnd = results[0] if results else None
    if _darwin_hwnd:
        logger.debug("Darwin hwnd cached: %d", _darwin_hwnd)
    else:
        logger.warning("Darwin window not found — key events will use fallback")
    return _darwin_hwnd


def focus_darwin_window() -> bool:
    """Bring the Darwin Project window to the foreground. Returns True on success."""
    hwnd = _get_darwin_hwnd()
    if not hwnd:
        logger.warning("Cannot focus Darwin window — hwnd not found")
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.1)

        # AttachThreadInput temporarily shares input queues between our thread and
        # Darwin's, which bypasses Windows' focus-stealing prevention for SetForegroundWindow.
        darwin_thread = _user32.GetWindowThreadProcessId(hwnd, None)
        cur_thread = _kernel32.GetCurrentThreadId()
        attached = darwin_thread != cur_thread and bool(
            _user32.AttachThreadInput(cur_thread, darwin_thread, True)
        )

        _user32.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.15)

        if attached:
            _user32.AttachThreadInput(cur_thread, darwin_thread, False)

        logger.debug("Darwin window focused")
        return True
    except Exception as e:
        logger.warning("focus_darwin_window failed: %s", e)
        return False


def shift_down():
    # Two-part hold: SendInput sets GetAsyncKeyState (per-frame hold check),
    # PostMessage WM_KEYDOWN triggers the Slate UI tray to open.
    # Both are required — neither works alone.
    hwnd = _get_darwin_hwnd()
    pyautogui.keyDown("shift")
    if hwnd:
        time.sleep(0.05)
        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, _VK_SHIFT, _LPARAM_KEYDOWN)


def shift_up():
    hwnd = _get_darwin_hwnd()
    if hwnd:
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, _VK_SHIFT, _LPARAM_KEYUP)
    pyautogui.keyUp("shift")


def grab_card(slot_coordinate: tuple[int, int], shift_already_held: bool = False) -> bool:
    """
    Shift+moveTo+mouseDown on a card slot — reveals the big zone map.
    Caller must follow up with complete_drag() to play or release_card() to cancel.
    Pass shift_already_held=True if the caller already called shift_down() (e.g. to hold
    shift for a before-screenshot), to avoid a redundant second shift_down.
    """
    sx, sy = slot_coordinate
    focus_darwin_window()
    if not shift_already_held:
        shift_down()
        time.sleep(0.15)
    pyautogui.moveTo(sx, sy, duration=0.2)
    time.sleep(0.1)
    pyautogui.mouseDown()
    return True


def release_card():
    """Release a grabbed card without dragging — cancels the play and returns it to its slot."""
    pyautogui.mouseUp()
    shift_up()
    time.sleep(0.2)


def complete_drag(target_coordinate: tuple[int, int], card_name: str, keep_shift: bool = False) -> bool:
    """Drag an already-grabbed card to target and release. Must be called after grab_card().

    keep_shift: if True, skip shift_up() so the caller keeps shift held for the
    after-verification screenshot and any subsequent retries.
    """
    tx, ty = target_coordinate
    pyautogui.moveTo(tx, ty, duration=DRAG_DURATION)
    pyautogui.mouseUp()
    if not keep_shift:
        shift_up()
    time.sleep(0.3)
    logger.info("Card '%s' dragged to (%d, %d)", card_name, tx, ty)
    return True


def play_card(
    slot_coordinate: tuple[int, int],
    target_coordinate: tuple[int, int],
    card_name: str,
    slot_template_path: str | None = None,
    bypass_mode: bool = False,
    keep_shift: bool = False,
) -> bool:
    """
    Shift-drag a card from slot_coordinate to target_coordinate.
    Uses WM_KEYDOWN/WM_KEYUP posted directly to the Darwin window for SHIFT,
    since the game uses Raw Input and ignores SendInput-based synthetic keys.
    Returns True on success.

    keep_shift: if True, skip the trailing shift_up() so the caller can hold shift
    continuously from before-screenshot through play through after-screenshot.
    """
    sx, sy = slot_coordinate
    tx, ty = target_coordinate

    if bypass_mode:
        logger.info("[BYPASS] Would play card '%s': drag (%d,%d) -> (%d,%d)", card_name, sx, sy, tx, ty)
        input("Press Enter to continue (bypass mode)...")
        return True

    logger.info("Playing card '%s': drag (%d,%d) -> (%d,%d)", card_name, sx, sy, tx, ty)
    focus_darwin_window()

    shift_down()
    time.sleep(0.15)  # let tray appear before moving to card
    pyautogui.moveTo(sx, sy, duration=0.2)
    time.sleep(0.1)
    pyautogui.dragTo(tx, ty, duration=DRAG_DURATION, button="left")
    if not keep_shift:
        shift_up()
    time.sleep(0.3)

    if slot_template_path:
        screenshot = take_screenshot()
        still_there = find_template(screenshot, slot_template_path)
        if still_there:
            logger.warning("Card '%s' still in slot after drag — play may have failed", card_name)
            save_error_screenshot(f"card_drag_failed_{card_name}")
            return False

    logger.info("Card '%s' played successfully", card_name)
    return True


def click(x: int, y: int, bypass_mode: bool = False):
    if bypass_mode:
        logger.info("[BYPASS] Would click (%d, %d)", x, y)
        input("Press Enter to continue (bypass mode)...")
        return
    pyautogui.click(x, y)


def press_key(key: str, bypass_mode: bool = False):
    if bypass_mode:
        logger.info("[BYPASS] Would press key '%s'", key)
        input("Press Enter to continue (bypass mode)...")
        return

    hwnd = _get_darwin_hwnd()
    key_lower = key.lower()
    scan = _SCAN_CODES.get(key_lower)
    if hwnd and scan is not None:
        # PostMessage directly to Darwin so focus doesn't matter
        vk = _VK_CODES.get(key_lower) or ord(key.upper())
        lparam_down = (scan << 16) | 1
        lparam_up   = (scan << 16) | 0xC0000001
        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, lparam_down)
        time.sleep(0.05)
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, vk, lparam_up)
        logger.info("Key '%s' sent via PostMessage to Darwin hwnd", key)
    else:
        # Fallback: focus window first, then send via pyautogui
        focus_darwin_window()
        pyautogui.press(key)
        logger.info("Key '%s' sent via pyautogui (focus fallback)", key)


def copy_clipboard() -> str:
    import pyperclip
    return pyperclip.paste()

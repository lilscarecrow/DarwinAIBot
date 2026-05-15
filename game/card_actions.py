import logging
import time

import pyautogui

from game.screen_detection import take_screenshot, find_template, save_error_screenshot

logger = logging.getLogger(__name__)

# Seconds to hold shift before dragging
DRAG_DURATION = 0.5


def play_card(
    slot_coordinate: tuple[int, int],
    target_coordinate: tuple[int, int],
    card_name: str,
    slot_template_path: str | None = None,
    bypass_mode: bool = False,
) -> bool:
    """
    Shift-drag a card from slot_coordinate to target_coordinate.
    Verifies the card left its slot after the drag.
    Returns True on success.
    """
    sx, sy = slot_coordinate
    tx, ty = target_coordinate

    if bypass_mode:
        logger.info("[BYPASS] Would play card '%s': drag (%d,%d) -> (%d,%d)", card_name, sx, sy, tx, ty)
        input("Press Enter to continue (bypass mode)...")
        return True

    logger.info("Playing card '%s': drag (%d,%d) -> (%d,%d)", card_name, sx, sy, tx, ty)

    pyautogui.moveTo(sx, sy, duration=0.2)
    pyautogui.keyDown("shift")
    time.sleep(0.1)
    pyautogui.dragTo(tx, ty, duration=DRAG_DURATION, button="left")
    pyautogui.keyUp("shift")
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
    pyautogui.press(key)


def copy_clipboard() -> str:
    import pyperclip
    return pyperclip.paste()

import logging
import time
import datetime
from pathlib import Path

import cv2
import numpy as np
import pyautogui

# The bot is controlled via /end — the mouse-corner failsafe is not needed
# and will fire spuriously when the cursor happens to be at a screen edge.
pyautogui.FAILSAFE = False

logger = logging.getLogger(__name__)

SCREENSHOT_ERROR_DIR = Path("screenshots/errors")


def take_screenshot() -> np.ndarray:
    img = pyautogui.screenshot()
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def save_error_screenshot(label: str):
    SCREENSHOT_ERROR_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = SCREENSHOT_ERROR_DIR / f"{ts}_{label}.png"
    img = take_screenshot()
    cv2.imwrite(str(path), img)
    logger.info("Error screenshot saved: %s", path)


def find_template(screenshot: np.ndarray, template_path: str, threshold: float = 0.8) -> tuple[int, int] | None:
    """
    Search for a template image within a screenshot using normalized cross-correlation.
    Returns (x, y) center of best match, or None if below threshold.
    """
    template = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if template is None:
        logger.error("Template not found: %s", template_path)
        return None

    result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val < threshold:
        return None

    h, w = template.shape[:2]
    return (max_loc[0] + w // 2, max_loc[1] + h // 2)


def wait_for_template(
    template_path: str,
    timeout: int = 60,
    poll_interval: float = 2.0,
    threshold: float = 0.8,
) -> bool:
    """Poll screenshots until template appears or timeout expires."""
    return wait_for_template_center(template_path, timeout, poll_interval, threshold) is not None


def wait_for_template_center(
    template_path: str,
    timeout: int = 60,
    poll_interval: float = 2.0,
    threshold: float = 0.8,
) -> tuple[int, int] | None:
    """Poll screenshots until template appears. Returns (x, y) center on match, None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screenshot = take_screenshot()
        match = find_template(screenshot, template_path, threshold)
        if match:
            logger.info("Template detected: %s", template_path)
            return match
        time.sleep(poll_interval)
    logger.warning("Timeout waiting for template: %s", template_path)
    return None


def poll_for_match_end(
    badge_template_path: str,
    poll_interval: float = 12.0,
    threshold: float = 0.75,
) -> bool:
    """
    Poll every poll_interval seconds for the placement badge (match end indicator).
    Returns True when detected. Caller is responsible for stopping the loop externally.
    """
    screenshot = take_screenshot()
    match = find_template(screenshot, badge_template_path, threshold)
    if match:
        logger.info("Match end detected via placement badge")
        return True
    return False


def sample_pixel_color(x: int, y: int) -> tuple[int, int, int]:
    """Sample a single pixel from the screen. Returns (R, G, B)."""
    screenshot = take_screenshot()
    bgr = screenshot[y, x]
    return (int(bgr[2]), int(bgr[1]), int(bgr[0]))


def color_within_threshold(
    color: tuple[int, int, int],
    target: tuple[int, int, int],
    tolerance: int = 20,
) -> bool:
    return all(abs(color[i] - target[i]) <= tolerance for i in range(3))


# Ordered from most-specific to least-specific so the first match wins.
# Each entry: (screen_name, template_path)
_SCREEN_SIGNATURES: list[tuple[str, str]] = [
    ("director_lobby",  "templates/lobby_password_label.png"),
    ("director_splash", "templates/latest_updates_continue.png"),
    ("choose_role",     "templates/choose_role_screen.png"),
    ("create_match",    "templates/solo_classic_label.png"),
    ("custom_browser",  "templates/create_custom_match.png"),
    ("region_popup",    "templates/region_popup_header.png"),
    ("play_screen",     "templates/play_screen_region.png"),
    ("main_menu",       "templates/play_button.png"),
]


def detect_current_screen(threshold: float = 0.8) -> str | None:
    """
    Single screenshot, checked against all known screen signatures in priority order.
    Returns the screen name of the first match, or None if no template matches.
    """
    screenshot = take_screenshot()
    for screen_name, template_path in _SCREEN_SIGNATURES:
        if find_template(screenshot, template_path, threshold):
            logger.info("Screen detected: %s", screen_name)
            return screen_name
    logger.info("Screen detection: no known screen matched")
    return None

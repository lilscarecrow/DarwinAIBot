import logging
import time
import datetime
from pathlib import Path

import cv2
import numpy as np
import pyautogui

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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screenshot = take_screenshot()
        if find_template(screenshot, template_path, threshold):
            logger.info("Template detected: %s", template_path)
            return True
        time.sleep(poll_interval)
    logger.warning("Timeout waiting for template: %s", template_path)
    return False


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

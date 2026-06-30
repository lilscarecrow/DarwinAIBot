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
    threshold: float = 0.88,
) -> bool:
    """
    Single-shot check for the placement badge (match end indicator).
    Returns True when detected. Caller is responsible for the polling loop.
    Saves a debug screenshot on detection so false positives can be diagnosed.
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


def detect_player_slot_xs(screenshot: np.ndarray, config: dict) -> list[int]:
    """
    Return center x-coordinates of real player card slots in the player bar.
    Always dynamic — counts slots from what is visible on screen.
    Excludes the leftmost slot (glitch card, always present).

    Detects card boundaries by finding near-black vertical separator lines.
    Projects brightness across several rows at the top of the bar rather than
    a single row: separator lines are dark at every height, card content varies,
    so the per-column minimum across rows reliably isolates separators.
    """
    bar = config.get("player_bar_region")  # [x0, y0, x1, y1]
    if not bar:
        return []
    x0, y0, x1, y1 = int(bar[0]), int(bar[1]), int(bar[2]), int(bar[3])

    bar_h = y1 - y0
    # Sample three rows spread across the top quarter of the bar (above portraits).
    # Using bar_h // 8, bar_h // 5, bar_h // 3 gives three independent samples
    # while staying above the portrait image area.
    offsets = [bar_h // 8, bar_h // 5, bar_h // 3]
    sample_ys = [y0 + off for off in offsets if y0 + off < screenshot.shape[0]]
    if not sample_ys:
        return []

    # Per-column brightness across all sampled rows, then take the minimum.
    # A separator column is dark at every sampled height; card content is not.
    profiles = [
        np.max(screenshot[r, x0:x1], axis=1).astype(np.float32)
        for r in sample_ys
    ]
    combined = np.min(np.stack(profiles, axis=0), axis=0)

    sep_threshold = int(config.get("player_separator_threshold", 25))
    is_sep = combined < sep_threshold

    sep_centers: list[int] = []
    in_sep, sep_start = False, 0
    for i, s in enumerate(is_sep):
        if s and not in_sep:
            in_sep, sep_start = True, i
        elif not s and in_sep:
            sep_centers.append(x0 + (sep_start + i) // 2)
            in_sep = False
    if in_sep:
        sep_centers.append(x0 + (sep_start + len(is_sep)) // 2)

    n_slots = len(sep_centers) + 1
    if not (4 <= n_slots <= 12):
        logger.warning(
            "Player bar: detected %d separators → %d slots (expected 8–11). "
            "Check player_bar_region bounds and player_separator_threshold.",
            len(sep_centers), n_slots,
        )
        return []

    logger.info("Player bar: %d slots detected (%d players + 1 glitch)", n_slots, n_slots - 1)
    boundaries = [0] + [s - x0 for s in sep_centers] + [x1 - x0]
    all_xs = [x0 + (boundaries[i] + boundaries[i + 1]) // 2 for i in range(n_slots)]
    return all_xs[1:]  # drop the leftmost slot (glitch)


def sample_player_alive(screenshot: np.ndarray, slot_x: int, config: dict) -> bool:
    """
    Return True if the player in this slot appears alive.
    Alive portraits are colorful (high HSV saturation); eliminated portraits are greyscale.
    """
    bar = config.get("player_bar_region")
    if not bar:
        return True
    y0 = int(bar[1])
    portrait_y_off = int(config.get("player_portrait_y_in_bar", 35))
    sat_threshold = int(config.get("player_saturation_threshold", 40))

    sample_y = y0 + portrait_y_off
    if 0 <= sample_y < screenshot.shape[0] and 0 <= slot_x < screenshot.shape[1]:
        bgr = screenshot[sample_y, slot_x].reshape(1, 1, 3).astype(np.uint8)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[0, 0]
        return int(hsv[1]) > sat_threshold
    return True


def color_within_threshold(
    color: tuple[int, int, int],
    target: tuple[int, int, int],
    tolerance: int = 20,
) -> bool:
    return all(abs(color[i] - target[i]) <= tolerance for i in range(3))


# Ordered from most-specific to least-specific so the first match wins.
# Each entry: (screen_name, template_path)
_SCREEN_SIGNATURES: list[tuple[str, str]] = [
    ("director_lobby",      "templates/lobby_password_label.png"),
    ("lobby_open",          "templates/lobby_countdown_label.png"),
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

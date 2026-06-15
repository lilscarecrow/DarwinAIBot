import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract

if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

logger = logging.getLogger(__name__)

_OCR_TIMEOUT_SECONDS = 10
_ocr_executor = ThreadPoolExecutor(max_workers=1)


@dataclass
class PlayerResult:
    placement: int
    player_name: str
    damage_done: int
    kills: int

    def __str__(self):
        return f"#{self.placement} {self.player_name} — DMG: {self.damage_done} / Kills: {self.kills}"


def _crop(image: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    return image[y:y + h, x:x + w]


def _ocr_region(image: np.ndarray, x: int, y: int, w: int, h: int, config: str = "") -> str:
    region = _crop(image, x, y, w, h)
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    future = _ocr_executor.submit(pytesseract.image_to_string, gray, config=config)
    try:
        return future.result(timeout=_OCR_TIMEOUT_SECONDS).strip()
    except FuturesTimeoutError:
        logger.warning("OCR timed out on region (%d,%d,%d,%d)", x, y, w, h)
        return ""
    except Exception as e:
        logger.warning("OCR failed on region (%d,%d,%d,%d): %s", x, y, w, h, e)
        return ""


def parse_results_screen(
    screenshot: np.ndarray,
    regions: dict,
) -> list[PlayerResult]:
    """
    OCR the results table from the match end screenshot.

    regions is a dict with keys 'placement', 'player_name', 'damage_done', 'kills',
    each containing a list of (x, y, w, h) tuples — one per row.

    Returns a list of PlayerResult objects.
    """
    results = []
    row_count = len(regions.get("placement", []))

    for i in range(row_count):
        try:
            placement_text = _ocr_region(screenshot, *regions["placement"][i], config="--psm 7 digits")
            name_text = _ocr_region(screenshot, *regions["player_name"][i], config="--psm 7")
            damage_text = _ocr_region(screenshot, *regions["damage_done"][i], config="--psm 7 digits")
            kills_text = _ocr_region(screenshot, *regions["kills"][i], config="--psm 7 digits")

            placement = int("".join(filter(str.isdigit, placement_text)) or 0)
            damage = int("".join(filter(str.isdigit, damage_text)) or 0)
            kills = int("".join(filter(str.isdigit, kills_text)) or 0)

            results.append(PlayerResult(
                placement=placement,
                player_name=name_text,
                damage_done=damage,
                kills=kills,
            ))
        except Exception as e:
            logger.warning("Failed to parse results row %d: %s", i, e)

    results.sort(key=lambda r: r.placement)
    return results


def count_director_point_pips(screenshot: np.ndarray, pips_cfg: dict) -> int | None:
    """
    Count filled director point pips by sampling pixel colors.
    pips_cfg: {"x_start": int, "y": int, "spacing": int, "count": int}
    A pip is filled when its pixel is bright (max channel > 130).
    Returns the number of filled pips (0–10), or None if config is invalid.
    """
    try:
        x0 = int(pips_cfg["x_start"])
        y  = int(pips_cfg["y"])
        sp = int(pips_cfg["spacing"])
        n  = int(pips_cfg["count"])
    except (KeyError, TypeError, ValueError):
        return None

    filled = 0
    for i in range(n):
        x = x0 + i * sp
        if 0 <= y < screenshot.shape[0] and 0 <= x < screenshot.shape[1]:
            b, g, r = [int(v) for v in screenshot[y, x]]
            if max(b, g, r) > 130:
                filled += 1
    return filled


def read_director_points(screenshot: np.ndarray, region: tuple[int, int, int, int]) -> int | None:
    """
    OCR the director points numerator (e.g. '06' from '06/10').
    region: (x, y, w, h) — should cover only the two-digit numerator, not the '/10'.
    Returns the current point count (0-10), or None if OCR fails.
    """
    x, y, w, h = region
    crop = _crop(screenshot, x, y, w, h)
    # 4x upscale + Otsu auto-threshold (adapts to background brightness)
    scaled = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    text = _ocr_region_img(thresh, config="--psm 8 -c tessedit_char_whitelist=0123456789")
    try:
        digits = "".join(filter(str.isdigit, text))
        if digits:
            return int(digits)
    except ValueError:
        pass
    logger.warning("Could not parse director points from OCR text: %r", text)
    return None


def _ocr_region_img(image: np.ndarray, config: str = "") -> str:
    """Run OCR directly on a pre-processed image (already grayscale/thresholded)."""
    future = _ocr_executor.submit(pytesseract.image_to_string, image, config=config)
    try:
        return future.result(timeout=_OCR_TIMEOUT_SECONDS).strip()
    except FuturesTimeoutError:
        logger.warning("OCR timed out on image")
        return ""
    except Exception as e:
        logger.warning("OCR failed: %s", e)
        return ""


def read_lobby_countdown(screenshot: np.ndarray, debug: bool = False) -> int | None:
    """
    OCR the 'CUSTOM MATCH EXPIRES IN MM:SS' orange banner in the Director lobby.
    Region is at the top-left of the 1920×1080 screen.
    Returns remaining seconds, or None if the text cannot be parsed.
    Set debug=True to save intermediate images to calibration_screenshots/ for inspection.
    """
    import re
    # Banner position in 1920×1080 pixels.
    # computer-use screenshot (1456×816) shows banner at y≈278; scale: 278*(1080/816)≈368
    x, y, w, h = 85, 362, 310, 42
    crop = _crop(screenshot, x, y, w, h)
    scaled = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
    # White text on orange background — threshold retains white text, drops orange
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

    if debug:
        import datetime
        from pathlib import Path
        ts = datetime.datetime.now().strftime("%H-%M-%S")
        out = Path("calibration_screenshots")
        out.mkdir(exist_ok=True)
        cv2.imwrite(str(out / f"countdown_crop_{ts}.png"), crop)
        cv2.imwrite(str(out / f"countdown_thresh_{ts}.png"), thresh)
        logger.info("Lobby countdown debug images saved to calibration_screenshots/")

    text = _ocr_region_img(thresh, config="--psm 7")
    # Normalize common OCR confusions for this font (I→1, l→1, |→1, O→0)
    normalized = text.replace("I", "1").replace("l", "1").replace("|", "1").replace("O", "0")
    match = re.search(r'(\d+):(\d+)', normalized)
    if match:
        minutes, seconds = int(match.group(1)), int(match.group(2))
        total = minutes * 60 + seconds
        logger.info("Lobby countdown OCR: %r → %dm%02ds (%ds total)", text.strip(), minutes, seconds, total)
        return total
    logger.warning("Lobby countdown OCR: could not parse time from %r", text.strip())
    return None


def format_results_for_discord(results: list[PlayerResult]) -> str:
    if not results:
        return "No results parsed."
    lines = ["**Match Results**", "```"]
    lines.append(f"{'#':<4} {'Player':<20} {'Damage':>8} {'Kills':>6}")
    lines.append("-" * 42)
    for r in results:
        lines.append(f"{r.placement:<4} {r.player_name:<20} {r.damage_done:>8} {r.kills:>6}")
    lines.append("```")
    return "\n".join(lines)

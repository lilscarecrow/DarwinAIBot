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
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(pytesseract.image_to_string, gray, config=config)
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

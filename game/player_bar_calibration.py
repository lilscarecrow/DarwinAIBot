"""
player_bar_calibration.py — offline analysis of the in-match player bar.

The bot's player tracking (names, first blood, eliminations, and everything
the ladder's LIVE feed shows) runs on game.player_cards_v2's geometry
detector — no per-machine calibration needed. This module runs the SAME
functions the bot uses on a saved screenshot and reports what each stage
sees, so a bad frame (wrong resolution, card strip not on screen, tesseract
missing) can be diagnosed without a live match. Used by
calibrate_player_bar.py (any machine, any OS) and by the F8 hotkey in
calibrate.py (live). See docs/PLAYER_BAR_CALIBRATION.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ReportV2:
    cards: list
    names: list
    expected: Optional[int]
    ocr_available: bool = False
    problems: list = field(default_factory=list)


def analyze_v2(img: np.ndarray, config: Optional[dict] = None, expected: Optional[int] = None,
               ocr: bool = True) -> ReportV2:
    from game import player_cards_v2 as v2
    cards = v2.detect_cards(img, config, expected=expected)
    rep = ReportV2(cards=cards, names=[], expected=expected)
    if not cards:
        rep.problems.append(
            "v2 found no complete card row: no count 2-10 has every centred position reading as a card "
            "(bright name band = alive, red X = dead). Is the card strip on screen, at 1920x1080?"
        )
        return rep
    if expected and len(cards) != expected:
        rep.problems.append(f"v2 counted {len(cards)} cards but the lobby expected {expected}")
    if ocr:
        try:
            names = v2.ocr_names(img, cards, config)
            if len(names) == len(cards) and any(isinstance(nm, str) and nm.strip() for nm in names):
                rep.names = names
                rep.ocr_available = True
            elif len(names) == len(cards):
                rep.problems.append("OCR read no names from the bands (tesseract installed?)")
        except Exception as e:
            rep.problems.append(f"OCR unavailable here ({e.__class__.__name__}); names not checked.")
    return rep


def annotate_v2(img: np.ndarray, rep: ReportV2) -> np.ndarray:
    out = img.copy()
    for c in rep.cards:
        color = (0, 220, 0) if c.alive else (0, 0, 255)
        cv2.rectangle(out, (c.x - 62, 60), (c.x + 62, 152), color, 1)
        cv2.line(out, (c.x - 58, c.band_y), (c.x + 58, c.band_y), (255, 200, 0), 2)
        label = f"{c.index}:{'A' if c.alive else 'X'}" + (f" {rep.names[c.index]}" if rep.names and rep.names[c.index] else "")
        cv2.putText(out, label, (c.x - 58, 168), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    if not rep.cards:
        cv2.putText(out, "v2: no cards", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
    return out


def format_report_v2(rep: ReportV2) -> str:
    lines = [f"v2 geometry detector: {len(rep.cards)} cards, {sum(1 for c in rep.cards if c.alive)} alive"
             + (f" (lobby expected {rep.expected})" if rep.expected else "")]
    if rep.cards:
        lines.append("  slot    x  state  band_y  band_v  band_s  red_x  name")
        for c in rep.cards:
            nm = rep.names[c.index] if rep.names else ""
            lines.append(f"  {c.index:>4} {c.x:>5}  {'alive' if c.alive else 'DEAD ':5} {c.band_y:>6} {c.band_v:>7} {c.band_s:>7} {c.red_x:>6.2f}  {nm}")
        if not rep.ocr_available:
            lines.append("  (names not read — OCR unavailable on this machine)")
    if rep.problems:
        lines.append("PROBLEMS:")
        lines += [f"  - {p}" for p in rep.problems]
    else:
        lines.append("v2: no problems found")
    return "\n".join(lines)

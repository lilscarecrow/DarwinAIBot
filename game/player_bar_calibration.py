"""
player_bar_calibration.py — offline analysis of the in-match player bar.

The bot's player tracking (names, first blood, eliminations, and everything
the ladder's LIVE feed shows) starts from six config keys that describe where
the HUD card strip is and how to read it. This module runs the SAME functions
the bot uses (game.screen_detection / game.ocr) on a saved screenshot and
reports what each stage sees, so the keys can be dialled in without a live
match. Used by calibrate_player_bar.py (any machine, any OS) and by the F8
hotkey in calibrate.py (live). See docs/PLAYER_BAR_CALIBRATION.md.
"""
from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Key -> default the bot applies when the key is absent. player_bar_region has
# no default: without it detect_player_slot_xs returns [] and the bot logs
# "no slots detected — player tracking disabled".
PLAYER_BAR_DEFAULTS: dict = {
    "player_bar_region": None,
    "player_separator_threshold": 25,
    "player_portrait_y_in_bar": 35,
    "player_saturation_threshold": 40,
    "player_name_y_in_bar": 62,
    "player_name_h": 14,
}


def _detection():
    """game.screen_detection imports pyautogui at module level; a machine with
    no display (Linux, CI) has none, and this module never takes screenshots,
    so a stand-in keeps the import working there."""
    try:
        import pyautogui  # noqa: F401
    except Exception:
        sys.modules.setdefault("pyautogui", types.SimpleNamespace(screenshot=None))
    from game import screen_detection
    return screen_detection


def effective_config(config: Optional[dict], overrides: Optional[dict] = None) -> dict:
    """Defaults <- config.json values <- explicit overrides (None = not given)."""
    cfg = dict(PLAYER_BAR_DEFAULTS)
    for k in PLAYER_BAR_DEFAULTS:
        if config and config.get(k) is not None:
            cfg[k] = config[k]
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    return cfg


@dataclass
class SlotReading:
    index: int          # 0-based among the REAL slots (the glitch slot is already dropped)
    x: int              # sampled column
    saturation: int     # HSV S of the portrait pixel (0-255)
    alive: bool         # what sample_player_alive says with the effective threshold
    name: Optional[str] = None


@dataclass
class Report:
    config: dict
    image_size: tuple
    bar: Optional[list]
    separators: list = field(default_factory=list)   # separator centre columns, absolute x
    slots: list = field(default_factory=list)        # SlotReading per real slot
    ocr_available: bool = False
    problems: list = field(default_factory=list)

    @property
    def n_slots_raw(self) -> int:
        """Slots the separator scan implies, glitch included (0 = no bar)."""
        return len(self.separators) + 1 if self.bar else 0


def separator_centers(img: np.ndarray, cfg: dict) -> list[int]:
    """The separator scan detect_player_slot_xs performs, without its 4-12
    slot gate, so a bad region still reports what it saw."""
    bar = cfg.get("player_bar_region")
    if not bar:
        return []
    x0, y0, x1, y1 = (int(v) for v in bar)
    bar_h = y1 - y0
    sample_ys = [y0 + off for off in (bar_h // 8, bar_h // 5, bar_h // 3) if 0 <= y0 + off < img.shape[0]]
    if not sample_ys or x1 <= x0:
        return []
    profiles = [np.max(img[r, x0:x1], axis=1).astype(np.float32) for r in sample_ys]
    combined = np.min(np.stack(profiles, axis=0), axis=0)
    is_sep = combined < int(cfg.get("player_separator_threshold", 25))
    centers, in_sep, start = [], False, 0
    for i, s in enumerate(is_sep):
        if s and not in_sep:
            in_sep, start = True, i
        elif not s and in_sep:
            centers.append(x0 + (start + i) // 2)
            in_sep = False
    if in_sep:
        centers.append(x0 + (start + len(is_sep)) // 2)
    return centers


def portrait_saturation(img: np.ndarray, cfg: dict, x: int) -> int:
    bar = cfg["player_bar_region"]
    y = int(bar[1]) + int(cfg.get("player_portrait_y_in_bar", 35))
    if not (0 <= y < img.shape[0] and 0 <= x < img.shape[1]):
        return -1
    bgr = img[y, x].reshape(1, 1, 3).astype(np.uint8)
    return int(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[0, 0][1])


def analyze(img: np.ndarray, config: Optional[dict] = None, overrides: Optional[dict] = None,
            ocr: bool = True) -> Report:
    """Run the bot's own detection on `img` (BGR, as cv2 loads it) and report."""
    cfg = effective_config(config, overrides)
    rep = Report(config=cfg, image_size=(int(img.shape[1]), int(img.shape[0])), bar=cfg.get("player_bar_region"))
    if not rep.bar:
        rep.problems.append(
            "player_bar_region is not set: the bot logs 'no slots detected — player tracking disabled' "
            "and sends no names, first blood or eliminations. Set it to [x0, y0, x1, y1] of the card strip."
        )
        return rep
    x0, y0, x1, y1 = (int(v) for v in rep.bar)
    if not (0 <= x0 < x1 <= img.shape[1] and 0 <= y0 < y1 <= img.shape[0]):
        rep.problems.append(
            f"player_bar_region {rep.bar} is outside the {rep.image_size[0]}x{rep.image_size[1]} image "
            f"(is this a native-resolution capture?)"
        )
        return rep

    sd = _detection()
    rep.separators = separator_centers(img, cfg)
    n = rep.n_slots_raw
    slot_xs = sd.detect_player_slot_xs(img, cfg)
    if not slot_xs:
        rep.problems.append(
            f"separator scan found {len(rep.separators)} separators → {n} slots; the bot accepts 4-12 and "
            f"gives up otherwise. Adjust player_bar_region (must span exactly the card strip) or "
            f"player_separator_threshold (try --sweep)."
        )
        return rep

    for i, x in enumerate(slot_xs):
        sat = portrait_saturation(img, cfg, x)
        rep.slots.append(SlotReading(index=i, x=int(x), saturation=sat, alive=bool(sd.sample_player_alive(img, x, cfg))))

    if ocr:
        try:
            from game import ocr as ocr_mod
            names = ocr_mod.ocr_player_names(img, slot_xs, cfg)
            if len(names) == len(rep.slots) and all(isinstance(nm, str) for nm in names):
                for s, nm in zip(rep.slots, names):
                    s.name = nm
                rep.ocr_available = True
                if all(not (s.name or "").strip() for s in rep.slots):
                    rep.problems.append(
                        "OCR read no names: check player_name_y_in_bar / player_name_h against the nameplate "
                        "strip in the annotated image, and that tesseract is installed."
                    )
        except Exception as e:  # pytesseract / tesseract binary missing, etc.
            rep.problems.append(f"OCR unavailable here ({e.__class__.__name__}: {e}); names not checked.")

    sats = [s.saturation for s in rep.slots if s.saturation >= 0]
    thr = int(cfg.get("player_saturation_threshold", 40))
    if sats and (max(sats) - min(sats)) < 15:
        rep.problems.append(
            f"portrait saturation is nearly uniform across slots ({min(sats)}-{max(sats)}): the sample row "
            f"(player_portrait_y_in_bar={cfg['player_portrait_y_in_bar']}) is probably not on the portraits."
        )
    near = [s.index for s in rep.slots if s.saturation >= 0 and abs(s.saturation - thr) <= 8]
    if near:
        rep.problems.append(
            f"slots {near} sit within 8 of player_saturation_threshold={thr}; alive/dead will flicker there."
        )
    return rep


def sweep(img: np.ndarray, cfg: dict, thresholds=range(10, 90, 5)) -> list[tuple[int, int]]:
    """(threshold, implied slot count) for a range of separator thresholds."""
    out = []
    for t in thresholds:
        c = dict(cfg)
        c["player_separator_threshold"] = t
        out.append((t, len(separator_centers(img, c)) + 1 if cfg.get("player_bar_region") else 0))
    return out


def annotate(img: np.ndarray, rep: Report) -> np.ndarray:
    """The frame with the bar, separators, portrait samples and name strips drawn on."""
    out = img.copy()
    if not rep.bar:
        cv2.putText(out, "player_bar_region not set", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        return out
    x0, y0, x1, y1 = (int(v) for v in rep.bar)
    cv2.rectangle(out, (x0, y0), (x1, y1), (0, 255, 255), 1)
    for sx in rep.separators:
        cv2.line(out, (sx, y0), (sx, y1), (255, 0, 255), 1)
    py = y0 + int(rep.config.get("player_portrait_y_in_bar", 35))
    ny = y0 + int(rep.config.get("player_name_y_in_bar", 62))
    nh = int(rep.config.get("player_name_h", 14))
    for s in rep.slots:
        color = (0, 220, 0) if s.alive else (0, 0, 255)
        cv2.circle(out, (s.x, py), 4, color, -1)
        cv2.rectangle(out, (s.x - 40, ny), (s.x + 40, ny + nh), (255, 200, 0), 1)
        label = f"{s.index}:{s.saturation}" + (f" {s.name}" if s.name else "")
        cv2.putText(out, label, (s.x - 40, y1 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
    return out


def config_snippet(rep: Report) -> dict:
    """The six keys with the values this report used — paste into config.json."""
    return {k: rep.config.get(k) for k in PLAYER_BAR_DEFAULTS}


def format_report(rep: Report, sweep_rows: Optional[list] = None) -> str:
    lines = [
        f"image: {rep.image_size[0]}x{rep.image_size[1]}   bar: {rep.bar}",
        f"separator threshold {rep.config['player_separator_threshold']} → {len(rep.separators)} separators → "
        f"{rep.n_slots_raw} slots (glitch included; bot accepts 4-12)",
    ]
    if rep.slots:
        thr = rep.config["player_saturation_threshold"]
        lines.append(f"portrait row y={int(rep.bar[1]) + rep.config['player_portrait_y_in_bar']}  "
                     f"saturation threshold {thr}  (alive = saturation > threshold)")
        lines.append("  slot   x   sat  alive  name")
        for s in rep.slots:
            lines.append(f"  {s.index:>4} {s.x:>5} {s.saturation:>4}  {'yes' if s.alive else 'NO ':>5}  {s.name or ''}")
        if not rep.ocr_available:
            lines.append("  (names not read — OCR unavailable on this machine)")
    if sweep_rows:
        lines.append("separator threshold sweep (threshold → slots): " +
                     ", ".join(f"{t}→{n}" for t, n in sweep_rows))
    if rep.problems:
        lines.append("PROBLEMS:")
        lines += [f"  - {p}" for p in rep.problems]
    else:
        lines.append("no problems found — paste the snippet below into config.json and restart the bot")
    return "\n".join(lines)

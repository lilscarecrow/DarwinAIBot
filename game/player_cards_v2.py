"""
player_cards_v2.py — geometry-based player-card detection (V2).

Why a second detector: the V1 scan in screen_detection.detect_player_slot_xs
looks for dark separator columns inside a fixed player_bar_region. On real
frames (measured on 1920x1080 VOD frames, 2026-09-06) that finds 20-60
"separators" — the translucent HUD lets the game world through, and the
strip is not a fixed box anyway: it is CENTRED on the screen and its width
follows the player count (9, 10, 11 cards at a constant ~131.6px pitch).

V2 uses that geometry. For a candidate count n the card centres are
    x_i = center_x + (i - (n-1)/2) * pitch
and each candidate is tested for the one thing every card has: a name band.
    alive  → a solid, saturated, bright colour band (V≈186, S≈255 on every
             background measured), full card width
    dead   → no such band, and a red X painted over the portrait (15-27 %
             strong-red pixels in the portrait box; alive portraits 0-2 %)
The spectated player's card is drawn enlarged with its band lower, so the
band is searched over a range of rows, not one. The n whose positions ALL
read as cards wins (largest such n; an expected count from the lobby breaks
ties). The always-present "glitch" card sits LEFT of the centred block and
is never a candidate.

Select with config `player_bar_detector`: "v1" (default, unchanged
behaviour), "v2" (this drives the bot), or "shadow" (v1 drives, v2 reports
what it would have said as live events / log lines for an A/B on the ladder).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

V2_DEFAULTS: dict = {
    "player_cards_center_x": 960.0,     # centre of the real-card block, px (1920x1080)
    "player_cards_pitch": 131.6,        # card-to-card distance, px
    "player_cards_band_rows": [104, 152],   # rows scanned for the name band (spectated card sits lower)
    "player_cards_band_min_rows": 6,    # a bright run must be at least this tall to be the band
    "player_cards_margin": [42, 58],    # band sampled this far either side of centre (avoids the name text)
    "player_cards_alive_v": 140,        # band brightness above this = alive ...
    "player_cards_alive_s": 120,        # ... together with saturation above this
    "player_cards_dead_rows": [112, 128],   # a normal card's band rows (reported for a dead card)
    "player_cards_portrait_rows": [70, 110],  # the portrait box, where a dead card's red X is painted
    "player_cards_portrait_half": 26,   # half-width of that box
    "player_cards_dead_red": 0.08,      # fraction of strong-red pixels in the box above which = dead
    "player_cards_min": 2,
    "player_cards_max": 10,
}


def v2_config(config: Optional[dict]) -> dict:
    cfg = dict(V2_DEFAULTS)
    for k in V2_DEFAULTS:
        if config and config.get(k) is not None:
            cfg[k] = config[k]
    return cfg


@dataclass
class Card:
    index: int
    x: int
    alive: bool
    band_y: int        # top row of the band window that decided it
    band_v: int
    band_s: int
    red_x: float       # strong-red fraction of the portrait box (the eliminated X)


def positions(n: int, cfg: dict) -> list[int]:
    cx, pitch = float(cfg["player_cards_center_x"]), float(cfg["player_cards_pitch"])
    return [int(round(cx + (i - (n - 1) / 2) * pitch)) for i in range(n)]


def _hsv(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2HSV)


def _margins(win: np.ndarray, x: int, m0: int, m1: int) -> tuple[np.ndarray, np.ndarray]:
    return win[:, x - m1:x - m0], win[:, x + m0:x + m1]


def red_x_fraction(hsv: np.ndarray, x: int, cfg: dict) -> float:
    """Fraction of strongly red pixels in the portrait box: an eliminated
    card has a red X painted over a greyscale portrait (0.15-0.27 measured);
    an alive portrait reads 0.00-0.02 unless the photo itself is red, and
    such a card is caught by its bright band first."""
    p0, p1 = (int(v) for v in cfg["player_cards_portrait_rows"])
    half = int(cfg["player_cards_portrait_half"])
    box = hsv[p0:p1, max(0, x - half):x + half]
    if box.size == 0:
        return 0.0
    h, s_, v = box[:, :, 0].astype(int), box[:, :, 1].astype(int), box[:, :, 2].astype(int)
    red = ((h < 8) | (h > 172)) & (s_ > 150) & (v > 120)
    return float(red.mean())


def read_card(hsv: np.ndarray, x: int, cfg: dict) -> Optional[Card]:
    """What the card at column x says: an alive Card, a dead Card, or None
    (nothing card-like there).

    Alive: scanning DOWN from the top of the band range, the first run of at
    least `band_min_rows` rows where BOTH margins are bright and saturated.
    Taking the first run is what separates the name band (rows ~112-127) from
    the health bar under it (~136-147, also bright and saturated) and still
    finds the spectated card's lower band (~130-148, the rows above it dark).
    Dead: no such band, and the red X over the portrait.
    """
    h, w = hsv.shape[:2]
    m0, m1 = (int(v) for v in cfg["player_cards_margin"])
    r0, r1 = (int(v) for v in cfg["player_cards_band_rows"])
    if x - m1 < 0 or x + m1 >= w or r1 >= h:
        return None
    av, as_ = float(cfg["player_cards_alive_v"]), float(cfg["player_cards_alive_s"])
    need = int(cfg["player_cards_band_min_rows"])
    run_start, run_len = None, 0
    for y in range(r0, r1):
        left, right = _margins(hsv[y:y + 1], x, m0, m1)
        ok = all(np.median(m[:, :, 2]) > av and np.median(m[:, :, 1]) > as_ for m in (left, right))
        if ok:
            run_start = y if run_start is None else run_start
            run_len += 1
            if run_len >= need:
                band = hsv[run_start:run_start + run_len]
                left, right = _margins(band, x, m0, m1)
                both = np.concatenate([left, right], axis=1)
                return Card(index=-1, x=x, alive=True, band_y=run_start,
                            band_v=int(np.median(both[:, :, 2])), band_s=int(np.median(both[:, :, 1])),
                            red_x=0.0)
        else:
            run_start, run_len = None, 0
    rx = red_x_fraction(hsv, x, cfg)
    if rx > float(cfg["player_cards_dead_red"]):
        d0 = int(cfg["player_cards_dead_rows"][0])
        return Card(index=-1, x=x, alive=False, band_y=d0, band_v=0, band_s=0, red_x=rx)
    return None


def detect_cards(img: np.ndarray, config: Optional[dict] = None, expected: Optional[int] = None) -> list[Card]:
    """All real player cards in the strip, left to right. Empty when no
    candidate count n has every position reading as a card."""
    cfg = v2_config(config)
    hsv = _hsv(img)
    lo, hi = int(cfg["player_cards_min"]), int(cfg["player_cards_max"])
    complete: dict[int, list[Card]] = {}
    for n in range(lo, hi + 1):
        cards = []
        for x in positions(n, cfg):
            c = read_card(hsv, x, cfg)
            if c is None:
                break
            cards.append(c)
        if len(cards) == n:
            complete[n] = cards
    if not complete:
        return []
    # A count that fits with an even/odd offset different from the truth puts
    # windows on card edges and fails; among the ones that fit, the largest is
    # the strip (a smaller n is a subset of it). The lobby's expected count
    # wins when it fits.
    n = expected if expected in complete else max(complete)
    out = complete[n]
    for i, c in enumerate(out):
        c.index = i
    return out


def cards_alive(img: np.ndarray, xs: list[int], config: Optional[dict] = None) -> list[bool]:
    """Alive flags at known card columns (the per-poll call). A column that
    reads as nothing keeps its previous meaning by returning True — the same
    "assume alive" fallback V1 uses when it cannot sample."""
    cfg = v2_config(config)
    hsv = _hsv(img)
    out = []
    for x in xs:
        c = read_card(hsv, int(x), cfg)
        out.append(True if c is None else c.alive)
    return out


def name_crop(img: np.ndarray, card: Card, cfg: Optional[dict] = None) -> np.ndarray:
    """The band's text area for OCR: 18 rows from the band's top, centre 100px."""
    y0 = max(0, card.band_y - 2)
    return img[y0:y0 + 18, max(0, card.x - 50):card.x + 50]


def ocr_prepare(crop: np.ndarray, scale: int = 4, pad: int = 20) -> np.ndarray:
    """Name-band crop → image tesseract reads best (measured on 48 VOD crops
    with the same Windows tesseract 5.4 the bot uses: 35/48 exact vs 30/48
    for V1's grey+Otsu, the rest being the unreadable ':]' handle and y↔v
    slips the ladder's glyph fold absorbs).

    The band is WHITE text on a SATURATED colour (or grey on dark when
    eliminated). A plain grey conversion throws that contrast away — a blue
    band and white text have similar luma. The per-pixel MIN channel keeps
    it: white text stays bright, a coloured band goes dark. Inverted so the
    text is dark on light (tesseract's preference), left un-thresholded, and
    padded with a white border so the line is not glued to the edges."""
    up = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    g = np.min(up, axis=2).astype(np.uint8)
    return cv2.copyMakeBorder(255 - g, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def ocr_names(img: np.ndarray, cards: list[Card], cfg: Optional[dict] = None) -> list[str]:
    """Best-effort names via the same tesseract path V1 uses; [] if OCR is
    unavailable. See ocr_prepare for the preprocessing."""
    try:
        from game.ocr import _ocr_region_img
    except Exception:
        return []
    names = []
    for c in cards:
        crop = name_crop(img, c, cfg)
        if crop.size == 0:
            names.append("")
            continue
        try:
            names.append(_ocr_region_img(ocr_prepare(crop), config="--psm 7").strip())
        except Exception:
            names.append("")
    return names

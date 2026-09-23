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

    SUPERSEDED (2026-09-15) by read_director_point_pips() below — single-pixel
    brightness sampling overcounts whenever a same-colored background element
    sits at that exact coordinate (a same-sized, same-colored decorative ring
    surrounds every pip regardless of fill state, which this function's single
    sample point can land directly on), and has no way to distinguish a
    partially-filled pip (the meter fills via a radial wipe, not an instant
    color swap) from filled/empty at all. Left in place, unused everywhere,
    same "disconnected, not deleted" precedent as this codebase's other
    superseded-but-possibly-useful-again code — delete outright once
    read_director_point_pips() has proven solid in production (that was the
    explicit plan when it was built, not a maybe).
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


# Inner-mask radius for _pip_fill_fraction() below — measured live (2026-09-15)
# against a real pip: the fill disc itself extends to about radius 11 from
# center, with a decorative ring (bright regardless of fill state) sitting at
# roughly radius 12-15, and background/gap beyond that. 9 sits safely inside
# the disc, clear of the ring, so the ring's always-bright pixels never bias
# the reading toward "filled" the way a same-colored ring easily could.
_PIP_INNER_RADIUS = 9

# A pixel counts as "lit" above this brightness (max of B/G/R) — same idea as
# count_director_point_pips' single-pixel check, just applied per-pixel across
# a whole masked area instead of to one point, and only ever used as an
# ingredient in a per-pip AREA fraction (see _pip_fill_fraction), never as a
# standalone filled/empty verdict on its own.
_PIP_BRIGHT_THRESHOLD = 150

# A pip whose measured fill fraction sits below this can't be mistaken for
# filled, and above this can't be mistaken for empty — measured live: real
# filled pips read ~0.92, empty ~0.07, a pip mid-radial-fill ~0.30-0.51. Used
# only for the whole-row "all filled" / "all empty" special cases below, where
# there's no second group to compare against (see their own comments for why
# that needs a different check than the interior split search).
_PIP_ALL_HIGH_FLOOR = 0.6
_PIP_ALL_LOW_CEILING = 0.15

# The winning interior split must beat the next-best candidate by at least
# this big a margin (best_sse / runner_up_sse must fall below this ratio) to
# be trusted at all. Measured live: a genuine filled/empty boundary won by
# ~39x (ratio ~0.026); a uniformly-pulsing row with no real boundary (the
# whole meter capped at 10/10, idling) still "won" some arbitrary split by
# only ~1.1-1.5x (ratio ~0.67-0.94) purely from ripple/gradient noise across
# nominally-identical pips — nowhere near a real boundary's margin. 0.35 sits
# comfortably below every genuine-noise ratio observed and comfortably above
# the one real split observed; revisit if production data ever disagrees.
_PIP_SPLIT_CONFIDENCE_RATIO = 0.35

# The winning split's "filled" group must ALL clear this absolute floor, not
# just be relatively higher than the "empty" group (2026-09-15 fix, found
# live: a genuinely half-filled pip, mid-radial-wipe at ~0.61, got grouped
# into "filled" alongside a truly-filled pip that itself only measured
# ~0.74). The relative confidence-ratio gate above didn't catch this: the
# split {0.74, 0.61} vs {0.10, ...} is a clean, well-separated two-group fit
# by SSE alone — the SSE metric has no opinion on whether the "filled"
# group's own values are anywhere near what a real filled pip reads, only on
# how tightly each group clusters internally.
#
# Raised 0.65 -> 0.80 (2026-09-17), after a ~4-minute live cross-signal
# monitor (74 sampled frames, OCR vs. pips side by side) caught the same
# failure mode again at the ORIGINAL 0.65 floor: a pip mid-radial-wipe at
# 0.70-0.72 got counted as filled while OCR steadily read one lower for the
# same span of frames (the pip was still visibly climbing — 0.72 -> 0.83
# across consecutive polls — not yet actually done). That session's data
# also showed every genuinely-settled filled pip reading 0.84-0.96, with no
# recurrence at all of the single ~0.74 dim-state case that originally set
# the floor this low — so 0.80 was chosen as sitting cleanly above every
# confirmed still-filling reading observed (0.70, 0.72) and cleanly below
# every confirmed genuinely-filled reading observed (0.84 and up) in that
# same session, at the cost of no longer accepting that one historical 0.74
# outlier if it turns out to be a real, recurring render state rather than a
# one-off — revisit if it resurfaces.
#
# This is an inherently fuzzy boundary, not a clean one: a single frame's
# fill fraction alone cannot always distinguish "genuinely done, just
# naturally reading a bit low this frame" from "still filling, about to
# finish" — both can transiently occupy the same value range. A pip caught
# at exactly the wrong instant (high 0.70s/low 0.80s) may still occasionally
# go either way; only comparing consecutive frames for a still-rising trend
# could resolve that class of case with confidence, which this constant
# alone does not attempt.
_PIP_FILLED_GROUP_FLOOR = 0.80


def _pip_fill_fraction(screenshot: np.ndarray, x: int, y: int) -> float | None:
    """Fraction of a pip's inner disc (radius _PIP_INNER_RADIUS, centered at
    x,y) that reads brighter than _PIP_BRIGHT_THRESHOLD — a per-pip AREA
    measurement standing in for "how much of this pip's radial fill wipe has
    completed", not a binary filled/empty verdict. A circular mask (not a
    square box) matters here: a square's corners reach past the pip's own
    disc into the decorative ring/background around it, which is exactly the
    kind of contamination this whole redesign exists to avoid — see
    read_director_point_pips()'s docstring.

    Returns None if the sample area falls outside the screenshot (a bad
    coordinate, or a differently-sized/positioned capture).
    """
    r = _PIP_INNER_RADIUS
    h, w = screenshot.shape[:2]
    if not (r <= x < w - r and r <= y < h - r):
        return None
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    mask = xx * xx + yy * yy <= r * r
    box = screenshot[y - r:y + r + 1, x - r:x + r + 1]
    bmax = box.max(axis=2)
    return float((bmax[mask] > _PIP_BRIGHT_THRESHOLD).mean())


def read_director_point_pips(screenshot: np.ndarray, pips_cfg: dict) -> int | None:
    """Count filled director point pips via a per-frame, self-referential
    split search (2026-09-15) — replacing count_director_point_pips()'s
    single-pixel brightness check, which had two problems live testing
    confirmed can't be patched with more/bigger samples at the same spot:

    1. A same-colored background element sitting at a pip's exact screen
       position reads as "filled" regardless of how many pixels you sample
       there, if the whole sampled area is uniformly contaminated — averaging
       a bigger patch doesn't help when every pixel in it agrees with the
       wrong answer.
    2. Each pip fills via a radial wipe animation, not an instant color swap,
       and the WHOLE ROW (filled and empty pips alike) also grows/shrinks and
       shifts color together in a ~1s pulse once points sit banked for a
       while — so there is no fixed "what does a filled pip look like"
       reference that stays valid over time. A stored calibration image (the
       first fix attempted here) would only ever match the exact instant it
       was captured.

    The fix: don't compare any pip's appearance to a stored reference at all.
    Compare the row to ITSELF, this frame only. Points fill strictly
    left-to-right, so the true state is always "first K pips filled, the rest
    empty" — one of only `count + 1` possibilities. For each candidate K,
    score how well the two groups it implies (positions 0..K-1 vs K..end)
    hang together (sum of within-group variance in each group's own fill
    fraction — see _pip_fill_fraction); the true boundary should produce two
    tight, well-separated groups whatever the row's current pulse phase
    happens to be, since a synchronized pulse moves both groups' baseline
    together without changing the gap between them.

    Two extra guards, both grounded in live measurements (see the module-level
    comments on the constants used here for the exact numbers):

    - Whole-row extremes (fully capped at `count`, or fully empty) are
      checked FIRST, via a straightforward "is every pip clearly bright" / "is
      every pip clearly dim" floor-and-ceiling test — not the split search.
      A fully uniform row has no second group to meaningfully separate from,
      so forcing the split search to judge it produces exactly the failure
      mode a live capture caught: an all-10-filled, mid-pulse row still
      "won" some arbitrary interior split by a thin margin, from nothing more
      than ripple noise across nominally-identical pips.
    - For everything else, the winning interior split must beat the
      next-best candidate by a wide margin (_PIP_SPLIT_CONFIDENCE_RATIO) to
      be trusted — the same live capture showed a genuine boundary winning by
      ~39x while ripple-driven false splits won by only ~1.1-1.5x, so this
      cleanly tells them apart. A frame that doesn't clear the bar returns
      None (no read this poll) rather than a guess — the next poll, at a
      different, uncorrelated point in the ~1s pulse cycle, gets another shot
      (see MatchRunner._update_points_reading()'s temporal agreement, which
      this feeds into the same way an OCR read does).

    Returns the filled-pip count (0..count), or None if config is invalid,
    the sample area is out of bounds, or no reading clears the confidence bar.
    """
    try:
        x0 = int(pips_cfg["x_start"])
        y = int(pips_cfg["y"])
        sp = int(pips_cfg["spacing"])
        n = int(pips_cfg["count"])
    except (KeyError, TypeError, ValueError):
        return None
    if n < 2:
        return None

    fracs = []
    for i in range(n):
        f = _pip_fill_fraction(screenshot, x0 + i * sp, y)
        if f is None:
            return None
        fracs.append(f)
    fracs = np.array(fracs)

    if fracs.min() > _PIP_ALL_HIGH_FLOOR:
        return n
    if fracs.max() < _PIP_ALL_LOW_CEILING:
        return 0

    candidates = []
    for k in range(1, n):
        left, right = fracs[:k], fracs[k:]
        sse = left.var() * len(left) + right.var() * len(right)
        candidates.append((sse, k))
    candidates.sort(key=lambda t: t[0])

    best_sse, best_k = candidates[0]
    runner_up_sse = candidates[1][0] if len(candidates) > 1 else None
    if not runner_up_sse:  # None, or exactly 0 (can't compute a meaningful ratio)
        return None
    if best_sse / runner_up_sse > _PIP_SPLIT_CONFIDENCE_RATIO:
        return None
    if fracs[:best_k].min() < _PIP_FILLED_GROUP_FLOOR:
        return None
    return best_k


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


# ocr_feed_text()'s current preprocessing (2026-09-10, replacing the min-channel
# trick — see its docstring): a pixel is "ink" (part of a letter's outline
# stroke) when the brightest of its B/G/R channels is below this. Measured
# directly off two real captured first-blood banners (screenshots/errors/
# 2026-09-10_13-53-19_..._feed_first_blood_raw.png and its 13-53-32 pair):
# the outline stroke around every glyph, regardless of fill color, sampled
# consistently at BGR ~(28,15,0) (max channel <= ~30, occasionally up to ~76
# on an anti-aliased edge pixel), while both real backgrounds sampled well
# above that (wood grain: max~130-150; snow: much higher) and both fill
# colors present (white action words, and the orange/red player-name color)
# sampled far above it too (fill pixels max out well over 200). 60 sits
# comfortably in the gap and was cross-checked against pytesseract directly
# on both real crops across a 40-90 range with identical output at every
# value tried.
_FEED_TEXT_DARKNESS_THRESHOLD = 60


def _prepare_feed_crop(screenshot: np.ndarray, region: tuple[int, int, int, int]):
    """Shared by ocr_feed_text() and save_feed_debug_images() so the debug
    dump is guaranteed to show exactly what OCR actually saw, not a
    recomputed approximation. Returns (raw_crop, processed) — both None if
    `region` is out of bounds. `raw_crop` is the natural-color crop straight
    off the screenshot, before any preprocessing touches it — the only
    place to read a player name's actual on-screen color from. `processed`
    is the binary outline mask ocr_feed_text() feeds to tesseract — see its
    docstring and _FEED_TEXT_DARKNESS_THRESHOLD's comment for why outline
    detection, not fill-color detection, is what this function does.
    """
    x, y, w, h = (int(v) for v in region)
    if y + h > screenshot.shape[0] or x + w > screenshot.shape[1]:
        return None, None
    crop = _crop(screenshot, x, y, w, h)
    scaled = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    value = np.max(scaled, axis=2).astype(np.uint8)
    processed = np.where(value < _FEED_TEXT_DARKNESS_THRESHOLD, 0, 255).astype(np.uint8)
    return crop, processed


def ocr_feed_text(screenshot: np.ndarray, region: tuple[int, int, int, int]) -> str:
    """OCR the damage/kill feed area as a multi-line block (`--psm 6`) —
    Darwin Project stacks several recent feed lines in this area, not just
    one. `region` is `(x, y, w, h)`; callers pass
    game.video_recorder._CROP_REGION, the same crop already used for match
    recordings, rather than a separately calibrated config key — an earlier
    single-line version of this (`ocr_kill_notification`, `--psm 7`, gated on
    a `kill_notification_region` config key) was removed 2026-09-10: it was
    never calibrated on any machine, and its one call site
    (MatchRunner._poll_player_bar()'s first-blood branch) was inline/blocking
    on the match thread, the exact risk this function's caller
    (MatchRunner._poll_damage_feed(), its own background thread) was built to
    avoid.

    **Preprocessing detects the glyph OUTLINE, not the fill color
    (2026-09-10, rewritten from an earlier min-channel-invert approach —
    see below for why that one wasn't enough).** Player names in this feed
    all render in one fixed orange/red color (not per-player — every name
    uses the same one), distinct from the white action words — but a fixed
    color is no easier to isolate directly than a varying one would be, and
    the min-channel approach below already showed why "isolate the fill
    color" doesn't generalize. Every glyph in this font, no matter its
    fill, is drawn with the same dark near-black outline stroke (see
    _FEED_TEXT_DARKNESS_THRESHOLD's comment for the actual measured
    values) — so `_prepare_feed_crop()` thresholds on that instead: any
    pixel darker than the threshold is treated as ink, everything else
    (background OR either fill color) is background. Color-blind by
    construction, so it would keep working even if the name color ever
    changed.

    Superseded approach, kept here for context: upscale 3x, take the
    per-pixel MIN across B/G/R, invert — the same trick
    game/player_cards_v2.py::ocr_prepare() uses for player-bar names. That
    separates WHITE text from a colored background (white's min channel
    stays high; the earlier fixed-threshold-grayscale version before that
    couldn't even manage this) but breaks down for the orange/red name
    color: its minimum channel is its low blue channel, the same as a
    colored background's low channel, so colored text and background
    became indistinguishable — found live 2026-09-10, action words parsed
    fine but names came back as pure garbage (`"~~"`, `"ww ly"`, later
    confirmed via a captured real banner to literally read `"~~~) ... é"`
    for `"TWO DREW FIRST BLOOD FROM PEFISS"`). The outline-based rewrite
    above was verified directly against pytesseract on that same real
    capture (and a second one) and reads both names correctly.

    Returns raw OCR text (may contain multiple lines), or "" if the region
    is out of bounds.
    """
    _, processed = _prepare_feed_crop(screenshot, region)
    if processed is None:
        return ""
    return _ocr_region_img(processed, config="--psm 6").strip()


def save_feed_debug_images(screenshot: np.ndarray, region: tuple[int, int, int, int], out_dir: str, label: str) -> None:
    """Saves the raw natural-color crop AND the processed image
    ocr_feed_text() actually hands to tesseract, side by side, as
    `{label}_raw.png` / `{label}_processed.png` under `out_dir`. Pulled out
    of ocr_feed_text() itself (rather than a `debug=True` flag there, the
    pattern read_lobby_countdown() uses) because ocr_feed_text() runs on
    every single feed poll — most of which find nothing worth keeping — so
    the decision to pay disk I/O belongs to the caller, only once it knows
    the result was actually interesting (match_runner.py's
    _poll_damage_feed_worker() calls this only when a feed pattern actually
    matched). The raw crop is what to inspect for a player name's true
    on-screen color — see ocr_feed_text()'s docstring.
    No-ops (logs nothing, raises nothing) if `region` is out of bounds.
    """
    crop, processed = _prepare_feed_crop(screenshot, region)
    if crop is None:
        return
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out / f"{label}_raw.png"), crop)
    cv2.imwrite(str(out / f"{label}_processed.png"), processed)
    logger.info("Feed debug images saved: %s_raw.png / %s_processed.png in %s", label, label, out_dir)


def read_lobby_countdown(screenshot: np.ndarray, debug: bool = False) -> int | None:
    """
    OCR the 'CUSTOM MATCH EXPIRES IN MM:SS' orange banner in the Director lobby.
    Region is at the top-left of the 1920×1080 screen.
    Returns remaining seconds, or None if the text cannot be parsed.
    Set debug=True to save intermediate images to calibration_screenshots/ for inspection.

    Only ever called moments after /custom creates the lobby (see
    bot/discord_bot.py's auto-start watcher), against a fixed ~20-minute
    lobby timeout. A grep of months of `logs/darwin_bot.log` confirms this
    empirically: every successfully-parsed read is "19:XX" — hundreds of
    them, never once "18:XX" or "20:XX" — with one recurring exception: the
    leading "1" of "19" occasionally OCRs as a stray glyph ("(", "[", "L")
    that the digit regex below doesn't match, so it silently drops out of
    the captured minutes group and leaves a lone, correctly-read ones digit
    behind (e.g. raw text "(9:47" -> captured minutes "9" -> parsed as 9m47s
    when the truth was 19m47s). Found live 2026-09-10: this exact failure
    fired a match roughly 10 minutes early, since the watcher's whole sleep
    duration is derived from this one number.
    A single-digit minutes capture is therefore reconstructed as 10 + that
    digit rather than trusted as-is — this is not a guess, it's the same
    reconstruction a human reading "(9:47" fresh off a 20-minute timer would
    make instantly. (Kept as a single read, not a double-read confirmation —
    an earlier version of this fix added a second, gap-delayed read and
    required the two to agree; reconstructing at the source makes that
    redundant, since the only failure mode it was catching is this one.)
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
        minutes_str, seconds = match.group(1), int(match.group(2))
        minutes = int(minutes_str)
        if len(minutes_str) == 1:
            # The leading "1" dropped out — see docstring. Reconstruct, don't trust.
            minutes += 10
            logger.info(
                "Lobby countdown OCR: %r → minutes captured as single digit %r, "
                "reconstructed as %dm (dropped leading '1')",
                text.strip(), minutes_str, minutes,
            )
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

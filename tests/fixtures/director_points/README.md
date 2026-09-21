# director_points fixtures

Raw, native-resolution (20×24, unscaled) crops of `_DIRECTOR_POINTS_REGION`,
saved live via `game.ocr._crop(screenshot, *_DIRECTOR_POINTS_REGION)` during a
live match on 2026-09-15 — the same day `read_director_points()` was rewritten
from grayscale+Otsu thresholding to background-color-distance detection (see
`game/ocr.py`'s docstring, and CLAUDE.md's Director points reading section).

The old grayscale approach failed on a large fraction of live-captured frames
because the numerator digits are part of the same synchronized pulse-idle
animation the pip row (`read_director_point_pips()`) already accounts for, and
render in different fill colors at different times — white in one state,
saturated pink in another — which a weighted-luminance grayscale conversion
doesn't reliably separate from an equally bright/saturated background.

| file | ground truth (by eye) | notes |
|---|---|---|
| `pink_on_blue_06.png` | `06` | pink digits on solid blue background |
| `pink_on_blue_07.png` | `07` | pink digits on solid blue background, different frame |
| `white_on_purple_10.png` | `10` | white digits on a light purple/lavender background — captured separately, confirms the fix isn't just "handles pink-on-blue" but genuinely color-blind, working off distance from whatever background is actually present |

All three were unreadable (or, worse, silently dropped a digit — see below)
under the old grayscale+Otsu preprocessing; all three read correctly under
the background-distance rewrite.

**The upscale factor matters, not just the color fix (2026-09-15, same day):**
an earlier version of this rewrite kept the original 4x upscale and initially
looked like a big improvement — but comparing it against 5x and 6x on 20 live
captures found it silently DROPPING one digit on some frames instead of
failing outright (e.g. a true `07` reading as plain `0`, a plausible-looking
but wrong value) where 6x either read both digits correctly or returned
nothing on the same frames. Zero confirmed wrong non-empty reads at 6x across
that dataset, versus two at 4x and one at 5x — `_POINTS_TEXT_UPSCALE` is 6 in
production for exactly this reason: a lower hit rate that fails safe beats a
higher one that occasionally lies.

`tests/test_ocr_director_points_colors.py` asserts `read_director_points()`
reads both correctly — a hard regression guard, not just a smoke test.

**A third live capture that day (a white-digit numerator over a tan/skin-tone
game-world background, not a flat UI panel) was clipped by
`_DIRECTOR_POINTS_REGION`'s own width** — the second digit's right edge fell
outside the calibrated 20×24 box. Both the old and new preprocessing failed
identically on that capture (confirmed directly), so it wasn't a regression
from this rewrite — it was the crop boundary itself. Not kept as a fixture
here (its ground truth crop is unevenly padded, not a clean same-shape
capture like the three above), but it's what motivated the region shift
below.

**`_DIRECTOR_POINTS_REGION` shifted right 3px (808 → 811) the same day, fixing
that clip.** Measuring ink-column extent against all three fixtures above
found the same pattern in every one: 3-6px of unused blank space on the LEFT
before the digit ink started, while the ink already touched the RIGHT edge
flush (zero margin) in all three — the crop was miscentered, not
(necessarily) undersized. Shifting right by 3 (the smallest observed left
margin, so none of these three lose any digit) recovered the clipped capture
directly: `read_director_points()` went from `None` to a correct read on that
exact frame with the shift alone. See CLAUDE.md's Director points reading
section for the full writeup, including why this shift alone won't rescue
every possible clip (that frame's true digit-pair width, ~33px, is still
wider than the 20px crop even after the shift — only recovered because
tesseract read the last digit alone, which happens to equal the full value
for anything under 10, not a general fix).

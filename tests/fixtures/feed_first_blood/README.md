# Damage-feed fixtures

Raw (unprocessed, natural-color) `_CROP_REGION` crops saved live by
`game/ocr.py::save_feed_debug_images()` — see `MatchRunner._save_feed_debug_images()`
in `game/match_runner.py`, called whenever a `_FEED_PATTERNS` match fires.
These two are the pair that motivated the 2026-09-10 outline-detection
rewrite of `ocr_feed_text()`'s preprocessing (see that function's
docstring): the old min-channel-invert approach read pure garbage for the
player names in both (`"~~~) ... é"`, `": ... :"`) because it can only
separate white text from a colored background, and every player name in
this feed renders in one fixed orange/red color (not per-player — the
same color for everyone), distinct from the white action words.

| file | ground truth (by eye) |
|---|---|
| feed_first_blood_1.png | `PEFISS DEALT 300 AXE DMG TO TWO` / `TWO DEALT 150 ARROW DMG TO PEFISS` / `TWO DREW FIRST BLOOD FROM PEFISS` |
| feed_first_blood_2.png | `TWO DREW FIRST BLOOD FROM PEFISS` (same first-blood event, caught on a later poll after the damage lines above had scrolled off) |

`tests/test_ocr_feed_colors.py` asserts `ocr_feed_text()` reads both names
correctly off these. Add a row (and a fixture) here if a future live catch
turns up a case this doesn't handle.

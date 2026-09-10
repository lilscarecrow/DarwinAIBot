# feed_kill fixtures

Raw `_CROP_REGION` crops saved live by `game/ocr.py::save_feed_debug_images()`
(see `MatchRunner._save_feed_debug_images()`), captured during a live match
on 2026-09-10 shortly after the outline-detection OCR rewrite landed (see
`tests/fixtures/feed_first_blood/README.md`). These two are the pair that
motivated the follow-up `\s+` → `\s*` regex relaxation in
`game/match_runner.py::_FEED_PATTERNS`: OCR text was now fully legible
(unlike before the outline rewrite), but tesseract renders near-zero gap
between a colored player name and an adjacent white word, so e.g.
`"TWO KILLED THUGZ BY ARROW"` reads as `"TWOKILLEDTHUGZBY ARROW"` — no
space around `KILLED`, one preserved before `ARROW` (a white-to-white
boundary). The old `\s+`-based pattern silently failed to match either of
these even though every word reads correctly.

| file | ground truth (by eye) | OCR'd text |
|---|---|---|
| feed_kill_1.png | `TWO KILLED THUGZ BY ARROW` | `TWOKILLEDTHUGZBY ARROW,` |
| feed_kill_2.png | `THEAURI KILLED KERO BY COLD` | `THEAURIKILLEDKEROBY COLD` |

`tests/test_ocr_feed_colors.py` asserts the `feed_kill` pattern matches
both, with the correct killer/victim/method groups. Six captures came in
from the same match; the other four either had no real kill line in that
particular frame (a stale "KILLED" match under the pre-rewrite OCR turned
out to be pure noise once read correctly) or had a specific letter-level
misread of "KILLED" itself (`"WONLLED"`, `"KLLED"`) severe enough that no
reasonable regex tolerance would catch it without risking false matches
elsewhere — not fixed, left as a known remaining gap.

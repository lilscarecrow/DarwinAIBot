# Player bar calibration — handoff

For scarecrow and for any Claude session working in this repo. Read this when
"alive/dead tracking doesn't work", "OCR is bugging", names come out as
`slot 3`, or the ladder's LIVE feed shows no eliminations.

## 1. What is going on (2026-09-06)

Everything the bot knows about players during a match comes from ONE strip of
pixels: the HUD card bar across the top of the screen. From it the bot:

1. finds the card slots (dark separator columns inside `player_bar_region`),
2. reads each nameplate once with tesseract (`player_name_y_in_bar`, `player_name_h`),
3. decides alive/dead every poll by sampling ONE portrait pixel per slot and
   checking its colour saturation (`player_portrait_y_in_bar`,
   `player_saturation_threshold`).

Those six keys were added to the code (commit 89e512e) but never to the config
template, never documented, and nothing in the repo shows a calibrated value.
The tracked screenshots are menu captures with no player bar in them.
**If `player_bar_region` is missing from config.json — which is the likely
case — `detect_player_slot_xs` returns nothing, the bot logs
`Player bar snapshot: no slots detected — player tracking disabled`, and no
names, first blood or eliminations are ever produced.** That is not OCR
misreading; it is tracking never starting.

Since the ladder relay shipped (docs/DS_LIFECYCLE_HANDOFF.md), that warning
also appears on darwinstalker.com's /admin/observability as `bot.warning`
after the bot is restarted, so lo can see it without your log.

## 1b. The V2 detector (2026-09-06) — use this

Running V1 on real 1920×1080 frames from the Twitch VOD showed it cannot
work even with a perfect region: the separator scan found 23-61
"separators" per frame (the translucent HUD lets the scenery through), and
the strip is not a fixed box — it is centred on the screen and its width
follows the player count.

`game/player_cards_v2.py` uses that geometry instead: card centres are
`960 + (i − (n−1)/2) × 131.6` for a count n, and each candidate is read by
the one thing every card has — the name band (solid bright colour = alive)
or the red X over the portrait (= eliminated). The spectated player's card
is drawn larger with its band lower, so the band is searched over a row
range. No calibration is needed at 1920×1080. It is exact on all five
labelled VOD frames in `tests/fixtures/player_bar/` (blood-moon tint, snow,
the director-panel layout, spectated cards), and `pytest` keeps it that way.

Pick the detector in config.json:

| `player_bar_detector` | what runs |
|---|---|
| `"v1"` (default) | today's behaviour, unchanged |
| `"shadow"` | V1 drives the match; V2 also runs and reports `detector_v2` (count, alive, positions) at match start and `eliminated_v2` on every flip it sees — compare against the game's alive counter and V1 on the ladder's event stream, zero risk |
| `"v2"` | V2 drives slots, names and alive/dead; V1 is not used |

Recommended path: `"shadow"` for one session, then `"v2"`.

Check any frame yourself: `python calibrate_player_bar.py frame.png`
prints both detectors (V2 first) and draws V2's cards on the annotated
image (green = alive, red = dead, orange line = band row).

## 2. The keys (V1 only)

| key | default | meaning |
|---|---|---|
| `player_bar_region` | *(none — required)* | `[x0, y0, x1, y1]` of the whole card strip, at 1920×1080. Must start at the left edge of the FIRST card (the glitch card) and end at the right edge of the last one. |
| `player_separator_threshold` | 25 | a column whose brightest channel is below this, at three rows in the top third of the bar, counts as a separator between cards |
| `player_portrait_y_in_bar` | 35 | rows below `y0` where the portrait pixel is sampled |
| `player_saturation_threshold` | 40 | HSV saturation above which that pixel means alive; eliminated portraits are greyscale |
| `player_name_y_in_bar` | 62 | rows below `y0` where the nameplate text strip starts |
| `player_name_h` | 14 | height of that strip (upscaled 4× and OCR'd with tesseract, psm 7) |

Plus, optional: `kill_notification_region` `[x, y, w, h]` — the kill-feed text
OCR'd once at first blood to name the killer.

The bot expects 4–12 slots including the always-present leftmost glitch card
(spinner, no name) which it drops; a real lobby therefore reads as 11 slots.

## 3. How to calibrate — offline, any machine (preferred)

1. Get ONE screenshot of a match with the card strip visible, at the game's
   native resolution (1920×1080, borderless windowed, 100 % Windows scaling).
   A second frame with at least one eliminated player is ideal. Press F8 in
   `calibrate.py` during a match (section 4) or use any screenshot tool — the
   stream frame is NOT usable, it is scaled.
2. Run the analyzer with a first guess at the region:

   ```
   python calibrate_player_bar.py frame.png --bar 400 20 1520 120 --sweep
   ```

   It prints, per stage: the separator count and implied slot count for the
   region, a threshold sweep (`25→11` means threshold 25 gives 11 slots —
   that is the one you want), each slot's portrait saturation and the
   alive/dead verdict, the OCR'd name per slot, and a PROBLEMS list in plain
   words. It writes `frame.annotated.png`: yellow box = region, magenta lines
   = separators, green/red dots = portrait sample alive/dead, blue boxes =
   name strips. Open it and check the dots are on portraits and the boxes are
   on nameplates; adjust `--portrait-y`, `--name-y`, `--name-h` until they are.
3. When it says `no problems found`, paste the printed snippet into
   config.json and restart the bot. Exit code 0 = clean, 1 = problems listed.
4. Send lo the frame(s) if you want a second pair of eyes — the analyzer runs
   on Linux with no game, so the same command gives the same answer there.

## 4. How to calibrate — live (F8 in calibrate.py)

Run `python calibrate.py` in a second terminal, start a match, and press F8
once the card strip is on screen. It saves `calibration_screenshots/player_bar_*.png`,
runs the same analysis with config.json's current keys, prints the report,
writes the annotated image, and folds the keys into the F6 config snippet.
No terminal input is needed at F8, so it works while the game has focus.

## 5. Verify it end to end

- Bot log, start of a match: `Player bar: 11 slots detected (10 players + 1 glitch)`
  then `Player bar snapshot — 10 players:` with a name per slot. `(unread)`
  on a slot = OCR miss for that nameplate.
- Ladder, after restart: darwinstalker.com/admin/observability, kind prefix
  `bot.` — a `bot.warning` mentioning the player bar means step 1 is failing.
- Ladder LIVE tab during a game: the game block shows `N / 10 alive`,
  eliminations in the feed, and eliminated players struck through in the
  scorecard. No block at all = no events reached the ladder (see
  DS_LIFECYCLE_HANDOFF.md); a block with `0 / 0 alive` = `match_start` had no
  slots, i.e. this document's problem.

## 6. Known limits (worth fixing next)

- V2 assumes 1920×1080 with the strip centred at x=960 and a 131.6px pitch
  (`player_cards_center_x` / `player_cards_pitch` in config if the game
  changes). It reads the LOBBY snapshot at match start like V1 does; on a
  menu screen it can report a phantom 2-card row, which is why it only runs
  where V1 ran.
- V2 name OCR crops the band; unverified here (no tesseract on the Linux
  box). The ladder seeds names from the Discord roster regardless.

- Alive/dead is ONE pixel of the portrait. CLAUDE.md's own "Player-Targeted
  Cards" notes say the reliable signal is the health bar at the bottom of the
  card. Switching `sample_player_alive` to a small patch on the health bar
  with majority voting (the same lesson recorded for the director-points pips)
  would remove most flicker. The analyzer's per-slot saturation column shows
  how close to the threshold each portrait sits; anything within ~8 flickers.
- Names come from nameplate OCR once, at match start. The ladder now seeds
  names from the Discord signup roster and folds OCR reads by identity, so a
  missed nameplate only affects how an elimination line is labelled.
- Everything assumes 1920×1080. A different resolution or UI scale moves
  every offset.

## 7. Files

- `game/player_bar_calibration.py` — the analysis (uses the bot's own
  `game/screen_detection.py` and `game/ocr.py` functions; never screenshots).
- `calibrate_player_bar.py` — the offline CLI.
- `calibrate.py` — F8 live hotkey.
- `tests/test_player_bar_calibration.py` — synthetic-frame tests of the
  pipeline (run with `pytest -q`; needs opencv + numpy, which
  requirements-dev.txt now lists).

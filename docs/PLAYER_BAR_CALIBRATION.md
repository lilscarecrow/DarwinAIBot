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
- V2 name OCR, measured on the 48 name bands in the fixtures with the SAME
  Windows tesseract 5.4 binary the bot uses (`C:\Program Files\Tesseract-OCR`):
  35/48 exact. The misses are the `:]` handle (a symbol, 5 crops), `SlyK`
  read as `SlvK` (5 crops — the ladder's glyph fold now maps y→v, so it
  resolves to SlyK), and three one-glyph slips. The preprocessing that won
  (`ocr_prepare`: per-pixel min channel, inverted, 4x, padded, psm 7) beat
  V1's grey+Otsu by 5 names; `tests/test_player_cards_v2.py::test_name_ocr_reads_most_bands_exactly`
  re-measures it wherever a tesseract is reachable (`TESSERACT_CMD=...`).
  The ladder seeds names from the Discord roster regardless, so OCR only
  labels elimination lines for unlinked players.

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

## 8. Slot-number badge (2026-09-09) — confirmed, both open questions answered

Each card's small top-right number badge **is** the same digit `/pov`'s
hotkey (1-9, 0) switches camera to for that player, and it **is stable for
the whole match** — confirmed directly (scarecrow, 2026-09-09), no live test
needed. When a player is eliminated, their card stays in the same slot with
the same badge; only a visual X overlay is added — nothing about their
position or number changes. So the badge is a genuine, match-long identity
key, immune to the pivot that raw card position isn't.

**What does move:** confirmed — the whole card block's *vertical* position on
screen is fixed regardless of player count, but its *horizontal* layout
(center-x block width, so each card's x) depends on how many players are in
the lobby, "just like the deck slots" pivoting with fewer cards. This is
exactly V2's existing model (`positions(n)` around a fixed `player_cards_center_x`
with a fixed `player_cards_pitch`) — no redesign needed there, it already
matches how the game actually lays the strip out. Because eliminated players'
cards never move or disappear, this positioning only needs to be resolved
once, at match start, for the whole match — not re-detected every poll.

**Practical upshot:** a `{slot_number: canonical_name}` map built once at
match start (see below) stays valid for the rest of the match. Elimination
tracking can key off that slot number rather than raw position, and doesn't
need to worry about the strip ever reshuffling mid-match.

**Badge OCR was tried and abandoned — the slot number doesn't need OCR at
all (2026-09-09).** The digit is small enough at native 1920×1080 that an
extensive preprocessing sweep against a real 6-card lobby screenshot — Otsu,
several fixed thresholds, inverted grayscale, the min-channel trick that
works well for names, five PSM modes, several crop tightnesses — topped out
around 2-4 of 6 correct, with several cards read as the wrong digit rather
than failing cleanly. What replaced it: `game/player_cards_v2.py::slot_number_for_index(index)`
— a card's left-to-right `index` maps straight to its `/pov` hotkey as
`"1".."9","0"`, no screen reading involved. This works because both
2026-09-09 confirmations above hold: the badges in that same screenshot read
`1,2,3,4,5,6` in exact left-to-right order matching `detect_cards()`'s index,
and `/pov`'s own key mapping already treats `"0"` as the 10th slot — so the
sequence is always `1..9,0`. `badge_crop()`/`ocr_badge_number()` are kept in
the module as a secondary cross-check in the diagnostic log (see below) —
useful to glance at, never to be trusted as the source of truth.

**What's built so far, still not wired into real match logic** — nothing
here feeds `_player_slot_xs`/`_player_alive`/`_player_names` or any of the
V1/V2 state the match actually uses:
`MatchRunner._log_slot_map_snapshot()` (called right at the B press, or at
the top of the auto-start branch when `_skip_start` is set — i.e. as close
to match start as the existing flow allows) runs V2's `detect_cards()` +
`ocr_names()`, derives each card's slot via `slot_number_for_index()`, logs
one line per card (`slot / index / x / name / alive`, plus the untrusted
`badge_ocr` reading for comparison), and — as of 2026-09-09 — also pushes a
`slot_map` live event via `self._emit(...)` (`n`, `slots`, `names`, `xs`,
`badge_ocr`), so the same data shows up on darwinstalker.com next to
`match_start`/`eliminated` rather than only in the local log file (the
ordinary `logger.info(...)` line stays local either way — `DsLogHandler`
only forwards INFO from `game.ds_lifecycle`/`game.ingest`, see
docs/DS_LIFECYCLE_HANDOFF.md §3a). Wrapped in try/except, never raises. The
one thing this doesn't cover yet is confirming name OCR accuracy — the part
still genuinely reliant on OCR — against a real match's logs.

**Runs on a background thread, not inline (2026-09-09 fix, found live):**
`_log_slot_map_snapshot()` does up to two tesseract calls per card (name +
badge OCR), which was blocking the match thread synchronously right at match
start and delaying the B-press/card-timer sequence by several seconds on a
real lobby — the same class of bug as the `on_match_start` delay fixed
2026-09-07, just introduced fresh by this feature. `_fire_slot_map_snapshot()`
(what `run()` actually calls at both call sites now) spawns a daemon thread
and returns immediately instead. One residual gap, low-stakes given this is
still diagnostic-only: `SessionState._pov_locked` (see `/pov`'s CLAUDE.md
entry) is released a fixed ~10-12s after match start regardless of whether
the background snapshot has finished by then, so on a slow read there's a
small window where a POV switch could still corrupt it — not closed, since
the worst case is a bad diagnostic read, not a match-logic bug.

**"Fully captured" flag (2026-09-09):** `captured = bool(cards) and all(bool(nm) for nm in names)`
— every card `detect_cards()` found this match got *some* name (ladder-linked
or raw OCR, either counts, per the unlinked note above), an N/N read with no
partial misses. Self-referential to what was actually detected on screen,
not cross-checked against `roster_size` (that's only a Discord-signup
estimate of who's supposed to be here, not proof of who actually is).
Written to `MatchRunner._lobby_captured`/`_slot_map` from the snapshot's own
background thread (plain attribute assignment, no lock, same as this
class's other cross-thread flags) and exposed via `is_lobby_captured()` /
`slot_map()` (`{"/pov" digit: name}`, only non-empty once captured) — the
gate any future feature needing a trustworthy full slot map should check,
rather than assuming a name exists for every slot. Also rides on the
`slot_map` event as `captured` (bool). Not wired into anything yet — this is
the flag itself, not a consumer of it.

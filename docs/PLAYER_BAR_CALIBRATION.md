# Player bar calibration — handoff

For scarecrow and for any Claude session working in this repo. Read this when
"alive/dead tracking doesn't work", "OCR is bugging", names come out as
`slot 3`, or the ladder's LIVE feed shows no eliminations.

## 1. What is going on

Everything the bot knows about players during a match comes from ONE strip of
pixels: the HUD card bar across the top of the screen. `game/player_cards_v2.py`
reads it with a geometry detector — no config keys, no per-machine
calibration, works at 1920×1080 out of the box:

- Card centres for a candidate count n are `960 + (i − (n−1)/2) × 131.6`
  (`player_cards_center_x` / `player_cards_pitch` in config if the game's
  layout ever changes) — each candidate is checked against the one thing
  every card has: the name band (solid bright colour = alive) or a red X
  over the portrait (= eliminated). The n whose positions ALL read as cards
  wins — always the largest such n (2026-09-10 fix, found live: a lobby's
  "expected" player count used to override this when a smaller n also
  spuriously fit, silently dropping a real 10th player's card for the whole
  match after a stale scrim-signup count of 9 got passed in — see
  `detect_cards()`'s docstring in `game/player_cards_v2.py`).
- Names come from OCR on that same band (`ocr_names()` — see §6 for its
  accuracy and the psm 7/8 fallback fixed 2026-09-09, §9).
- Alive/dead is re-sampled every poll (`cards_alive()`) at the x-positions
  found once at match start.

`MatchRunner._init_player_bar()` runs this once, right before the match
countdown, and `_poll_player_bar()` re-samples alive/dead throughout the
match (every `screen_poll_interval_seconds`, or sooner around a card
trigger) — any alive→dead flip becomes one `eliminated` live event (plus
one `first_blood` the first time anyone dies), pushed to the ladder via
`DraftLifecycle.event()`.

**An older separator-scan detector was removed 2026-09-09.** It looked for
dark separator columns inside a fixed `player_bar_region` config box (six
keys total: `player_bar_detector`, `player_bar_region`,
`player_separator_threshold`, `player_portrait_y_in_bar`,
`player_saturation_threshold`, `player_name_y_in_bar`, `player_name_h`).
Running it on real 1920×1080 VOD frames (2026-09-06) showed it could never
work even with a perfect region — it found 23-61 "separators" per frame
(the translucent HUD lets the scenery through), and the strip is not a
fixed box anyway: it is centred on the screen and its width follows the
player count. `player_bar_region` was never calibrated on any deployed
machine, so this detector had been silently dead code (`player_bar_region`
absent → "no slots detected — player tracking disabled" logged every
single match) since it was added — removed rather than fixed once the
geometry detector below proved out as a full replacement, not just a
comparison mode.

## 2. Sanity-check a frame

`python calibrate_player_bar.py frame.png` runs the exact same detection
the bot uses on a saved screenshot (any machine, no game or display
needed) and writes an annotated image (green box = alive, red box = dead,
orange line = the band row it matched on). Add `--expected N` if you know
the lobby's player count — as of 2026-09-10 this only flags a mismatch in
the report, it can no longer change which count gets picked (see above).
`python calibrate.py`'s F8 hotkey does the same thing live, mid-match, no
terminal input needed.

## 3. Verify it end to end

- Bot log, start of a match: `Player bar snapshot — 10 players:` with a
  name per slot. `(unread)` on a slot = OCR miss for that nameplate (the
  ladder still gets an elimination line for it, just labelled by slot
  number instead of name — see §6).
- Ladder, after restart: darwinstalker.com/admin/observability, kind
  prefix `bot.` — a `bot.warning` mentioning the player bar means
  detection failed to find any cards that match.
- Ladder LIVE tab during a game: the game block shows `N / 10 alive`,
  eliminations in the feed, and eliminated players struck through in the
  scorecard. No block at all = no events reached the ladder (see
  DS_LIFECYCLE_HANDOFF.md); a block with `0 / 0 alive` = `match_start` had
  no slots, i.e. detection found nothing this match.

## 4. Known limits (worth fixing next)

- Assumes 1920×1080 with the strip centred at x=960 and a 131.6px pitch
  (`player_cards_center_x` / `player_cards_pitch` in config if the game
  changes). It reads the LOBBY snapshot at match start; a menu screen (no
  real card strip) can report a phantom low-n row, which is why it only
  ever runs from `_init_player_bar()`/`_log_slot_map_snapshot()`, both
  called at match start, never speculatively.
- Name OCR, measured on the 48 name bands in `tests/fixtures/player_bar/`
  with the same Windows tesseract 5.4 binary the bot uses
  (`C:\Program Files\Tesseract-OCR`): 35/48 exact. The misses are the `:]`
  handle (a symbol, 5 crops), `SlyK` read as `SlvK` (5 crops — the
  ladder's glyph fold now maps y→v, so it resolves to SlyK), and three
  one-glyph slips. `tests/test_player_cards_v2.py::test_name_ocr_reads_most_bands_exactly`
  re-measures it wherever a tesseract is reachable (`TESSERACT_CMD=...`).
  The ladder seeds names from the Discord roster regardless, so OCR only
  labels elimination lines for unlinked players.
- Names come from nameplate OCR once, at match start. The ladder now seeds
  names from the Discord signup roster and folds OCR reads by identity, so
  a missed nameplate only affects how an elimination line is labelled.

## 5. Files

- `game/player_cards_v2.py` — the detector itself (`detect_cards`,
  `ocr_names`, `cards_alive`, `slot_number_for_index`).
- `game/player_bar_calibration.py` — the offline analysis wrapper
  (`analyze_v2`/`annotate_v2`/`format_report_v2`), used by both tools below.
- `calibrate_player_bar.py` — the offline CLI.
- `calibrate.py` — F8 live hotkey.
- `tests/test_player_cards_v2.py` — detection + name-OCR tests against real
  VOD frame fixtures (`tests/fixtures/player_bar/`).

## 6. Slot-number badge (2026-09-09) — confirmed, both open questions answered

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
exactly the detector's existing model (`positions(n)` around a fixed
`player_cards_center_x` with a fixed `player_cards_pitch`) — no redesign
needed there, it already matches how the game actually lays the strip out.
Because eliminated players' cards never move or disappear, this positioning
only needs to be resolved once, at match start, for the whole match — not
re-detected every poll.

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

**What's built so far — diagnostic, separate from the match-driving path
above:** `MatchRunner._log_slot_map_snapshot()` (called right at the B
press, or at the top of the auto-start branch when `_skip_start` is set —
i.e. as close to match start as the existing flow allows) runs its own
`detect_cards()` + `ocr_names()` pass, derives each card's slot via
`slot_number_for_index()`, logs one line per card (`slot / index / x /
name / alive`, plus the untrusted `badge_ocr` reading for comparison), and
pushes a `slot_map` live event via `self._emit(...)` (`n`, `slots`,
`names`, `xs`, `badge_ocr`), so the same data shows up on
darwinstalker.com next to `match_start`/`eliminated` rather than only in
the local log file (the ordinary `logger.info(...)` line stays local
either way — `DsLogHandler` only forwards INFO from
`game.ds_lifecycle`/`game.ingest`, see docs/DS_LIFECYCLE_HANDOFF.md §3a).
Wrapped in try/except, never raises. Runs independently of (and in
addition to) `_init_player_bar()`'s own detection above — a second,
separately-invoked read purely for this diagnostic map and, since
2026-09-09, the Twitch prediction feature (see CLAUDE.md's "Twitch 'Who
Wins?' Prediction" section).

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
`slot_map` event as `captured` (bool).

## 7. Two live findings from a partial-capture match (2026-09-09) — one was a red herring

A live match logged `slot_map` with 2 of 10 names unread (slots for "M" and
"Guts", x=1026/1157), skipping the Twitch prediction feature's lobby-capture
gate. Investigating with the actual screenshot (not just the log) turned up
two separate things, only one of which needed a fix.

**Red herring: an 11th "duplicate card" glitch.** The screenshot genuinely
showed a player's card ("Hellcrying") rendered twice — once alive, once
"ELIMINATED" — 11 cards total instead of the usual 10. First instinct was
that this was corrupting `detect_cards()`'s geometry (assumed n-card spacing
misaligning once an 11th card is on screen). Measuring the real on-screen
card centers disproved that: the 10 genuine player cards sit at *exactly*
the same fixed 10-card grid as always (`positions(10, cfg)`, center_x=960,
pitch=131.6 — dense-scanning the raw pixels confirms every one of the 10
real cards to within ~2px of that formula). The duplicate card sits exactly
one more pitch-width to the *left* of slot 0 (~x=236) — precisely the
"always-present glitch card sits LEFT of the centred block, never a
candidate" position this module's docstring already described for the
older blank/no-name version of this glitch. `detect_cards()` never samples
that x at n=10, so the duplicate was never actually read, named, or counted
— it was already harmless before this investigation, just newly confirmed
to sometimes be a full duplicate (name + health bar) rather than blank. No
code change was needed for this part; a `player_cards_max: 11` bump and a
`dedupe_cards()` pass were drafted on the wrong theory and not kept.

**Real cause: `--psm 7` drops short single-word names.** Cropping and OCRing
the "M" and "Guts" slots directly (bypassing `ocr_names()`) showed the name
band itself was perfectly legible — `--psm 8` (single word) read `Guts`
exactly and `OM` for `M` (one stray glyph, still fuzzy-matchable) — but
`--psm 7` (single line), what `ocr_names()` actually calls, returned `""`
for both. Every other name on the same frame (`Phantom Pain`, `SteffKnight`,
`TheAuri`, ...) read fine under psm 7. The common factor for the two misses
is that they're the shortest names on the card — a 1-4 character word
centered in a fixed-width crop that's mostly blank padding, which psm 7's
line-segmentation apparently discards as noise rather than treating as a
text line. Longer names fill enough of the crop width to read normally.

**Fix:** `ocr_names()` now retries with `--psm 8` only when `--psm 7` comes
back empty — never overrides a real psm 7 read, so it can only recover a
miss, not introduce a regression. Confirmed against the real screenshot:
both slots now resolve (`OM` → fuzzy-matches `M` via the ladder roster fold,
`Guts` exact).

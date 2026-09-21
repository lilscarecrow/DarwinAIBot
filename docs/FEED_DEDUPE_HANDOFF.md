# Kill feed: one event per death, and environmental deaths (2026-09-20)

For the bot operator and any agent working here. Short version: **pull and restart when
convenient. No config change. Nothing breaks if you wait**, because darwinstalker.com already
cleans up what the old bot sends.

## Why

darwinstalker.com now turns `feed_kill` / `feed_first_blood` events into public player stats
(a KILL FEED card on each profile, a kill log under each set). Auditing the stored events on
prod showed two bot-side problems:

1. **Every kill arrived 2 to 4 times.** A feed line is on screen for several polls and OCRs a
   little differently each time (`BY AXE`, `BY AXE,`, `. TWO KILLED ...`). The dedupe keyed on
   the exact text, so it rarely caught its own repeat. 222 of 867 resolved kills were repeats;
   first blood averaged 1.7 events per game (up to 4).
2. **`X KILLED BY COLD` was sent as a kill by X.** The regex splits on `KILLED`, so with no
   second name it captured `victim_raw = "BY COLD"`. 42 rows: players credited with a kill for
   freezing, burning, quitting or disconnecting.

The ladder now handles both server-side for every event already stored, so this change is about
sending clean data from here on, not about fixing history.

## What changed (`game/match_runner.py`, `_poll_damage_feed_worker`)

- **Once-per-match keys.** A player dies once per match and first blood is drawn once, so the
  worker records `once|died|<victim_slot>` and `once|feed_first_blood` and skips later reads of
  the same death. Recorded **only for a complete read** (victim slot resolved, plus a killer
  slot or a cause; for first blood, killer slot resolved). A garbled first frame therefore never
  blocks the good read after it, and never blocks the Give Wood reward. Incomplete reads still
  use the old text key.
- **`_split_by_cause()`** reads a `BY <cause>` tail in the victim position:
  - weapon (`AXE`, `ARROW`, `HEADSHOT`, also fused `BYARROW`): a real kill whose victim name the
    OCR lost. `killer_*` kept, no `victim_*`, the tail becomes `method`.
  - anything else (`COLD`, `LAVA`, `QUITTING`, `DISCO`...): X died to the environment. Sent as
    `victim_raw` / `victim_slot` + `cause`, with **no `killer_*` fields**.
  - a name that just starts with "By" (no space, not a weapon) is left alone.

## Contract with darwinstalker.com

| Line | Fields sent |
|------|-------------|
| `A KILLED B BY AXE` | `killer_raw`, `killer_slot`, `victim_raw`, `victim_slot`, `method`, `text` (unchanged) |
| `A KILLED BY ARROW` | `killer_raw`, `killer_slot`, `method`, `text` |
| `A KILLED BY COLD` | `victim_raw`, `victim_slot`, `cause`, `text` (NEW shape; kind is still `feed_kill`) |

The server reads both the new `cause` shape and the old `victim_raw = "BY COLD"` shape, so old
and new bots can coexist.

**`killer_slot` / `victim_slot` now matter.** The server uses them as a hint when its own name
matching is unsure: they let it accept a slightly worse OCR read of that player (`WICK` → nick,
33 rows recovered) and settle ties (`WISE AMAZZARU` with both Wise and Amazzaru in the lobby).
They never overrule a clearly better match, so a wrong slot costs nothing, but a right one now
adds resolutions. They must stay a **0-based index into the `slot_map` event's `names` list for
that game**, as a string. If that numbering ever changes, tell the ladder side first.

## How to verify after a restart

- Bot log: each kill shows one `Feed match [feed_kill]` line, not several a few seconds apart.
  An environmental death logs fields with `cause` and no `killer_raw`.
- Ladder: open the set at darwinstalker.com/sets after it is published and expand **Kill log**.
  Deaths to the environment read "X died to cold".
- `python -m pytest tests/test_match_runner_feed.py` (the two real-capture tests need tesseract).

## Known gaps, not addressed here

- A lobby whose scorecards are entered by hand later (tournament mode) streams every game into
  one `game_index`, because the index only advances on `post_results`. The server copes by
  treating each `match_start` as a new game for dedupe, but the kill log still groups them
  under one "G" heading. Advancing the local counter at `match_end` when no upload happens would
  fix it at the source.

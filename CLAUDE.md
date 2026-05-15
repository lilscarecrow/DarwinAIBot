# CLAUDE.md — DarwinDirector Bot

This file provides guidance to Claude Code when working in this repository.

## Project Overview

DarwinDirector is an automated Show Director bot for **Darwin Project**. It manages game sessions via Discord slash commands, automates Director card plays on a timer, handles zone closing logic, and logs match results.

The full design specification is in **`DarwinProjectBotPlan.docx`** (on the Desktop of the machine this was designed on — `C:\Users\brent\Desktop\DarwinProjectBotPlan.docx`). That document is the authoritative reference for match flow, zone logic, Discord commands, config structure, and future enhancements. Read it before making architectural decisions.

## Tech Stack

| Component | Tool |
|---|---|
| Discord Bot | discord.py 2.x (slash commands via app_commands) |
| Input Automation | PyAutoGUI |
| Screenshot & Template Matching | OpenCV (cv2) |
| OCR | Tesseract + pytesseract |
| Clipboard | pyperclip |
| Process Management | psutil |
| Image Processing | Pillow, numpy |

Install dependencies: `pip install -r requirements.txt`

## Running the Bot

```bash
python main.py
```

`main.py` validates `config.json` on startup and exits with clear error messages if anything is missing or invalid. The bot will not connect to Discord until config passes validation.

## Project Structure

```
DarwinAIBot/
├── main.py                     # Entry point + config validation
├── config.json                 # Runtime config (secrets + calibration data)
├── requirements.txt
├── CLAUDE.md
├── bot/
│   └── discord_bot.py          # DarwinBot + DirectorCog (all 6 slash commands)
├── game/
│   ├── launcher.py             # Launch game process, monitor for crashes
│   ├── screen_detection.py     # OpenCV template matching, pixel sampling, screenshots
│   ├── card_actions.py         # Shift-drag card plays, key presses, bypass mode
│   ├── ocr.py                  # Tesseract results parsing, Discord formatter
│   └── match_runner.py         # Full match loop (card timers, zone closes, end detection)
├── session/
│   └── state.py                # BotState enum + SessionState machine
├── zones/
│   ├── zone_logic.py           # Adjacency map, BFS connectivity, can_close_zone()
│   ├── base_strategy.py        # Abstract base class for zone strategies
│   ├── strategy_factory.py     # Factory + strategy registry
│   └── strategies/
│       ├── outer_first.py      # Always close fewest-neighbor zone first
│       ├── random_zone.py      # Random valid zone
│       └── weighted_outer.py   # Prefer outer zones, occasional variation (default)
├── logs/                       # Runtime log (darwin_bot.log, appended across sessions)
├── screenshots/errors/         # Auto-saved on any automation failure
└── templates/                  # OpenCV template images (captured from game, not in repo)
```

## Code Architecture

### Session State Machine (`session/state.py`)

States: `IDLE → LAUNCHING → IN_MENU → IN_CUSTOM → MATCH_IN_PROGRESS → MATCH_ENDED → IDLE`

`SessionState` tracks current state, last/next action labels, and match timer. Transitions are logged. `is_command_valid(command)` enforces which Discord commands are allowed in each state. `reset()` returns to IDLE and clears all state.

### Discord Bot (`bot/discord_bot.py`)

`DarwinBot(commands.Bot)` holds config and session. All commands live in `DirectorCog(commands.Cog)`.

**Guards on every command:**
1. `_role_check` — silent ignore if user lacks `discord_required_role` (per spec)
2. `_state_check` — ephemeral error if command invalid in current state
3. `_lock_check` — ephemeral error if another long-running operation is active

**The asyncio lock (`_session_lock`)** wraps all `run_in_executor` calls to prevent concurrent operations. `/end` bypasses this lock intentionally — it calls `_active_runner.stop()` then closes the game.

**Long-running commands** (`/launch`, `/custom`, `/start`) use `run_in_executor` to run blocking game automation in a thread without blocking the Discord event loop.

**Commands:**
| Command | Valid States | Description |
|---|---|---|
| `/launch` | IDLE | Launch game, poll for menu screen |
| `/deck [cards]` | IN_MENU | Check/swap Director deck |
| `/custom` | IN_MENU | Create private match, return lobby code |
| `/start` | IN_CUSTOM | Start match, responds with results when done |
| `/status` | Any | Current state + last/next action |
| `/end [confirm]` | Any | Force close game; requires `confirm:True` if match in progress |

### Match Runner (`game/match_runner.py`)

`MatchRunner.run()` is the full match sequence, called via `run_in_executor` from `/start`:

1. Pre-match deck check (stub — pending calibration)
2. Press B to start match
3. 5-second sync delay
4. Main loop:
   - Fire card events when elapsed time reaches trigger (Electromania @ 2:00, Beach Party @ 4:00)
   - Every 30s: sample zone pixels → `valid_closeable_zones()` → strategy selects zone → play close card
   - Every `screen_poll_interval_seconds`: template-match placement badge for match end
5. OCR results screen → format → return to Discord

`MatchRunner.stop()` sets a `threading.Event` that the loop checks between every action. Called by `/end`.

### Zone Logic (`zones/`)

7-zone hex grid with fixed adjacency. `can_close_zone()` enforces three rules:
- Zone must be OPEN
- Cannot close the last open zone
- Remaining open zones must stay connected (BFS validation)

Zone strategy is pluggable via `config.json → zone_selection_strategy`. Adding a new strategy: create a file in `zones/strategies/`, subclass `BaseZoneStrategy`, add to `STRATEGIES` dict in `strategy_factory.py`.

### Screen Detection (`game/screen_detection.py`)

- `find_template()` — OpenCV normalized cross-correlation, returns center coords or None
- `wait_for_template()` — polling wrapper with timeout
- `poll_for_match_end()` — single-shot check for placement badge
- `sample_pixel_color()` — reads one pixel (R,G,B) for zone state detection
- `save_error_screenshot()` — auto-saves to `screenshots/errors/` with timestamp + label

### Card Actions (`game/card_actions.py`)

`play_card()` shift-drags from a slot coordinate to a target coordinate, then optionally verifies the card left its slot via template match. All actions respect `bypass_mode` — when enabled, logs the action and waits for Enter instead of sending input to the game.

## Config Reference (`config.json`)

```json
{
    "game_executable_path": "",          // Full path to DarwinProject.exe
    "discord_bot_token": "",             // Discord bot token (keep secret)
    "discord_required_role": "DarwinBotAdmin",
    "zone_selection_strategy": "weighted_outer",
    "ahk_bypass_mode": false,            // true = log actions instead of executing

    "card_slots": {                      // Screen coordinates for each card slot (0-9)
        "1": null, "2": null, ...        // e.g. [960, 850] — calibrate in-game
    },
    "cards": {
        "electromania": {
            "slot": null,                // Which card_slot key this card is in
            "play_time_seconds": 120,    // 2:00
            "drop_target": null          // [x, y] to drag to — calibrate in-game
        },
        "beach_party": {
            "slot": null,
            "play_time_seconds": 240,    // 4:00
            "drop_target": null
        }
    },
    "zone_close_card_slot": null,        // [x, y] of the Close Zone card — calibrate
    "zone_sample_coordinates": {         // [x, y] pixel to sample per zone — calibrate
        "1": null, ..., "7": null
    },
    "zone_drop_coordinates": {           // [x, y] drag target per zone — calibrate
        "1": null, ..., "7": null
    },
    "zone_color_thresholds": {           // RGB tuples for open/closing/closed — calibrate
        "open": null,
        "closing": null,
        "closed": null
    },
    "results_ocr_regions": null,         // Per-column (x,y,w,h) lists — calibrate
    "screen_poll_interval_seconds": 12,
    "launch_timeout_seconds": 60
}
```

**config.json is gitignored** (contains the bot token). Set it up manually on each machine.

## Templates Directory

OpenCV template images live in `templates/` (not in the repo — capture from the game):
- `templates/play_button.png` — orange PLAY button on main menu (used by `/launch`)
- `templates/placement_badge.png` — colored placement badge on results screen (match end detection)

Capture these as cropped screenshots of the exact UI element at your game's native resolution.

## Calibration Checklist

Before the bot can run a real match, these must be filled in `config.json`:
- [ ] `game_executable_path`
- [ ] `discord_bot_token` + role created in server
- [ ] `card_slots` coordinates for Electromania and Beach Party slots
- [ ] `cards.electromania.slot` and `cards.beach_party.slot` (which slot key)
- [ ] `cards.electromania.drop_target` and `cards.beach_party.drop_target`
- [ ] `zone_close_card_slot`
- [ ] `zone_sample_coordinates` (all 7 zones)
- [ ] `zone_drop_coordinates` (all 7 zones)
- [ ] `zone_color_thresholds` (open / closing / closed RGB values)
- [ ] `results_ocr_regions` (x,y,w,h per column per row)
- [ ] `templates/play_button.png` captured
- [ ] `templates/placement_badge.png` captured

## Testing Requirements

Custom matches in Darwin Project require **Director + minimum 2 players** to start. Cannot start a match with fewer.

**Test phases (in order):**
1. No game needed — zone logic, state machine, Discord commands, bypass mode all work now
2. Director client only — menu navigation, deck check, custom match creation, lobby code capture
3. Director + 2 players on separate machines — first full live match test

**EAC + VM note:** EAC actively detects and blocks virtualized environments. Running player clients in VMs on the same machine will not work. Players must be on separate physical machines.

## Pending Implementation (TODOs)

These stubs exist in the code but need in-game calibration or implementation:

- `game/match_runner.py` — `_pre_match_deck_check()`: navigate deck tab, screenshot, compare card list
- `bot/discord_bot.py` — `_do_launch()`: replace template path placeholder with real captured template
- `bot/discord_bot.py` — `_do_deck_check()`: UI navigation to deck tab
- `bot/discord_bot.py` — `_do_create_custom()`: click Play → Custom → Create New → Solo Classic → Start → Director, capture lobby code from clipboard

## Future Enhancements (from plan)

- Director points bar monitoring before card plays
- Variable zone closing timing
- Lobby screenshot polling for automatic player count detection
- Additional zone selection strategies
- Card deck modification via `/deck` parameter
- Zone coordinate calibration utility
- Multi-resolution support

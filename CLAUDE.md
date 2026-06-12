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
│   └── discord_bot.py          # DarwinBot + DirectorCog (all slash commands)
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
| `/custom <region>` | IN_MENU | Set region (NA/EU), create private match, return lobby code |
| `/start` | IN_CUSTOM | Start match, responds with results when done |
| `/menu` | IN_CUSTOM | Navigate back to main menu from any screen in the custom flow |
| `/status` | Any | Current state + last/next action |
| `/end [confirm]` | Any | Force close game; requires `confirm:True` if match in progress |

All responses use `discord.Embed` with color-coded status (green=ok, red=fail, blue=active, orange=in-match, gray=neutral). State is shown in every embed footer.

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

- `find_template()` — OpenCV normalized cross-correlation, threshold 0.8, returns center coords or None
- `wait_for_template_center()` — polling wrapper with timeout, returns center or None
- `detect_current_screen()` — checks `_SCREEN_SIGNATURES` in order, returns first match name or None
- `poll_for_match_end()` — single-shot check for placement badge
- `sample_pixel_color()` — reads one pixel (R,G,B) for zone state detection
- `save_error_screenshot()` — auto-saves to `screenshots/errors/` with timestamp + label

**Known screens** (checked in priority order in `_SCREEN_SIGNATURES`):

| Screen name | Signature template | Notes |
|---|---|---|
| `director_lobby` | `lobby_password_label.png` | Director waiting lobby with password |
| `director_splash` | `latest_updates_continue.png` | Splash/news screen after launch |
| `choose_role` | `choose_role_screen.png` | INMATE / DIRECTOR role selection |
| `create_match` | `solo_classic_label.png` | Create custom match settings screen |
| `custom_browser` | `create_custom_match.png` | Custom match browser |
| `region_popup` | `region_popup_header.png` | "CHOOSE YOUR REGION" modal popup |
| `play_screen` | `play_screen_region.png` | PLAY mode-selection screen with region button |
| `main_menu` | `play_button.png` | Main menu with PLAY / CUSTOM / TRAINING |

### Card Actions (`game/card_actions.py`)

`play_card()` shift-drags from a slot coordinate to a target coordinate, then optionally verifies the card left its slot via template match. All actions respect `bypass_mode` — when enabled, logs the action and waits for Enter instead of sending input to the game.

## Custom Lobby Creation Flow (`_do_create_custom`)

Complete sequence triggered by `/custom <region>` from `IN_MENU` state. All coordinates are for **1920×1080**.

```
Step 0 — Region setup
  hover_click(355, 258)                          PLAY button on main menu
  wait: play_screen_region.png                   confirms PLAY screen loaded
  check region_na.png / region_eu.png            detect current region
  if wrong:
    hover_click(215, 1045)                       CHANGE REGION button (bottom-left)
    wait: region_popup_header.png
    moveTo(960, 700)                             move away — game highlights active row white
    click_until(740,468 or 720,511)              NA row / EU row (hardcoded, not template)
    verify: play_screen_region.png               popup closed, back on PLAY screen
  click_until(1840, 1044)                        BACK → main menu
  verify: play_button.png

Step 1 — Enter custom flow
  click(226, 333)                                CUSTOM button

Step 2 — Match browser
  wait + click: create_custom_match.png          CREATE NEW CUSTOM MATCH button

Step 3 — Create Match screen gate
  wait: solo_classic_label.png → center_sc       SOLO CLASSIC always visible here (privacy-agnostic)

Step 4 — Privacy check
  find: privacy_private.png                      if not found, click(90, 119) to toggle

Step 5+6 — Mode + START (with retry)
  click_until(*center_sc, start_button.png)      click SOLO CLASSIC, verify lit START appears
  → center_start

Step 6+7 — START → Choose Role (with retry)
  click_until(*center_start, choose_role_screen.png)
  → role_center

Step 8 — Director role
  click(role_center[0], role_center[1] - 175)   card body is ~175px above label center

Step 9 — Lobby
  wait: lobby_password_label.png (timeout=80s)  "SEARCHING FOR GAME" transition is normal
  sleep(10.0)                                    wait for game lag before clipboard
  click(center[0]+316, center[1]-9)             clipboard icon offset from label center
  pyperclip.paste()                             → lobby code
```

**Key offsets calibrated at 1920×1080:**
- Clipboard icon: `lobby_password_label` center + (316px right, 9px up)
- DIRECTOR card click: `choose_role_screen` template center − 175px vertically
- Region popup rows: NA=(740, 468), EU=(720, 511) — hardcoded, not template-matched (game highlights active row white which breaks matching)

## `/menu` Navigation Flow (`_do_go_to_menu`)

Loop up to 8 steps, 60s timeout. Each iteration calls `detect_current_screen()` then acts:

| Screen | Action |
|---|---|
| `main_menu` | Done — return True |
| `director_lobby` | `main_menu_button.png` → click → `yes_button.png` → click |
| `director_splash` | `latest_updates_continue.png` → click |
| `region_popup` | `play_screen_back.png` (also matches popup BACK, score 0.91) → click |
| `play_screen` | `play_screen_back.png` → click, fallback (1840, 1044) |
| anything else | `back_button.png` (orange) → click, fallback (1815, 1017) |

After each action: `sleep(1.5)` to let screen transition settle before re-detecting.

## Automation Patterns

### hover_click (required for all game UI)

The game requires a `MouseEnter` hover event before a click registers (button highlights white). **Never use `pyautogui.click(x, y)` directly** — always hover first:

```python
pyautogui.moveTo(x, y)
time.sleep(0.2)   # let game register MouseEnter / highlight
pyautogui.click()
```

### click_until (retry with re-hover)

For critical clicks where the expected outcome can be verified by template:

```python
click_until(x, y, verify_template, verify_timeout=5, max_attempts=3)
```

On each retry, moves mouse to `(960, 300)` first to force a fresh `MouseEnter` on re-approach. Returns matched center on success, None on failure.

### Template capture rules

- Always capture from a **pyautogui screenshot** (native 1920×1080), never from MCP computer-use screenshots (which are 1456×816 and will produce wrong-resolution templates)
- Self-match score must be ≥ 0.95 before using a template
- Cross-test against screens where the template should NOT match (score must be < 0.8)
- Templates with animation/glow (e.g. "CHOOSE ROLE" title) score poorly — crop a static element instead
- Avoid including ping/ms values in region templates (they change between sessions)

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

All templates captured at **1920×1080** via pyautogui. Centers listed are for the current calibration machine.

| Template | Purpose | Center (approx) |
|---|---|---|
| `play_button.png` | Main menu detection + PLAY button click | (355, 258) |
| `play_screen_region.png` | PLAY mode-selection screen gate ("CHANGE REGION" label) | (105, 1014) |
| `play_screen_back.png` | BACK button on PLAY screen and region popup (dark blue border) | (1840, 1044) |
| `region_na.png` | Detects NA (US East) is currently selected | (130, 1045) |
| `region_eu.png` | Detects EU (Frankfurt) is currently selected | (130, 1045) |
| `region_popup_header.png` | "CHOOSE YOUR REGION" popup detection | (955, 416) |
| `region_row_na.png` | NA row in popup (reference only — not used for matching) | (740, 468) |
| `region_row_eu.png` | EU row in popup (reference only — not used for matching) | (720, 511) |
| `create_custom_match.png` | CREATE NEW CUSTOM MATCH button | dynamic |
| `solo_classic_label.png` | SOLO CLASSIC card / Create Match screen gate | dynamic |
| `privacy_private.png` | Privacy setting is PRIVATE indicator | dynamic |
| `start_button.png` | Lit START button (only lit after mode selected) | dynamic |
| `choose_role_screen.png` | DIRECTOR label strip on Choose Role screen | (860, 575) |
| `lobby_password_label.png` | MATCH PASSWORD label in Director lobby | dynamic |
| `back_button.png` | Orange BACK button (Choose Role, Create Match, Custom Browser) | (1815, 1017) |
| `main_menu_button.png` | MAIN MENU button in Director lobby | (1837, 1040) |
| `quit_to_main_menu.png` | Quit confirmation popup header | (960, 457) |
| `yes_button.png` | YES button in quit confirmation | (821, 583) |
| `latest_updates_continue.png` | CONTINUE on director splash screen | dynamic |
| `placement_badge.png` | Match end detection — **not yet captured** | — |

## Calibration Checklist

- [x] `game_executable_path`
- [x] `discord_bot_token` + role created in server
- [x] `templates/play_button.png` captured
- [x] All custom lobby flow templates captured (see table above)
- [ ] `card_slots` coordinates for Electromania and Beach Party slots
- [ ] `cards.electromania.slot` / `drop_target` and `cards.beach_party.slot` / `drop_target`
- [ ] `zone_close_card_slot`
- [ ] `zone_sample_coordinates` (all 7 zones)
- [ ] `zone_drop_coordinates` (all 7 zones)
- [ ] `zone_color_thresholds` (open / closing / closed RGB values)
- [ ] `results_ocr_regions` (x,y,w,h per column per row)
- [ ] `templates/placement_badge.png` captured (requires a match to end)

## Testing Requirements

Custom matches in Darwin Project require **Director + minimum 2 players** to start. Cannot start a match with fewer.

**Test phases (in order):**
1. No game needed — zone logic, state machine, Discord commands, bypass mode all work now
2. Director client only — menu navigation, deck check, custom match creation, lobby code capture ✅ **complete**
3. Director + 2 players on separate machines — first full live match test

**EAC + VM note:** EAC actively detects and blocks virtualized environments. Running player clients in VMs on the same machine will not work. Players must be on separate physical machines.

## Pending Implementation (TODOs)

- `game/match_runner.py` — `_pre_match_deck_check()`: navigate deck tab, screenshot, compare card list
- `bot/discord_bot.py` — `_do_launch()`: replace template path placeholder with real captured template
- `bot/discord_bot.py` — `_do_deck_check()`: UI navigation to deck tab, screenshot, parse current deck
- In-game calibration: card slot coordinates, zone pixel coordinates, zone color thresholds, OCR regions (requires live Director match with 2+ players)

## Future Enhancements (from plan)

- Director points bar monitoring before card plays
- Variable zone closing timing
- Lobby screenshot polling for automatic player count detection
- Additional zone selection strategies
- Card deck modification via `/deck` parameter
- Zone coordinate calibration utility
- Multi-resolution support

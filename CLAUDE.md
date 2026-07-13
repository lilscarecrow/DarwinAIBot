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
│   └── discord_bot.py          # DarwinBot + DirectorCog + ScrimCog (all slash commands)
├── game/
│   ├── launcher.py             # Launch game process, monitor for crashes
│   ├── screen_detection.py     # OpenCV template matching, pixel sampling, screenshots
│   ├── card_actions.py         # Shift-drag card plays, key presses, bypass mode
│   ├── ocr.py                  # Tesseract results parsing, Discord formatter
│   ├── tts.py                  # TTS worker queue, broadcast/cable modes, Discord voice
│   ├── deck_utils.py           # Card point costs, deck layout from state.json
│   ├── profiles.py             # Match card play schedules
│   ├── match_runner.py         # Full match loop (card timers, zone closes, end detection)
│   ├── video_recorder.py       # Background match recording (H.264 MP4, cropped, 4fps)
│   └── ingest.py               # Push results screenshot to darwinstalker.com scrim ladder
├── session/
│   └── state.py                # BotState enum + SessionState machine
├── zones/
│   ├── zone_logic.py           # Adjacency map, valid_closeable_zones() (returns all OPEN zones)
│   ├── base_strategy.py        # Abstract base class for zone strategies
│   ├── strategy_factory.py     # Factory + strategy registry
│   └── strategies/
│       ├── outer_first.py      # Always close fewest-neighbor zone first
│       ├── random_zone.py      # Random valid zone
│       └── weighted_outer.py   # Prefer outer zones, occasional variation (default)
├── logs/                       # Runtime log (darwin_bot.log, appended across sessions)
├── screenshots/errors/         # Auto-saved on any automation failure
├── screenshots/recordings/     # Match recordings (H.264 MP4, 4fps, cropped) — deleted after each match, see Video Recorder section
└── templates/                  # OpenCV template images (captured from game, not in repo)
```

## Code Architecture

### Session State Machine (`session/state.py`)

States: `IDLE → LAUNCHING → IN_MENU → IN_CUSTOM → MATCH_IN_PROGRESS → MATCH_ENDED → IDLE`

`SessionState` tracks current state, last/next action labels, and match timer. Transitions are logged. `is_command_valid(command)` enforces which Discord commands are allowed in each state. `reset()` returns to IDLE and clears all state.

### Discord Bot (`bot/discord_bot.py`)

`DarwinBot(commands.Bot)` holds config and session. Commands live in `DirectorCog(commands.Cog)` and `ScrimCog(commands.Cog)`. `intents.members = True` is required (must also be enabled in the Discord Developer Portal under Privileged Gateway Intents).

**Guards on every command:**
1. `_role_check` — silent ignore if user lacks `discord_required_role` (per spec)
2. `_state_check` — ephemeral error if command invalid in current state
3. `_lock_check` — ephemeral error if another long-running operation is active

**`discord_required_role` is intentionally the same value as `scrim_admin_role`** (currently `"PC Scrim Admin"`) — merged so the same role gates both Director automation (`/launch`, `/custom`, `/start`, `/quit`, `/deck`) and scrim admin actions (`/role add`, `/role remove`). This means anyone with `PC Scrim Admin` can force-close the game or edit the live card deck, not just manage scrim signups — that's a deliberate scope decision, not an oversight, but worth remembering if the role is ever handed out more broadly.

**Guild command sync doesn't self-clean.** `discord_guild_ids` controls which guilds get `tree.sync()`'d on startup, but removing a guild from that list does **not** un-register the commands Discord already has stored for it — they stay registered (and visible/callable) in that guild indefinitely. If a guild is dropped from config (e.g. after a server migration), its stale command list will drift from the code over time. Not dangerous as long as nobody in that guild holds `discord_required_role`, but worth an occasional check via `GET /applications/{app_id}/guilds/{guild_id}/commands` if a guild is meant to be fully decommissioned.

**The asyncio lock (`_session_lock`)** wraps all `run_in_executor` calls to prevent concurrent operations. `/quit` bypasses this lock intentionally — it calls `_active_runner.stop()` then closes the game.

**Long-running commands** (`/launch`, `/custom`, `/start`) use `run_in_executor` to run blocking game automation in a thread without blocking the Discord event loop.

**Commands:**
| Command | Valid States | Description |
|---|---|---|
| `/launch` | IDLE | Launch game, poll for menu screen |
| `/deck` | Any | View and edit the Director deck (purely API-driven, no game UI needed) |
| `/custom <region>` | IN_MENU | Set region (NA/EU), create private match, return lobby code |
| `/start` | IN_CUSTOM | Start match, responds with results when done |
| `/menu` | IN_CUSTOM | Navigate back to main menu from any screen in the custom flow |
| `/status` | Any | Current state + last/next action |
| `/quit` | Any | Force close game — shows ephemeral Yes/No confirmation prompt |

All responses use `discord.Embed` with color-coded status (green=ok, red=fail, blue=active, orange=in-match, gray=neutral). State is shown in every embed footer.

**Results mirroring:** end-of-match screenshots are posted **only** to the `results` channel (ID `1520509048540238015`) via `_mirror_results()` — they are no longer sent as a file attachment in whatever channel `/start` was run from (that channel gets nothing further after the initial "Match In Progress" message, except for non-screenshot abort/force-stop text results, which still post there). The local screenshot file is deleted after the mirror send and the ladder ingest upload both complete.

### Scrim Signup System (`ScrimCog`)

Manages a static signup message in the configured channel. On every `on_ready`, the bot checks whether the tracked message still exists — if deleted or first run, it posts a new embed and saves the message ID to `config.json`.

**Reaction flow:**
- Players react with ✅ to sign up
- `_reactors()` excludes the bot's own seed reaction (both by `.bot` flag and explicit ID match — belt and suspenders) so the queue count reflects real players only
- When the real reactor count reaches `scrim_min_players` (default 8), `scrim_admin_role` is pinged in the **`ai-director`** channel (ID `1520518111089000548`) — not the signup channel itself
- A 1-hour reset countdown starts the moment the **first** real reactor joins an empty queue (`_start_reset_timer()`, triggered from `on_raw_reaction_add` when count hits 1) — this replaced the old "clear at the top of every wall-clock hour" behavior, so someone reacting at :59 doesn't get cut off a minute later. The timer is cancelled if the queue empties out before it fires (`on_raw_reaction_remove`) or is manually reset via `/role remove`, so a stale countdown from an old batch of reactors can never fire against a fresh one.
- When the countdown fires (`_do_reaction_reset()`), it's skipped entirely if anyone currently holds `scrim_player_role` — a scrim is underway and players shouldn't get reset/pinged mid-match. Otherwise it clears reactions and pings removed players in channel ID `1520509256678506737` to re-sign up.
- Reaction clearing (both the timer and `/role remove`) removes each real reactor's reaction **individually** (`message.remove_reaction`) rather than via a bulk `clear_reactions()` + re-add. Bulk clears appeared to leave stale names in some Discord clients' "who reacted" hover list; individual removals go through the same `MESSAGE_REACTION_REMOVE` event path used for a normal manual un-react. The bot's own seed reaction is never touched, so it doesn't need to be re-added.
- **Bot restart caveat:** the reset countdown lives in memory only. If the bot restarts while a queue already has reactors, `_resume_reset_timer_if_needed()` starts a fresh full hour from restart time rather than knowing how long the queue had already been open — a queue that was 55 minutes old right before a restart effectively gets renewed.

**Commands (scrim admin role required, i.e. `discord_required_role` — see merge note above):**
| Command | Description |
|---|---|
| `/role add` | Gives `scrim_player_role` to the first 10 reactors (Discord reaction order). Response is public (not ephemeral). |
| `/role remove` | Removes `scrim_player_role` from all who have it, clears all reactions on signup message, cancels any pending reset timer. Response is public (not ephemeral). |

Both commands' permission-denial message ("You don't have permission...") remains ephemeral — only the successful-result messages were made public.

**Config keys:**
| Key | Value |
|---|---|
| `scrim_signup_channel_id` | `1520517054988419123` |
| `scrim_signup_message_id` | Auto-persisted — do not edit manually |
| `scrim_player_role` | `PC Scrim Player` |
| `scrim_admin_role` | `PC Scrim Admin` (same value as `discord_required_role`) |
| `scrim_min_players` | `8` |
| `scrim_reaction_emoji` | `✅` |

Two channel IDs are hardcoded constants in `discord_bot.py` rather than config keys: `_AI_DIRECTOR_CHANNEL_ID` (queue-full ping) and the notify channel used in `_do_reaction_reset` (`1520509256678506737`, removed-players re-signup ping). Both were confirmed to belong to the current guild (`480566249609232389`) after the server migration — if the guild ever changes again, these need updating in code, not config.

### Match Runner (`game/match_runner.py`)

`MatchRunner.run()` is the full match sequence, called via `run_in_executor` from `/start`:

1. Press B to start match (via PostMessage to Darwin hwnd — focus-independent)
2. 5-second sync delay
3. Announce active profile name via TTS: *"Using profile: [name]"*
4. Start `VideoRecorder` in background thread
5. Main loop (wrapped in `try/finally` to guarantee recorder finalization):
   - Sleep until the next card trigger time (capped at `screen_poll_interval_seconds`) — cards fire within ~0.1s of scheduled time
   - Fire card events: check director points first, wait if insufficient, then shift-drag
   - Every 30s: sample zone pixels → `valid_closeable_zones()` → shuffle all OPEN zones → try each until one verifies
   - Every `screen_poll_interval_seconds`: double-confirm match end (two detections 2s apart, threshold 0.88)
6. Stop recorder (`finally` block — runs on normal end, force-stop, and exceptions)
7. Take screenshot of results screen → mirror to the `results` channel via `_mirror_results()` (not posted in the invoking channel) → push to ladder ingest → delete local screenshot file
8. Fire background upload of recording via `_upload_recording()` — see Video Recorder section for what this actually does today
9. Click MAIN MENU button on results screen → wait for main menu → transition to `IN_MENU`

`MatchRunner.stop()` sets a `threading.Event` that the loop checks between every action. Called by `/quit`.

`run()` returns a tuple `(results_text, recording_path)`. The discord bot unpacks this; plain string returns (e.g. force-stop) are handled via `isinstance(result, tuple)` guard.

**Director points reading (`_read_points`):**
- Primary: count filled pips by brightness (`max(B,G,R) > 130`) — immune to color/size changes
- Always use `pip_count - 1` as conservative reading (guards against partially-filled pip)
- If OCR count == pip_count (conservative + 1), the last pip is fully filled — trust OCR
- Fallback to OCR alone if pip config missing; no-op if neither configured
- Config: `director_points_pips: {"x_start": 862, "y": 1012, "spacing": 26, "count": 10}`
- Config: `director_points_region: [808, 1002, 20, 24]` — calibrated to the 2-digit numerator only (not the `/10`); x=808 skips background/tree pixels on the left, w=20 excludes the slash and denominator. OCR uses 4× upscale + Otsu auto-threshold + PSM 8 (single word) for best accuracy.

**Card point costs** (all in `CARD_POINT_COSTS` in `game/deck_utils.py`):
| Card | Cost | Card | Cost |
|---|---|---|---|
| zone_close | 3 | electromania | 3 |
| beach_party | 5 | blood_moon | 5 |
| open_zone | 5 | lava_zone | 5 |
| nuclear_blast | 5 | anti_grav_storm | 5 |
| man_hunt | 5 | spawn_electronic | 2 |
| telepathy | 3 | expose | 3 |
| warm_up | 1 | speed_boost | 1 |
| give_wood | 1 | give_leather | 1 |
| favorite_player | 0 | | |

### Zone Logic (`zones/`)

7-zone hex grid with fixed adjacency. `valid_closeable_zones()` returns all zones currently in `OPEN` state — no connectivity filtering. The bot shuffles the list and tries each zone in order, relying on the verification step (slot pixel delta check) to detect game rejections rather than pre-filtering.

The connectivity logic (`can_close_zone`, `open_zones_stay_connected`, BFS) has been removed. `neighbor_count()` is kept for the strategy classes which use it for weighting.

**Zone close flow in `_attempt_zone_close()`:**
- Grab zone_close card → read zone states from screenshot → `valid_closeable_zones()` → `random.shuffle()` → try each until slot pixel verifies or list exhausted
- The game itself enforces any rules about which zones can actually be closed

Zone strategy is pluggable via `config.json → zone_selection_strategy` but the live path currently ignores the strategy and uses random shuffle directly. Adding a new strategy: create a file in `zones/strategies/`, subclass `BaseZoneStrategy`, add to `STRATEGIES` dict in `strategy_factory.py`.

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

### Director Deck Sync (`noble-hopper/`)

> **Note:** `noble-hopper/` is gitignored — it is not tracked in this repo. It runs as a separate local process launched by `main.py`. `noble-hopper/state.json` contains captured auth tokens and must never be committed.


The Director deck is managed entirely via the game's API — no UI automation is needed. The noble-hopper process (mitmproxy + web server) handles sync.

**Why response injection doesn't work for the deck:**
The game treats `sDPowerArray` (in `othersOptions`) as local state — it pushes its local deck TO the server via `saveOthersOptionsCommand` at startup rather than reading it from the server. Injecting into the profile GET response is ignored. Skins and power unlocks work with response injection because the game reads those FROM the server with no local cache.

**Three sync paths (in priority order):**

1. **Startup proxy sync** (`proxy_addon.py` `request` hook) — On the first request to `darwinproject.ca` after game launch, if `directorDeckEnabled = True` in `state.json`, the proxy makes a blocking `saveOthersOptionsCommand` POST using the fresh auth headers from that intercepted request. This runs BEFORE the game's profile GET, so the server has the correct deck when the game initializes. This is the primary sync path. Syncs every launch unconditionally (the `needsSync` gate was removed — it was unreliable).

2. **Pre-launch force sync** (`_do_launch()` in `discord_bot.py`) — Before launching the game, the bot calls `/api/force-sync-deck` on the noble-hopper server, which uses the auth token captured from the previous game session. This handles the case where `needsSync` was already True from a deck edit before this launch. May fail if the token has expired between sessions.

3. **Piggybacked sync** (`proxy_addon.py` `request` hook) — Whenever the game sends any `saveOthersOptionsCommand` (e.g. when visiting the Director Deck screen in-game), the proxy intercepts and overrides `sDPowerArray` with the configured deck.

**`needsSync` flag flow:**
- Set to `True` by `_write_deck()` in `discord_bot.py` whenever the user saves a deck change via `/deck`
- Cleared to `False` by the startup proxy sync on success
- Cleared to `False` by `/api/force-sync-deck` on success
- The 30-second rate limit on `_last_sync_attempt` in `SkinChangerAddon` prevents hammering the API if multiple game requests fire in quick succession at startup

**`state.json` key fields:**
- `directorDeck` — 11-slot array of `ItemType_*` strings (the desired deck)
- `directorDeckEnabled` — bool, must be true for any sync to fire
- `capturedApiUrl` — `https://pc-live.api.darwinproject.ca/profile/commands/<userId>` (captured from game traffic)
- `capturedApiHeaders` — auth headers from the game's last API request (token refreshes each session)
- `lastOthersOptions` — full `othersOptions` object template, needed to construct valid `saveOthersOptionsCommand` body
- `lastSyncedDeck` — cleared by `_write_deck()` to mark a pending change
- `needsSync` — True when a deck change is pending startup sync

**Card display aliases** (`_DIRECTOR_CARDS` in `discord_bot.py`):
The Discord UI shows friendly names that differ from the internal ItemType. The mapping is display-only — the ItemType values used in all API calls are unchanged:
| Display name | ItemType |
|---|---|
| Beach Party | `ItemType_SDP_NakedAll` |
| Blood Moon | `ItemType_SDP_Hecatombe` |
| Expose | `ItemType_SDP_MutualVision` |
| Spawn Electronic | `ItemType_SDP_ActivatePylon` |
| Electromania | `ItemType_SDP_ActivateAllPylons` |

### Card Actions (`game/card_actions.py`)

`play_card()` shift-drags from a slot coordinate to a target coordinate, then optionally verifies the card left its slot via template match. All actions respect `bypass_mode` — when enabled, logs the action and waits for Enter instead of sending input to the game.

**`press_key()` — PostMessage routing for game input:**
Darwin Project uses Raw Input. `pyautogui.press()` sends to whichever window is focused, which is often Discord. `press_key()` sidesteps this by sending `WM_KEYDOWN` / `WM_KEYUP` via `win32api.PostMessage()` directly to the Darwin hwnd — no focus change required. Keys in `_SCAN_CODES` (`b`, `escape`, `shift`, and others) use this path; anything not in that dict falls back to `pyautogui.press()` with `focus_darwin_window()` first.

**`focus_darwin_window()` — AttachThreadInput trick:**
Standard `SetForegroundWindow()` fails from background processes (Windows blocks cross-process focus stealing). The fix: call `AttachThreadInput(current_thread, darwin_thread, True)` to share input queues before calling `SetForegroundWindow()`. Always detach after. Only needed for the `pyautogui` fallback path — PostMessage-based keys don't require focus at all.

**`shift_down()` / `shift_up()` — public shift hold:**
Used internally by `play_card()`, `grab_card()`, and `complete_drag()`. The same two-part combo (pyautogui.keyDown + PostMessage WM_KEYDOWN) is required — both must fire or the tray won't open.

**`grab_card(slot_coordinate, shift_already_held=False)` / `release_card()` / `complete_drag(target_coordinate, card_name)`:**
Split the card drag into three steps so zone state can be read from the map between grab and play:
- `grab_card()` — shift+moveTo+mouseDown; the big zone map appears on mouseDown. Pass `shift_already_held=True` if the caller already called `shift_down()` (e.g. to hold shift for a before-screenshot) — avoids a redundant second `shift_down`.
- `release_card()` — mouseUp+shift_up with no drag; cancels the play, card returns to slot
- `complete_drag()` — moveTo+mouseUp+shift_up to finish a grab already in progress

**Zone state detection — grab-based:**
The big zone map only appears when a zone_close card is grabbed (shift+click+hold). Zone states cannot be read with a shift-only peek. The flow in `_attempt_zone_close()`:
1. Wait for enough director points
2. `shift_down()` → before-screenshot (tray visible) → `grab_card(shift_already_held=True)` → `time.sleep(0.35)` → screenshot
3. `_update_zone_states_from_screenshot()` — votes across 4 `zone_map_sample_points` per tile; majority wins
4. `valid_closeable_zones()` → pick zone (live path uses random shuffle across valid zones, not the configured strategy — intentional for now)
5. `complete_drag(keep_shift=True)` to target zone → after-screenshot → verify slot pixel changed → `shift_up()`, or `mouseUp()+shift_up()` if nothing closeable
In bypass mode, uses cached `_zone_states` (all OPEN initially) and calls `play_card(bypass_mode=True)`.

**Zone_close slot verification threshold — 80, not 40:**
The inline pixel delta check in `_attempt_zone_close` uses `delta > 80` (not the global 40 used by `_verify_card_removed`). This is intentional.

The zone_close card's tray position varies by profile. In the Blood profile (custom_a), beach_party is never played, so the tray always has one extra card. This pushes zone_close from x=814 (Standard/Everything) to x=852. The game world background bleeds slightly through the card art at x=852, causing a consistent small delta of ~56-58 even when the card returns to its slot after a rejection. This is not a timing issue — it is a specific background bleed at that screen coordinate. Real plays produce delta of 240+; the false-positive bleed produces delta 56-58. Threshold 80 sits safely between them.

`_verify_card_removed` (used by all non-zone_close cards) remains at delta > 40 — those cards are never affected by this background bleed.

**Zone visual states on the big map (what to calibrate against):**
- **OPEN**: plain medium blue/teal hex, no border glow
- **CLOSING**: visibly darker (navy/dim) hex, no orange border — tile darkens as lava begins but orange outline has not appeared yet
- **CLOSED**: dark red/maroon hex with a bright orange border glow at the edges

Sample points are placed at ~110px from each tile center (near the hex edge, well outside the player icon area at center). At this distance, colors are: OPEN=blue, CLOSING=dark navy, CLOSED=dark red. The orange border glow is right at the very edge and would require sampling at ~120-125px to catch; the interior color differences are sufficient for three-way distinction. Calibrate `zone_color_thresholds` using `calibrate_zone_colors.py` while holding a zone_close card with known zone states visible.

### Video Recorder (`game/video_recorder.py`)

Records match footage in a background thread. Started after the match countdown, stopped in a `try/finally` so the file is always finalized regardless of how the match ends.

- **Format:** H.264 MP4 (`avc1`) — all three FOURCC options tested True on this machine
- **FPS:** 4 (1 frame every 0.25 seconds)
- **Crop:** configured via `recording_crop_region: [x, y, w, h]` — currently `[755, 175, 410, 200]` (tight center band focused on the kill feed area)
- **Output:** `screenshots/recordings/match_{timestamp}.mp4` — ~60-65 MB for a 20-min match at these settings (roughly 8x the file size of the old 0.5fps setting)
- **Upload:** `_upload_recording(path)` fires as a detached async task after the match. The actual upload is still a stub (TODO — wire up when `recording_api_endpoint` is set in config). **The local recording file is deleted unconditionally at the end of `_upload_recording()` regardless of whether an upload happened** — this was an explicit choice to reclaim disk space now, accepting that until the upload is actually implemented, recordings aren't preserved anywhere once deleted.

**Safety:** process crash will leave the file corrupt (VideoWriter MOOV atom not flushed). All other exit paths (normal end, `/quit`, exceptions, asyncio timeout) are covered by the `try/finally`.

### Ladder Ingestion (`game/ingest.py`)

Pushes the raw end-of-match results screenshot to the **`darwinstalker.com`** scrim ladder ingestion API (spec: `SHOW_DIRECTOR_HANDOFF.md`, gitignored — not tracked in this repo, and still documents the old `ds.xdos.ai` base URL — see domain migration note below). Everything sent lands in an **unpublished draft** grouped by (platform, day UTC); a human moderator reviews and publishes later, so this is genuinely fire-and-forget — failures are logged and swallowed, never retried.

- `post_results_screenshot(screenshot_path, base_url, token, platform)` — `POST /api/ingest/screenshot`, multipart form with the PNG + `platform`. Called from `discord_bot.py`'s `_post_results_to_ingest()` via `run_in_executor` (blocking `requests` call off the event loop).
- Wired into both match-end paths (`/start` and the auto-start watcher) in `discord_bot.py`, right after `_mirror_results()` and before the local screenshot file is deleted — the file must still exist on disk when this fires.
- No-ops silently if `ds_ingest_token` is unset in config.
- Success response: `{"draft_id": ..., "game_index": ..., "ocr_error": ...}` — logged at INFO. `ocr_error: null` means the server's OCR read the scorecard cleanly.

**Domain migration — `ds.xdos.ai` → `darwinstalker.com`:** the ladder site moved domains; `ds.xdos.ai` now 301-redirects to `darwinstalker.com`. `config.json → ds_ingest_base_url` was updated to `https://darwinstalker.com` directly. This mattered because a 301 redirect downgrades a `POST` to a `GET` when followed (standard client behavior, not a bug) — hitting the old `ds.xdos.ai` URL produced `405 Method Not Allowed` with an empty body, since the redirect target's route only accepts `POST`. If ingest ever starts failing with `HTTP 405` again, check for another redirect first (`requests.post(..., allow_redirects=True)` then inspect `resp.history` for a 301/302) before assuming the API contract changed.

**SSL cert gotcha (Windows):** `requests`/`certifi` ships its own fixed CA bundle instead of using the Windows trust store. On this machine, something doing TLS interception (AV/corporate proxy) re-signs HTTPS traffic with a root CA that Windows trusts but `certifi` doesn't — every `requests` call to the ingest API failed with `SSLCertVerificationError: unable to get local issuer certificate` until `pip-system-certs` was installed (patches Python to use the OS trust store at interpreter startup, no code changes needed). It's in `requirements.txt` — if a fresh machine hits the same SSL error, this is the fix.

### TTS (`game/tts.py`)

All TTS is **fire-and-forget** — no call blocks the match loop. A single `_worker_thread` processes `(text, mode)` tuples from `_queue` in order.

**Config required:** `tts_device` must be set in `config.json` (e.g. `"CABLE Input"`). If absent, TTS is silently disabled (`tts.is_enabled()` returns False). Use `tts.is_enabled()` to check — do not access `_device_name` directly.

**Broadcast lifecycle (in-game voice chat via G key):**
- G opens a 15s window, then 90s cooldown (`_BROADCAST_CYCLE = 105s`)
- `try_open_broadcast()` — checks cooldown, presses G if available, returns True/False
- `queue_close_broadcast()` — queues a sentinel that presses G to close the window **after all preceding audio in the queue finishes**. This is the correct way to close broadcast — never call `close_broadcast()` directly mid-sequence.
- `close_broadcast()` — presses G immediately and resets cooldown to 90s from now. Only call directly when no audio is queued.

**TTS functions:**
- `speak_cable(text)` — queues audio to CABLE Input, no G press. Use for all in-broadcast announcements (broadcast window already open).
- `speak(text, broadcast=True)` — queues audio; if `broadcast=True`, the worker checks cooldown, presses G, plays audio (standalone use — not used in the match loop).
- `try_open_broadcast()` + `speak_cable(...)` + `queue_close_broadcast()` — the correct pattern for in-match card announcements.

**Waiting-on-points broadcast close:**
If `_wait_for_points()` needs to wait and the broadcast window is open, it queues `speak_cable("Waiting on points for X")` then `queue_close_broadcast()` immediately — the 90s cooldown starts ticking while waiting for points. When points arrive, `_fire_card_event` tries `try_open_broadcast()` again (may fail if cooldown hasn't expired).

**Pre-caching:** `precache_async(phrases)` fires a background thread that generates and caches all TTS audio via edge-tts before cards fire, so every queued phrase hits the cache instead of making a live network request.

**Discord voice mirroring:** `/voice join` connects the bot to the user's voice channel — all TTS audio then plays concurrently to both CABLE Input and the Discord channel. Set/cleared via `tts.set_voice_client(vc)`.

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
  press_key("escape")                           close the lobby menu
  press_key("shift")                            dismiss the initial tray display
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
- **"REWARD" text on the results screen does NOT render in pyautogui screenshots** (it lives on a separate GPU layer). Use the MAIN MENU button (`placement_badge.png`) for match-end detection instead.

### Match end detection pitfalls

`placement_badge.png` is the "MAIN MENU" button (white text on dark blue, 98×30px). It appears on the results screen and is also used by `_do_post_match_return()` to find and click MAIN MENU after the match.

- Threshold is **0.88** — lower values cause false positives from HUD elements during the match
- Double-confirm required: the template must match **twice, 2 seconds apart** before ending the match (`_match_has_ended()`)
- `poll_for_match_end()` saves a debug screenshot to `screenshots/errors/` on every positive detection (useful for debugging false positives)

### Match profiles (`game/profiles.py`)

Profiles define the card play schedule. Active profile is set in `config.json → active_profile`.

**Standard profile** (default): Electromania 2:30 · Beach Party 4:00 · Electromania 6:30 · Blood Moon 9:00

Adding new profiles: add an entry to `PROFILES` dict in `game/profiles.py`. The bot picks it up via `get_profile()` — no other changes needed.

## Config Reference (`config.json`)

```json
{
    "game_executable_path": "",          // Full path to DarwinProject.exe
    "discord_bot_token": "",             // Discord bot token (keep secret)
    "discord_required_role": "PC Scrim Admin",   // Intentionally the same value as scrim_admin_role — see merge note in Discord Bot section
    "discord_guild_ids": ["..."],        // Guild IDs for instant slash command sync — removing an ID here does NOT un-register commands already synced to that guild, see note above
    "zone_selection_strategy": "weighted_outer",
    "active_profile": "standard",        // Match card play profile (see game/profiles.py)
    "ahk_bypass_mode": false,            // true = log actions instead of executing
    "tts_device": "CABLE Input",         // Sounddevice output name for TTS; omit to disable TTS
    "tts_voice": "en-US-AriaNeural",  // edge-tts voice name
    "card_play_lead_time_seconds": 2,    // Fire card events this many seconds early to account for drag time

    // Card tray layout (calibrate with shift held in-game)
    "card_tray_center_x": 966,           // X center of the tray when all cards visible
    "card_tray_card_y": 943,             // Y coordinate of card center row
    "card_tray_card_width": 76,          // Pixel spacing between card centers

    "cards": {
        "electromania": {
            "drop_target": null          // [x, y] to drag to — calibrate in-game
        },
        "beach_party": {
            "drop_target": null
        },
        "blood_moon": {
            "drop_target": null
        }
    },
    "zone_map_sample_points": {          // 3-5 [x,y] points per zone tile on the big map (appears when grabbing a zone_close card)
        "1": null, "2": null, "3": null, "4": null, "5": null, "6": null, "7": null
    },
    "zone_drop_coordinates": {           // [x, y] drag target per zone — calibrate
        "1": null, ..., "7": null
    },
    "zone_color_thresholds": {           // RGB tuples for open/closing/closed — calibrate against whichever source above is active
        "open": null,
        "closing": null,
        "closed": null
    },
    "results_ocr_regions": null,         // Per-column (x,y,w,h) lists — calibrate (optional; bot now sends screenshot)
    "director_points_region": [808, 1002, 20, 24],  // OCR crop: 2-digit numerator only (not "/10"). Calibrated at 1920×1080.
    "director_points_pips": {            // Pixel sampling for filled pip count
        "x_start": 862, "y": 1012, "spacing": 26, "count": 10
    },
    "screen_poll_interval_seconds": 12,
    "launch_timeout_seconds": 180,

    // Video recording
    "recording_api_endpoint": "",        // POST endpoint for upload — upload is still a TODO stub; local file is deleted after each match regardless (see Video Recorder section)
    "recording_crop_region": [755, 175, 410, 200],   // [x, y, w, h] crop at 1920×1080 — tight center band on kill feed

    // Ladder ingestion (darwinstalker.com, formerly ds.xdos.ai) — see game/ingest.py
    "ds_ingest_base_url": "https://darwinstalker.com",
    "ds_ingest_token": "",                // Bearer token, issued out of band — leave empty to skip ingest
    "ds_ingest_platform": "pc",           // "pc" | "xbox"

    // Scrim signup system
    "scrim_signup_channel_id": "1520517054988419123",
    "scrim_signup_message_id": null,     // Auto-persisted by bot on startup — do not edit manually
    "scrim_player_role": "PC Scrim Player",
    "scrim_admin_role": "PC Scrim Admin", // same value as discord_required_role — see merge note above
    "scrim_min_players": 8,
    "scrim_reaction_emoji": "✅"
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
| `placement_badge.png` | Match end detection — **MAIN MENU button** (98×30px at x=1780, y=1028) | (1829, 1043) |

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
- [ ] `results_ocr_regions` (x,y,w,h per column per row) — not needed if sending screenshot to Discord
- [x] `templates/placement_badge.png` captured (MAIN MENU button, 98×30px — self-match 1.0, in-game HUD 0.50)
- [x] `director_points_region` calibrated to `[808, 1002, 20, 24]` (2-digit numerator only)
- [x] `director_points_pips` calibrated in config.json
- [x] `tts_device` set to `"CABLE Input"` in config.json

## Testing Requirements

Custom matches in Darwin Project require **Director + minimum 2 players** to start. Cannot start a match with fewer.

**Test phases (in order):**
1. No game needed — zone logic, state machine, Discord commands, bypass mode all work now
2. Director client only — menu navigation, deck check, custom match creation, lobby code capture ✅ **complete**
3. Director + 2 players on separate machines — first full live match test

**EAC + VM note:** EAC actively detects and blocks virtualized environments. Running player clients in VMs on the same machine will not work. Players must be on separate physical machines.

## Pending Implementation (TODOs)

- `bot/discord_bot.py` — `_do_launch()`: replace template path placeholder with real captured template
- In-game calibration: card slot coordinates, zone pixel coordinates, zone color thresholds, OCR regions (requires live Director match with 2+ players)

## Player-Targeted Cards

Cards that target a specific player (`expose`, `favorite_player`, `give_leather`, `give_wood`, `man_hunt`, `speed_boost`, `warm_up`) drag to a player card slot at the top of the screen. These are grouped as `_PLAYER_TARGETED_CARDS` in `match_runner.py` and pick from `player_target_coordinates` in config at runtime (same pattern as zone-targeted cards).

**Player bar layout:**
- Up to 9 numbered player cards (slots 1–9) run across the top of the HUD
- A 10th card sometimes appears at the far left — this is a known client glitch (spinning loading icon, no name, no health bar). It is not a real player and must be excluded from targeting.
- Dead/eliminated players show: greyed-out name, "ELIMINATED" text, X overlaid on portrait, and no health bar.

**Do not use OCR for player targeting.** Names are small, variable color, and the glitch card has no name. The reliable signal is the **health bar**:
- Alive player → colored health bar pixel at the bottom of the card
- Dead / glitch card → dark/empty pixel at that position

**Planned implementation (not yet built):**
- Config: `player_card_slots` — list of `[x, y]` center coordinates for each of the 9 fixed slot positions (calibrate once; positions don't change between matches even if players do)
- Config: `player_health_bar_y_offset` — pixel offset below card center where the health bar sits
- At play time: sample the health bar pixel for each slot; collect slots where pixel is colored (alive); pick one at random and drag there
- The glitch slot is excluded by only calibrating slots 1–9 in `player_card_slots`

## Future Enhancements (from plan)

- Variable zone closing timing
- Lobby screenshot polling for automatic player count detection
- Additional zone selection strategies
- Zone coordinate calibration utility
- Multi-resolution support
- Player-targeted card implementation (health bar sampling to find alive players — see Player-Targeted Cards section above)

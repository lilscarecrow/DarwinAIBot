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

**Startup summary (2026-09-06):** after config validation, `main.py` checks/connects each service and finishes with an easy-to-grep block:
```
=== Startup Summary ===
Discord: OK (logged in as AI Director#8139)
Twitch chat (!pov): OK
Twitch events (subs/cheers/channel points): 5/5 OK
OBS: OK (see connection check above)
========================
```
- **OBS** is checked synchronously and immediately in `main()` via `obs_control.check_connection()` — a one-time connect + `GetVersion` call, so a wrong host/port/password or OBS not running is reported at startup instead of only surfacing the first time `/custom` tries to use it. No-ops (reports "disabled") if `obs_stream_enabled` is `false`. Its result is cached (`obs_control.last_check_ok()`) for the summary to read.
- **Discord** and **Twitch** connect asynchronously, so `_log_startup_summary()` runs as a third task alongside `discord_bot.start()`/the Twitch bot in the same `asyncio.gather()` — it `wait()`s on an `asyncio.Event` each bot sets once its own setup finishes (`DarwinBot.startup_done` in `on_ready()`; `DarwinTwitchBot.startup_done` at the end of `setup_hook()`), with a 30s timeout (`_STARTUP_SUMMARY_TIMEOUT_SECONDS`) per service so a hang during connection doesn't leave the summary waiting forever — it reports "TIMED OUT" instead.
- **Twitch's line is two-part**, not just "connected": `chat_subscribed` (bool) and `events_subscribed` (`(succeeded, attempted)` tuple) are captured from `_subscribe_chat()`/`_subscribe_events()`'s return values in `setup_hook()`, so the summary can say e.g. `3/5 OK` rather than a single pass/fail — the individual per-type failures are still logged in detail above this block by those methods themselves; the summary is just the at-a-glance rollup.
- This is a one-time startup report only — it does not re-fire on a later reconnect (see the EventSub reconnect notes above for what handles that).

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
│   ├── launcher.py             # Launch game via Steam protocol, monitor for crashes
│   ├── screen_detection.py     # OpenCV template matching, pixel sampling, screenshots
│   ├── card_actions.py         # Shift-drag card plays, key presses, bypass mode
│   ├── ocr.py                  # Tesseract results parsing, Discord formatter
│   ├── tts.py                  # TTS worker queue, broadcast/cable modes, Discord voice
│   ├── deck_utils.py           # Card point costs, deck layout from state.json
│   ├── profiles.py             # Match card play schedules
│   ├── match_runner.py         # Full match loop (card timers, zone closes, end detection)
│   ├── video_recorder.py       # Background match recording (H.264 MP4, cropped, 4fps)
│   ├── ingest.py               # HTTP calls to the darwinstalker.com scrim ladder (open/close draft, screenshot)
│   ├── ds_lifecycle.py         # DraftLifecycle: the ONE place the bot drives the ladder draft (see docs/DS_LIFECYCLE_HANDOFF.md)
│   ├── ds_relay.py             # background thread batching live match events + relayed log lines to the ladder
│   └── ds_log_handler.py       # logging.Handler forwarding WARNING+ (and ladder INFO) lines to the ladder's event stream
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

### Game Launcher (`game/launcher.py`)

`launch_game()` launches the game via `os.startfile("steam://rungameid/544920")`, **not** by running `Darwin.exe` directly. `game_executable_path` in config is still validated (must exist on disk) as a sanity check that the game is installed where expected, but the path itself is no longer executed.

**Why:** prior to the game's UE5 update, `subprocess.Popen([exe_path])` on `Darwin.exe` worked fine. After the update, direct-launching `Darwin.exe` causes it to exit immediately without ever spawning `Darwin-Win64-Shipping.exe` — no crash dump, no Windows Application Error event, and the Shipping exe's own debug.log never gets touched. `Darwin.exe` is a thin Steamworks/EasyAntiCheat bootstrap stub (this title uses EAC — see `installscript_544921.vdf` in the Steam install dir), and it appears to now validate that it was launched through Steam's own process context before proceeding — something the UE5 rebuild seems to have tightened. Launching via the `steam://rungameid/` protocol instead routes through the same path a manual Steam launch uses, sidestepping the check entirely. Steam App ID `544920` is fixed for this title (confirmed via `appmanifest_544920.acf`) and is hardcoded as `STEAM_APP_ID` in `launcher.py` — it is not machine-specific like the exe path.

**Caveat:** the Steam client must be running (and logged in) for `steam://` launches to work. If Steam isn't already open, the protocol handler will start it first, which may add extra time before `Darwin-Win64-Shipping.exe` appears — worth keeping in mind if `launch_timeout_seconds` (180s default) ever needs bumping.

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
| `/custom <region>` | IN_MENU | Set region (NA/EU/APAC), create private match, return lobby code |
| `/start` | IN_CUSTOM | Start match, responds with results when done |
| `/menu` | IN_CUSTOM | Navigate back to main menu from any screen in the custom flow |
| `/status` | Any | Current state + last/next action |
| `/quit` | Any | Force close game — shows ephemeral Yes/No confirmation prompt |
| `/pov <player>` | IN_CUSTOM, MATCH_IN_PROGRESS | Switch the Director's camera to a player's point of view |
| `/tournament <enabled>` | Any | Toggle tournament mode on/off (persisted to config.json) |

**`/tournament` (2026-09-04):** toggles `tournament_mode` (config, default `false`) — persisted via `DirectorCog._persist_config_value()`, a generalized version of `ScrimCog._save_message_id()`'s read-modify-write pattern, so it survives a bot restart. Valid in any state (only `_role_check`, no `_state_check`) since it's a pure config flag, not tied to session/match state. Two behavior changes while it's on:
- **`/custom`'s OBS stream start is delayed** `_TOURNAMENT_STREAM_DELAY_SECONDS` (120s) instead of going live the instant the lobby is created — fired via `DirectorCog._delayed_stream_start()` on `asyncio.ensure_future` (not awaited), so it doesn't block `/custom`'s response or anything else in that success path. Since the outcome isn't known yet at response time, the embed's "Twitch Stream" field shows `🕐 Starting in 2 min (tournament mode)` instead of the normal `🔴 Live`/`⚠️ Could not start`.
- **The anti-cheat minimap cover never auto-reveals.** `MatchRunner.__init__` reads `tournament_mode` into `self._tournament_mode`, and the main loop's timed-hide check (see Anti-cheat minimap cover above) is skipped entirely when it's set — the cover shown at `/custom` time simply stays up for the rest of the match instead of uncovering at `obs_minimap_cover_seconds`.

**`/custom`'s full details embed now goes to the lobby-ping channel, not the command sender (2026-09-07):** the "Custom Match Ready" embed (region, profile, lobby code, ladder-claim nudge, Twitch stream status) is sent to `_LOBBY_PING_CHANNEL_ID` as `ping_ch.send(content=f"<@&{_LOBBY_PING_ROLE_ID}>", embed=embed)` — same role ping as before, now carrying the embed instead of a bare `<role> <code>` text line. The command sender instead gets a short `/launch`-style confirmation (`self._ok("Custom Match Ready", "Private lobby created. Run \`/start\` to begin the match.")`) telling them what to do next, mirroring `/launch`'s "Run `/custom` to create a match." response. Everything else in `/custom`'s success path (OBS stream start, ladder draft open, Twitch announce, minimap cover, auto-start watcher) is unchanged — only which channel gets which message moved.

**`/pov` (2026-08-30):** dropdown of `1`-`9`, `0` (`app_commands.Choice[int]`, matching the game's own number-row hotkeys — `0` is the 10th slot). Sends the corresponding digit key straight to the Darwin window via `press_key()` in `game/card_actions.py`, same PostMessage mechanism as every other game keystroke (`_SCAN_CODES` now includes scan codes for `0`-`9`). Valid in both the custom lobby (`IN_CUSTOM`, before `/start`) and mid-match (`MATCH_IN_PROGRESS`). Intentionally skips `_lock_check` in both states — like `/quit` — since `_session_lock` is held for the entire match duration by `/start`'s `_run_match()` (gating on it would make `/pov` uncallable mid-match, exactly when it's needed most); it isn't held throughout `IN_CUSTOM` so the skip is a no-op there, just kept for consistency with the match-time behavior. Calls `press_key(key, no_focus_fallback=True)` so a missing Darwin window reports failure back to Discord instead of silently sending the digit to whatever window happens to have OS focus.

**POV locked during match startup (2026-09-09):** `/pov`, `!pov`, and the channel-points redemption all now additionally check `session.is_pov_locked()` (on top of the existing `is_command_valid("pov")` state check) and refuse — ephemeral reply on Discord, a chat reply on Twitch, a silent refund on the redemption, same as any other rejected attempt — while it's `True`. `SessionState._pov_locked` is set the instant a `MatchRunner` is constructed (`/start`, the auto-start watcher — covers both call sites uniformly rather than setting it at each one) and cleared right after `run()`'s forced default-POV-1 press completes. Why: a manual POV switch during that narrow startup window enlarges a different card's name band (the "spectated" card is drawn bigger, per `game/player_cards_v2.py`), which would corrupt `_log_slot_map_snapshot()`'s OCR read — that snapshot and the forced POV-1 press both happen in this same window, before any manual override should be possible. If a match aborts before ever reaching the POV-1 press, `SessionState.reset()` (called by `_reset_session()`, the universal path back to `IDLE`) clears the lock too, so an aborted match can never leave POV stuck refusing forever.

**`/deck` unregistered (2026-08-07):** the current Director deck (2× Electromania, Beach Party, Blood Moon, 3× Zone Closing, Give Wood, 2× Favorite Player, Telepathy) already covers every match profile via natural unlocks, so live deck editing isn't a day-to-day need anymore. The `deck()` method and its supporting code (`DeckEditorView`, `_read_deck`/`_write_deck`, `_DIRECTOR_CARDS`) are still in `discord_bot.py` — only the `@app_commands.command` decorator was removed, so it no longer syncs to Discord for anyone. Re-add the decorator to bring it back.

All responses use `discord.Embed` with color-coded status (green=ok, red=fail, blue=active, orange=in-match, gray=neutral). State is shown in every embed footer.

**Results mirroring:** end-of-match screenshots are posted **only** to the `results` channel (ID `1520509048540238015`) via `_mirror_results()` — they are no longer sent as a file attachment in whatever channel `/start` was run from (that channel gets nothing further after the initial "Match In Progress" message, except for non-screenshot abort/force-stop text results, which still post there). The local screenshot file is deleted after the mirror send and the ladder ingest upload both complete.

### Twitch Chat Bot (`bot/twitch_bot.py`) (2026-08-30)

A second bot — `DarwinTwitchBot(commands.Bot)`, built on **TwitchIO 3.x** — runs concurrently with the Discord bot in the same process/event loop, sharing the exact same `SessionState` instance so it can never drift out of sync with what the Discord bot and `MatchRunner` see.

**Scope is deliberately narrow: `!pov` is the only chat command.** Status/profile/etc. are intentionally Discord-only and were *not* mirrored here, even though they'd have been trivial to add — a custom bot was chosen over an off-the-shelf one (Nightbot/StreamElements/Moobot) specifically so viewer-triggered actions like this can call into the same automation the Discord bot already has, which no off-the-shelf bot could do without a clunky webhook bridge. If Discord-style status/profile output in Twitch chat is ever wanted, it's a small addition to `PovComponent` (or a sibling `Component`) — not a redesign.

**`!pov <1-9, 0>`** (`PovComponent` in `twitch_bot.py`) — mirrors the Discord `/pov` command exactly: same `_VALID_POV_KEYS`, same `session.is_command_valid("pov")` gate (valid in `IN_CUSTOM`/`MATCH_IN_PROGRESS`, from the same `VALID_COMMANDS` table in `session/state.py`), same `press_key(key, no_focus_fallback=True)` call via `run_in_executor`. **Mod-only**, via `@commands.is_moderator()` — a guard checked before the command body runs. `event_command_error()` is overridden to catch `commands.GuardFailure` and silently ignore it (matching the Discord bot's silent-reject-on-missing-role convention: `_role_check` sends nothing back either) rather than logging every non-mod's attempt as an error.

**One announcement, not per-match:** `DarwinTwitchBot.announce(text)` posts to the broadcaster's chat and is called exactly once, in `/custom`'s success path in `discord_bot.py` (right next to the OBS `start_stream()` call) — a static message introducing the scrim, linking the Darwin Pro League Discord (`https://discord.gg/ynSjykan5C`), and linking the streamer's Ko-fi (`https://ko-fi.com/lilscarecrow`) — 242 characters, well under Twitch's 500-char chat limit. This replaced an earlier draft that announced every match start/end; that was cut in favor of a single lobby-creation announcement instead. `_twitch_announce()` (the `DirectorCog` helper that calls `announce()`) no-ops silently if `self.bot.twitch_bot` is `None` (i.e. `twitch_enabled: false`), and `announce()` itself never raises — same fire-and-forget convention as `ds_ingest`/OBS elsewhere in this codebase.

**Concurrent bot execution (`main.py`):** previously `main.py` called the blocking `discord_bot.run(token)`. It's now `async def _run(config, session)`, using `asyncio.gather()` to run `discord_bot.start(token)` and the Twitch bot together on one event loop, wrapped in `async with DarwinTwitchBot(...) as twitch_bot:` for proper cleanup. When `twitch_enabled` is `false` (the default), only `discord_bot.start()` runs — behavior is unchanged from before this feature existed. `DarwinBot.twitch_bot` is set to the running `DarwinTwitchBot` instance (or left `None`) before `start()` is called, which is how `DirectorCog` reaches it.

**EventSub subscriptions must survive a reconnect, not just a fresh login (2026-09-05 fix, revised same day after a duplicate-events regression):** found live — a network blip cycled the bot through 3 EventSub websocket sessions in about 30 seconds, and TwitchIO's own internal resubscribe-on-reconnect logic tried to migrate all 6 subscriptions (chat, subs, gift subs, resub messages, cheers, channel points) to the new session and failed every single one with `400: 'websocket transport session does not exist or has already disconnected'` — the session had already died again by the time each request reached Twitch. Nothing retried after that; chat and channel-points listening went silently dead for the rest of the process's life (no crash, no further errors, no log activity at all for that subsystem) until the next manual restart, while everything else (Discord, ad breaks — plain REST calls, unrelated to this websocket) kept working the whole time, masking the outage. `_subscribe_chat()`/`_subscribe_events()` used to only run from `setup_hook()` (startup) and `event_oauth_authorized()` (a fresh OAuth grant) — neither fires on a reconnect.

The first fix made `DarwinTwitchBot.event_websocket_welcome()` (fires on every new websocket session, including reconnects) blindly call both subscribe methods again on every welcome after the first (`self._seen_first_welcome` skips the very first one, which `setup_hook()` already covers). **This caused a regression, found live the same day: every `!pov` reply, sub/cheer shoutout, and channel-points action started firing twice.** Root cause: TwitchIO's own internal resubscribe-on-reconnect usually *does* succeed — the original incident was the exception, not the rule — and blindly creating our own subscriptions again raced that internal attempt. Once the old session's subscriptions are gone (the normal case — Twitch auto-revokes websocket-transport subscriptions when their connection closes), both the internal attempt and ours look like a fresh, non-conflicting creation from Twitch's side, so both can succeed independently, leaving two live subscriptions delivering the same event twice.

**Current fix:** `_resubscribe_missing()` (called from `event_websocket_welcome()` for every welcome after the first) waits `_RESUBSCRIBE_GRACE_SECONDS` (8s) for TwitchIO's own migration to finish, then calls `fetch_eventsub_subscriptions(token_for=owner_id, user_id=owner_id)` to check what's actually `enabled`, and only (re-)creates the subscription types that are genuinely missing — `_subscribe_events()` gained a `skip_types` param for this. If the status check itself fails (network error, etc.), it falls back to resubscribing everything, accepting the same small duplicate-risk window as before but now only in that narrower failure case instead of on every single reconnect.

**A Twitch-side failure must never take down the Discord bot (2026-08-30 fix):** the Twitch bot's `start()` is wrapped in a local `_run_twitch()` coroutine with its own try/except before being handed to `asyncio.gather()` — without that wrapper, any unhandled exception from the Twitch side (e.g. the EventSub-subscription failure below, or any future one) propagates through `gather()` and cancels the Discord task too, killing the *entire* bot process over what should be a nice-to-have on top of the core Discord automation. This was found live: on a first run before the one-time browser OAuth step was completed, `setup_hook()`'s EventSub subscription 403'd (no authorized user token yet) and took the whole process down with it.

**Ctrl+C was silently losing the Twitch OAuth token every restart — fixed by not depending on shutdown at all, not by fixing shutdown (2026-08-30):** `twitchio.Client.start()` calls `close()` (adapter close → EventSub websocket close → `save_tokens()`, in that order) in its own `finally` block, and relying on that to persist the token turned out to be **fundamentally unreliable** under a real Ctrl+C. Root cause: a Task that's mid-unwind from its own `CancelledError` stays flagged "cancelling" for the *entire* unwind — so any further `await` inside that `finally` block (i.e. every await inside `Client.close()`, including the one that reaches `save_tokens()`) can get silently re-interrupted by that same pending cancellation. Nothing gets logged when this happens: a cancelled task ending in `CancelledError` is asyncio's normal/expected outcome, not an error worth logging, so the failure was invisible — the log would show the adapter-close line but never the token-save line, no exception anywhere, and the file stayed empty.

Three increasingly careful shutdown-sequence rewrites in `main.py` (cancel-then-close, close-then-cancel, a shielded cleanup task run before cancelling anything) were each tried and each still failed the same way, or introduced their own new symptom (an `aiohttp` `ClientResponse.__del__` / "Event loop is closed" warning from a later attempt). None of it was worth keeping: **`main.py`'s shutdown handling has been reverted back to plain `asyncio.gather()` with no custom `KeyboardInterrupt`/`CancelledError` handling at all** — Ctrl+C may still print something to the console, and that's accepted as cosmetic. The actual fix lives entirely in `bot/twitch_bot.py`: `DarwinTwitchBot.event_oauth_authorized()` calls `await self.save_tokens()` immediately upon receiving a token, during completely normal execution, nowhere near shutdown. By the time any Ctrl+C happens the token is already on disk, so how cleanly (or not) the rest of shutdown goes no longer matters for correctness — which is why fighting the shutdown path further wasn't worth it.

Three increasingly careful fixes to the shutdown sequence in `main.py` were tried and each still failed the same way under a real Ctrl+C: cancel the bot tasks and let their own `start()`-finally trigger `close()`; cancel first, then also call `close()` explicitly (raced — whichever fired second just hit the `self._has_closed` guard and no-opped); run the explicit close as a shielded, freshly-created task before cancelling anything (still lost the token in testing). Rather than keep chasing asyncio cancellation semantics, **the actual fix is architectural**: `DarwinTwitchBot.event_oauth_authorized()` now calls `await self.save_tokens()` immediately, right when a token is granted — during completely normal execution, nowhere near shutdown or cancellation. By the time any Ctrl+C happens, the token is already safely on disk, so how cleanly the rest of shutdown goes no longer matters for correctness. `main.py`'s shutdown sequence was simplified back down accordingly (cancel tasks → await them with `return_exceptions=True` → best-effort `close()` on both bots, logged but not treated as critical if it errors).

Fix, in `main.py`'s `_run()`: don't let `asyncio.run()`'s automatic mass-cancellation touch this at all. `KeyboardInterrupt`/`asyncio.CancelledError` is caught directly around `await asyncio.gather(*tasks)` inside `_run()` itself (not left to propagate out to `asyncio.run()`'s own handling), then both bots are closed **explicitly and sequentially** in a `finally` block — `twitch_bot.close()` then `discord_bot.close()`, each awaited to completion in a plain, uncancelled context — before any leftover tasks get cancelled. `main()` also wraps `asyncio.run(_run(...))` in `try/except KeyboardInterrupt` as a final backstop so nothing unhandled reaches the console. Both `Client.close()` implementations are idempotent (`if self._has_closed: return`), so calling them here even after a bot already crashed on its own is harmless.

**EventSub chat subscription must tolerate "not authorized yet" (2026-08-30 fix):** `setup_hook()` no longer subscribes to `ChatMessageSubscription` directly — it calls `_subscribe_chat()`, which wraps the subscription in a try/except and just logs a warning (with the exact authorize URL to visit) if it fails, rather than raising. `event_oauth_authorized()` is overridden to call `super().event_oauth_authorized(payload)` (the default token-registration behavior) followed by another `_subscribe_chat()` attempt — so a fresh install's first `setup_hook()` call harmlessly 403s (no token yet), and the subscription completes itself automatically the moment the one-time browser authorization succeeds, with no restart needed.

**Config keys** (`twitch_enabled` gates everything, default `false`; `main.py`'s `validate_config()` requires the other four only when it's `true`):
| Key | Value |
|---|---|
| `twitch_enabled` | `false` by default |
| `twitch_client_id` / `twitch_client_secret` | From a Twitch Developer Console app registration (dev.twitch.tv/console/apps) |
| `twitch_bot_id` | Numeric Twitch user ID of the bot account (not the username) |
| `twitch_owner_id` | Numeric Twitch user ID of the broadcaster/channel the bot posts to (not the username) |
| `twitch_command_prefix` | `"!"` |
| `twitch_ads_enabled` | `false` by default — see Ad break automation below |
| `twitch_ad_break_seconds` | `180` — length of each triggered ad break, max 180 (Twitch caps it there regardless) |
| `twitch_pov_reward_title` | `"Change POV"` — must exactly match (case-insensitive) a custom reward's title in the Twitch Creator Dashboard, see Channel points POV reward below |

**Ad break automation (2026-09-04):** `DarwinTwitchBot.start_ad_break(length)` (`bot/twitch_bot.py`) calls TwitchIO's `PartialUser.start_commercial()` (Twitch's Start Commercial/Helix ads API) on the broadcaster's channel. `DirectorCog._twitch_start_ad_break()` fires it at match end — right after the `MATCH_ENDED` transition, at both match-end call sites (`/start`'s normal completion and the auto-start watcher) — specifically *before* the MAIN MENU click and the mirror/ingest work, so the ad has the longest possible runway to play out during the natural downtime while the next match spins up, rather than being delayed behind that other post-match work. It's fired via `asyncio.ensure_future` (not awaited), matching the fire-and-forget convention used everywhere else in this integration (`announce()`, ds_ingest, OBS) — a slow or failed ad call never blocks the match-end flow. Gated by `twitch_ads_enabled` (default `false`) and a no-op if `self.bot.twitch_bot` is `None` (i.e. `twitch_enabled: false`).

Requires **Affiliate or Partner status** (confirmed available the moment Affiliate is granted) and the channel must be **live** at the time of the call — Twitch's own restriction, not this bot's. Twitch also enforces its own cooldown between ad breaks; `CommercialStart.retry_after` (seconds until the next one is allowed) is logged, and a call attempted before that elapses just logs Twitch's rejection message rather than erroring — same never-raise pattern as `start_stream()`/`stop_stream()` in `game/obs_control.py`.

**Sub/gift-sub/resub/cheer shoutouts (2026-09-04):** `DarwinTwitchBot._subscribe_events()` subscribes to `channel.subscribe`, `channel.subscription.gift`, `channel.subscription.message`, and `channel.cheer` (alongside the existing chat subscription in `_subscribe_chat()` — each subscribes independently with its own try/except, so a not-yet-granted scope for one doesn't block the others). Each event handler (`event_subscription`, `event_subscription_gift`, `event_subscription_message`, `event_cheer`) routes through a shared `_shoutout(text)` helper that posts the message both to Twitch chat (`announce()`) *and* through the game's TTS (`tts.speak_cable()` — CABLE Input directly, no G-press/broadcast-window needed, so it's audible whether or not the in-game megaphone happens to be open). `channel.subscribe` fires for gift recipients too (`payload.gift == True`); `event_subscription` explicitly skips those so the gift isn't double-announced once per recipient and once for the whole batch via `event_subscription_gift`. Resubs are **not** included in `channel.subscribe` (Twitch's own behavior) — they're announced via `channel.subscription.message` instead, which also carries `cumulative_months`.

**Channel points "Change POV" reward (2026-09-04):** `event_custom_redemption_add()` handles `channel.channel_points_custom_reward_redemption.add`, subscribed with no `reward_id` filter (so it fires for *every* custom reward redeemed on the channel) and checks `payload.reward.title` against `twitch_pov_reward_title` (config, default `"Change POV"`) itself — any other reward is ignored entirely. **The reward must be created manually in the Twitch Creator Dashboard** (Viewer Rewards → Manage Rewards) with a title matching that config value and **"Require viewer to enter text" enabled**, so `payload.user_input` carries the player number — same `1`-`9`/`0` choices as `/pov` and `!pov`. Redemptions default to `UNFULFILLED` and sit in the dashboard queue forever unless explicitly updated, so this always resolves one of two ways: `payload.fulfill(token_for=...)` on a successful `press_key()` call, or `payload.refund(token_for=...)` (points returned to the viewer) if the input was invalid, the game state doesn't allow POV changes right now, or the keystroke send failed — both require `channel:manage:redemptions`.

**Redemption text is parsed, not exact-matched (2026-09-07):** `event_custom_redemption_add()` runs `payload.user_input` through `_extract_pov_key()` instead of checking it against `_VALID_POV_KEYS` directly. Found live — viewers used to typing chat commands kept typing `!pov 5` or `pov 5` into the redemption's text box instead of the bare `5` the reward asks for, and those got refunded as "invalid" even though the intent was clear. `_extract_pov_key()` strips an optional leading `!`/`pov` prefix (`_POV_PREFIX_RE = re.compile(r"^!?\s*pov\s*", re.IGNORECASE)`, case-insensitive, with or without a space before the digit) before checking the remainder against `_VALID_POV_KEYS`, so `"5"`, `"!pov 5"`, `"pov 5"`, and `"POV5"` all resolve to `"5"`; anything that still doesn't land on a valid key (typos, extra words, empty input) returns `None` and is refunded exactly as before — no channel points are consumed for a redemption that doesn't resolve. Scoped to the channel-points path only: `!pov` in chat is already split into command + argument by twitchio itself and never touches this parser.

**Global cooldown for viewer redemptions, mods/broadcaster bypass (2026-09-04, lowered to 30s 2026-09-07):** `event_custom_redemption_add()` rejects (refunds) a redemption from a non-privileged viewer if it comes within `_POV_REDEMPTION_COOLDOWN_SECONDS` (30s) of the last one that actually changed POV — a plain module-level cooldown, not per-viewer, so it caps how often POV can flip regardless of who's spamming the reward. `_is_mod_or_broadcaster(user_id)` decides privilege: the broadcaster always passes (`user_id == owner_id`), otherwise it calls `PartialUser.fetch_moderators(user_ids=[user_id])` (requires the new `moderation:read` scope, added to `_OAUTH_SCOPES` — needs the same one-time re-authorization as every other scope addition here) and treats any lookup failure as **not privileged**, so the cooldown fails safe (stays enforced) rather than silently opening up if the scope isn't granted yet. Privileged users skip the cooldown check entirely — this is deliberately enforced in bot code rather than via the reward's own Twitch-side cooldown setting, since Twitch's built-in per-reward cooldown applies to everyone with no concept of a mod bypass. The cooldown only arms after a redemption actually sends a successful keystroke (`sent and not is_privileged`) — a failed send or invalid input doesn't burn the window. None of this touches `/pov` or `!pov`: both remain mod/admin-gated with **no** cooldown, exactly as before — this cooldown exists solely on the channel-points path, which is the one open to arbitrary viewers.

**One-time setup still needed before this can go live** (not something Claude Code can do — requires the account owner's own Twitch login):
1. Register an app at the Twitch Developer Console to get `twitch_client_id`/`twitch_client_secret`.
2. Resolve the bot account's and broadcaster's usernames to numeric user IDs (Twitch Helix `GET /helix/users?login=<name>`) for `twitch_bot_id`/`twitch_owner_id`.
3. Set `twitch_enabled: true` and start the bot. TwitchIO 3.x ships a **built-in OAuth web adapter** (`Bot.start(with_adapter=True)`, the default) that handles the authorization flow itself — no custom OAuth server was written for this. The first run requires visiting the adapter's local authorization URL once to grant the token(s).
4. After that first authorization, tokens persist to `.tio.tokens.json` in the repo root (gitignored) and TwitchIO auto-refreshes them on subsequent runs — no repeat setup needed unless that file is deleted or scopes change.
5. **All scopes this bot ever requests are combined into one constant**, `_OAUTH_SCOPES` in `bot/twitch_bot.py` (`_OAUTH_URL` is the full authorize URL built from it) — currently `user:bot`, `user:read:chat`, `user:write:chat`, `channel:bot` (chat), `channel:edit:commercial` (ads), `channel:read:subscriptions` (sub/resub/gift-sub shoutouts), `bits:read` (cheer shoutouts), `channel:read:redemptions` + `channel:manage:redemptions` (the channel-points POV reward), `moderation:read` (checking whether a channel-points redeemer is a mod, for the cooldown-bypass logic above). Visiting `_OAUTH_URL` (`http://localhost:4343/oauth?scopes=...`, logged automatically whenever any subscription attempt fails from a missing scope) grants all of them in one authorization — if the bot account and broadcaster are the same Twitch account (the current setup), one grant covers everything. Adding a new scope-gated feature later: add the scope to `_OAUTH_SCOPES` and re-authorize once; every existing grant stays valid, nothing needs to be redone.
6. For the channel-points reward specifically, also create the "Change POV" custom reward itself in the Twitch Creator Dashboard (see above) — the bot only *listens* for redemptions of rewards that already exist there, it doesn't create them.
5. **To enable ad breaks**, the broadcaster's token additionally needs the `channel:edit:commercial` scope, which is not part of the original grant above — re-authorize once via `http://localhost:4343/oauth?scopes=user:bot+user:read:chat+user:write:chat+channel:bot+channel:edit:commercial` (adds the scope to the existing grant; the bot must be running so the adapter's local `/oauth` route is up). Then set `twitch_ads_enabled: true` in `config.json` and restart the bot.

### Scrim Signup System (`ScrimCog`)

Manages a static signup message in the configured channel. On every `on_ready`, the bot checks whether the tracked message still exists — if deleted or first run, it posts a new embed and saves the message ID to `config.json`.

**Reaction flow:**
- Players react with ✅ to sign up
- `_reactors()` excludes the bot's own seed reaction (both by `.bot` flag and explicit ID match — belt and suspenders) so the queue count reflects real players only
- When the real reactor count reaches `scrim_min_players` (default 8), `scrim_admin_role` is pinged in the **`ai-director`** channel (ID `1520518111089000548`) — not the signup channel itself
- A 1-hour reset countdown starts the moment the **first** real reactor joins an empty queue (`_start_reset_timer()`, triggered from `on_raw_reaction_add` when count hits 1) — this replaced the old "clear at the top of every wall-clock hour" behavior, so someone reacting at :59 doesn't get cut off a minute later. The timer is cancelled once the queue is fully empty across **both** lobbies — checked after every individual reaction removal in `on_raw_reaction_remove`, whether that removal came from a manual un-react, the countdown itself, or `/role remove` (see below) — so a stale countdown from an old batch of reactors can never fire against a fresh one.
- When the countdown fires (`_do_reaction_reset()`), it's skipped entirely if anyone currently holds `scrim_player_role` **or** `scrim_player_role_2` — a scrim is underway (either lobby) and players shouldn't get reset/pinged mid-match. Otherwise it clears reactions and pings removed players in channel ID `1520509256678506737` to re-sign up.
- Reaction clearing (the timer and `/role remove`) removes each real reactor's reaction **individually** (`message.remove_reaction`) rather than via a bulk `clear_reactions()` + re-add. Bulk clears appeared to leave stale names in some Discord clients' "who reacted" hover list; individual removals go through the same `MESSAGE_REACTION_REMOVE` event path used for a normal manual un-react. The bot's own seed reaction is never touched, so it doesn't need to be re-added.
- **True signup order is tracked live (2026-08-30), separately from `_reactors()`:** `ScrimCog._signup_order` is a plain in-memory list of user IDs, appended to in `on_raw_reaction_add` (and pruned in `on_raw_reaction_remove` — a leave-then-rejoin goes to the back of the queue, not back to their old spot) as reactions actually happen. This exists because Discord's `reaction.users()` iteration order — what `_reactors()` returns — is **not documented or guaranteed to match signup order**, which matters once `/role add` has to decide who's in the first 10 vs. the next 10. `_ordered_reactors()` builds the real list by walking `_signup_order` and filtering to whoever's still actually reacted (via `_reactors()`), then appends any current reactor missing from that tracking (e.g. after a bot restart, since `_signup_order` is memory-only like the reset timer above) in `_reactors()`'s own order as a best-effort fallback. `/role add` uses `_ordered_reactors()`; everywhere else that only needs a count or "is anyone signed up" still uses the plain `_reactors()`.
- **Bot restart caveat:** the reset countdown lives in memory only. If the bot restarts while a queue already has reactors, `_resume_reset_timer_if_needed()` starts a fresh full hour from restart time rather than knowing how long the queue had already been open — a queue that was 55 minutes old right before a restart effectively gets renewed. `_signup_order` has the identical caveat (see above).

**Commands (scrim admin role required, i.e. `discord_required_role` — see merge note above):**
| Command | Description |
|---|---|
| `/role add [region]` | Gives `scrim_player_role` to the first 10 reactors (by actual signup order — see above) and, if `scrim_player_role_2` is configured, `scrim_player_role_2` to the next 10 — see "Two-lobby overflow" and "Region filter" below. The region breakdown is a standing message below the signup message, not part of this response — see below. Response is public (not ephemeral). |
| `/role remove <lobby>` | **Required `lobby` choice, `1` or `2`.** Removes only that lobby's role (`scrim_player_role` for `1`, `scrim_player_role_2` for `2`) from everyone who currently has it, then deletes and reposts the signup message — see below, this resets **both** lobbies' reactions, not just the targeted one. Response is public (not ephemeral). |

Both commands' permission-denial message ("You don't have permission...") remains ephemeral — only the successful-result messages were made public.

**`/role add`'s `region` parameter (2026-09-07):** an optional `app_commands.Choice[str]` — `MIX` (default, omitting the parameter is the same as choosing it), `NA`, or `EU`. `MIX` is the original behavior unchanged: lobby 1 = the first 10 reactors in raw signup order. `NA`/`EU` instead build lobby 1 from `[u for u in reactors if region_role in u.roles][:10]` — still walking reactors in signup order, just skipping anyone who doesn't hold the matching `region_role_na`/`region_role_eu` role (same config keys and role lookup as the region breakdown table). Only lobby 1's *selection* is filtered — lobby 2 (and the overflow-beyond-20 case) are computed from whoever's left over after lobby 1 is pulled out (`remaining = [u for u in reactors if u not in first_lobby]`), in original signup order, with no region filter of their own; a region-matched player who didn't make the first 10 goes back into that same remaining pool alongside everyone else; not double-lobby-filtered on request. If the requested region's role isn't found in the guild, or literally no reactor holds it, the command replies with an error and assigns nothing — same fail-closed pattern as the existing "role not found" checks earlier in this command.

**`/role remove`'s role removal is lobby-scoped; its reaction reset is not (2026-08-30, reaction handling revised 2026-09-04):** it snapshots `role.members` for whichever lobby was requested *before* removing the role (since `role.members` would shrink as members are processed), removes the role from exactly those members — that part stays lobby-isolated. The reaction side went through three iterations:
1. *Original:* removed each of that lobby's reactions one-by-one via `_clear_real_reactions()`, leaving the other lobby's reactions untouched. Left a race window — many sequential awaits during which a player re-reacting mid-loop, or an individual removal silently failing under Discord rate limits, could leave a stale checkmark behind.
2. *Bulk clear attempt:* switched to `message.clear_reaction(emoji)` + re-`add_reaction()` (two atomic calls, no race window) — but since both lobbies share one emoji on one message, this necessarily wiped both lobbies' signups at once, and `clear_reaction()`'s `MESSAGE_REACTION_REMOVE_EMOJI` event (vs. a per-user `MESSAGE_REACTION_REMOVE`) left some Discord clients showing stale names in the "who reacted" hover list — server-side state was correct (`_reactors()` always hits the API fresh), but the display lagged.
3. **Current:** `_delete_and_repost_signup_message()` deletes the tracked signup message and calls `_ensure_signup_message()` to post a fresh one (reusing the same "tracked message was deleted, post a new one" path that already runs on every `on_ready`) — no per-user loop to race, and a brand-new message can't carry any stale reaction cache. Trade-off: a new message appears in the channel (with a new tracked ID, auto-persisted) every time `/role remove` runs, and — same as step 2 — this always resets **both** lobbies' signups regardless of which `lobby=` was targeted, so `role_remove()` warns the admin in its response if the other lobby still has active players.

No raw reaction events fire for a deleted message, so `_signup_order` and the reset timer are reset manually inside `_delete_and_repost_signup_message()` rather than relying on `on_raw_reaction_remove`'s usual bookkeeping.

**Discord API calls in the signup-message lifecycle are timeout-wrapped (2026-09-04 fix):** found live — a `/role remove` deleted the old message fine, but the immediately-following `ch.send(embed=embed)` inside `_ensure_signup_message()` hung on a stalled/dead connection for **7+ minutes**, with no exception and no timeout, leaving the Discord interaction stuck "thinking" the whole time (confirmed via `darwin_bot.log`: the delete's `NotFound` warning logged instantly, then total silence until the bot's gateway session was invalidated/reconnected — the request itself finally completed right around when that happened). None of `fetch_channel`/`fetch_message`/`send`/`add_reaction`/`delete` had a timeout, so any one of them stalling indefinitely could reproduce this. `_get_signup_message()`, `_ensure_signup_message()`, and `_delete_and_repost_signup_message()` now wrap every Discord API await in `asyncio.wait_for(..., timeout=_DISCORD_API_TIMEOUT_SECONDS)` (15s) — a stall now fails fast with a logged warning (and, for the "post new message" step, a clean early return) instead of hanging the command indefinitely. Same philosophy as the game-automation timeouts (`_LAUNCH_TIMEOUT`, `_CUSTOM_TIMEOUT`, `_MATCH_TIMEOUT`) applied to Discord's own API for the first time in this codebase.

**Live region-breakdown message below the signup message, not a `/role add` reply (2026-09-04, moved to a standing message 2026-09-09):** originally `_build_region_table()` appended a monospace table (Player / NA / EU columns, `X`/`-` per cell) to `/role add`'s response, covering only whoever that invocation had just assigned. It's now a standing message of its own — `_ensure_region_message()` posts it directly below the signup message (same channel, `scrim_signup_channel_id`) the first time both exist, and it tracks **every current signup reactor**, not just role-add's picks, so it reflects reality even before `/role add` has ever run. `_region_table_lines(guild, members)` builds the same Player/NA/EU rows as before (role names from `region_role_na`/`region_role_eu`, config default `"NA"`/`"EU"`, looked up fresh via `discord.utils.get(guild.roles, ...)` — a role rename takes effect immediately) and returns `None` if neither role exists in the guild, in which case no region message is created/updated at all — same "nothing to show" case the old table returned `""` for. `_region_breakdown_embed()` wraps those rows in a small embed (`_COLOR_NEUTRAL`); `/role add`'s response no longer includes any region info of its own.

**Auto-updates on every signup add/remove:** `on_raw_reaction_add`/`on_raw_reaction_remove` both call `_update_region_message(guild, reactors)` right after recomputing `_reactors(message)` — it edits the tracked message in place (`msg.edit(embed=...)`, not delete+repost, since this message carries no reactions of its own to worry about stale-caching) so the table always reflects who's actually signed up as of the last reaction event. If the tracked message doesn't exist yet (region roles weren't in the guild at startup) `_update_region_message` calls `_ensure_region_message()` instead, so the message self-heals into existence the moment both region roles exist and someone next reacts/unreacts — no restart needed. `on_ready()` also force-refreshes it once at startup (reactions can happen while the bot is offline) and `_delete_and_repost_signup_message()` (the `/role remove` reset path) deletes and reposts it alongside the signup message it tracks, exactly like the signup message itself — same reasoning: a brand-new message can't carry stale content, and there's no per-reaction loop to race since this message has no reactions to clear. Persisted as `scrim_region_message_id` (config, auto-persisted like `scrim_signup_message_id` — `_save_message_id(key, id)` is now the shared, key-parameterized helper both message ids go through).

**Two-lobby overflow (2026-08-30):** `scrim_player_role_2` is optional. `/role add` always takes `reactors[:10]` for the primary role; only when `scrim_player_role_2` is configured does it also take `reactors[10:20]` for the second role. If more than 10 reacted and `scrim_player_role_2` is unset, the overflow beyond 10 is left unassigned and the response says so (same behavior as before this feature existed — nothing changes unless you set the key). `_match_in_progress()` (used to gate the 1-hour reset countdown) checks both roles, so an active second lobby also blocks the reset from firing. Note: the scrim-roster capture passed to the ladder ingest API (`DirectorCog._resolved_roster`, see Ladder Ingestion section) still caps at `reactors[:10]` (plain, not ordered) and was not changed — it doesn't currently account for a second lobby's players.

**Config keys:**
| Key | Value |
|---|---|
| `scrim_signup_channel_id` | `1520517054988419123` |
| `scrim_signup_message_id` | Auto-persisted — do not edit manually |
| `scrim_region_message_id` | Auto-persisted — do not edit manually. The live region-breakdown message below the signup message, see above |
| `scrim_player_role` | `PC Scrim Player` |
| `scrim_player_role_2` | `PC Scrim Player 2` — optional; second-lobby role for signups 11-20, see "Two-lobby overflow" above |
| `scrim_admin_role` | `PC Scrim Admin` (same value as `discord_required_role`) |
| `scrim_min_players` | `8` |
| `scrim_reaction_emoji` | `✅` |
| `region_role_na` / `region_role_eu` | `"NA"` / `"EU"` — Discord role names checked by the live region-breakdown message below the signup message |

Two channel IDs are hardcoded constants in `discord_bot.py` rather than config keys: `_AI_DIRECTOR_CHANNEL_ID` (queue-full ping) and the notify channel used in `_do_reaction_reset` (`1520509256678506737`, removed-players re-signup ping). Both were confirmed to belong to the current guild (`480566249609232389`) after the server migration — if the guild ever changes again, these need updating in code, not config.

### Match Runner (`game/match_runner.py`)

`MatchRunner.run()` is the full match sequence, called via `run_in_executor` from `/start`:

1. Press B to start match (via PostMessage to Darwin hwnd — focus-independent)
2. 5-second sync delay
3. Press `1` to default the Director's camera to player 1's POV (2026-08-31) — same key as `/pov`/`!pov`'s "1" choice, via the same `_press()` helper used for the B press above. Fires right before the profile announcement below, so viewers see live gameplay from the start of the match without needing a mod to run `/pov` or `!pov` first.
4. Announce active profile name via TTS: *"Using profile: [name]"*
5. Start `VideoRecorder` in background thread
6. Main loop (wrapped in `try/finally` to guarantee recorder finalization):
   - Sleep until the next card trigger time (capped at `screen_poll_interval_seconds`) — cards fire within ~0.1s of scheduled time
   - Fire card events: check director points first, wait if insufficient, then shift-drag
   - Every 30s: sample zone pixels → `valid_closeable_zones()` → shuffle all OPEN zones → try each until one verifies
   - Every `screen_poll_interval_seconds`: double-confirm match end (two detections 2s apart, threshold 0.88)
7. Stop recorder (`finally` block — runs on normal end, force-stop, and exceptions)
8. Take screenshot of results screen (`_capture_results()`, inside `MatchRunner`) → returns `(screenshot_path, recording_path)` to `discord_bot.py`
9. **Click MAIN MENU button on results screen → wait for main menu → transition to `IN_MENU`** (2026-08-31: moved here, right after the screenshot, instead of after the Discord/API step below) — the game doesn't need to sit on the results screen waiting on Discord/network calls before moving on; the screenshot is already captured by this point, so nothing below depends on the results screen still being up
10. Mirror the screenshot to the `results` channel via `_mirror_results()` (not posted in the invoking channel) → push to ladder ingest → delete local screenshot file
11. Fire background upload of recording via `_upload_recording()` — see Video Recorder section for what this actually does today. This one is *not awaited* (`asyncio.ensure_future`), so it genuinely races with step 10 rather than running strictly after it; every other step here is sequential.

`MatchRunner.stop()` sets a `threading.Event` that the loop checks between every action. Called by `/quit`.

`run()` returns a tuple `(results_text, recording_path)`. The discord bot unpacks this; plain string returns (e.g. force-stop) are handled via `isinstance(result, tuple)` guard.

**Director points reading (`_read_points`):**
- Primary: count filled pips by brightness (`max(B,G,R) > 130`) — immune to color/size changes
- Always use `pip_count - 1` as conservative reading (guards against partially-filled pip)
- If OCR count == pip_count (conservative + 1), the last pip is fully filled — trust OCR
- On any other disagreement, use `min(pip_count, ocr_count)` — **not** pip-1 unconditionally (2026-09-04 fix, see below)
- Fallback to OCR alone if pip config missing; no-op if neither configured
- Config: `director_points_pips: {"x_start": 862, "y": 1012, "spacing": 26, "count": 10}`
- Config: `director_points_region: [808, 1002, 20, 24]` — calibrated to the 2-digit numerator only (not the `/10`); x=808 skips background/tree pixels on the left, w=20 excludes the slash and denominator. OCR uses 4× upscale + Otsu auto-threshold + PSM 8 (single word) for best accuracy.

**Pip reading disabled by default — OCR-only, pips kept dormant (2026-09-04):** the pip check is a *single pixel* per pip (`count_director_point_pips` in `game/ocr.py`) with pure brightness thresholding, no hue check and no averaging — a bright background element landing on that exact coordinate reads as a filled pip regardless of its actual color, with no cross-pip voting to catch it (unlike the multi-point majority-vote pattern zone-color detection uses). This was overcounting points in practice, causing cards to be attempted before the Director actually had enough, and the failed play. Two changes:
1. `_read_points`'s disagreement handling was changed from unconditionally trusting `pip_count` (documented as "the safer value") to `min(pip_count, ocr_count)` — that assumption only holds if pip errors bias low, but background-bleed overcounting biases high, so blindly trusting pips was actively wrong in exactly this failure mode.
2. A new toggle, **`director_points_use_pips`** (config, default `true`, currently set to `false` in this repo's `config.json`) gates the pip path in `_read_points` entirely — when `false`, `pips_cfg` is forced to `None` regardless of whether `director_points_pips` is configured, so `_read_points` falls through to OCR alone. `count_director_point_pips`, the `director_points_pips` calibration values, and the whole pip/OCR reconciliation logic are all left in place (same "disconnected, not deleted" precedent as zone_close's legacy per-zone logic) — flipping the toggle back to `true` re-enables pip reading with zero recalibration needed. If pip reading is ever revisited, the leading candidate improvement is multi-pixel patch voting per pip (sample a small area and require a majority bright, not just one pixel) rather than the current single-pixel sample.

**`_wait_for_points` no longer fails open on a bad OCR read (2026-09-06 fix, extended same day to continuous sampling):** found live — `_wait_for_points()` used to return immediately (treating points as sufficient) the moment a single `_read_points()` call returned `None`, and the card would then get attempted without actually having enough points. It's now backed by a single shared, match-wide value, `self._last_confirmed_points`, merged via `_update_points_reading()`'s ratchet-up guard: a `None` read or one that comes back *lower* than the last confirmed value is disregarded as a misread rather than trusted — the confirmed value only ever moves up from an actual higher reading, never down on its own. "Ready" is only ever declared off that confirmed value, never off a guess.

Points only genuinely decrease when this bot itself plays a card, so that path (`_fire_card_event`'s tail in `_play_tray_card()`, and `_attempt_zone_close()`) explicitly sets `self._last_confirmed_points = None` right after every play attempt (successful or not) — invalidating the pre-play value so the next read, whatever it comes back as, becomes the new trusted baseline instead of being compared against a now-stale higher one.

`self._last_confirmed_points` is sampled continuously throughout the match, not just reactively when a card is about to fire: the main loop in `run()` reads points once every `screen_poll_interval_seconds` (same cadence as its other periodic checks — match-end polling, first-blood polling) via the same `_update_points_reading()` path, gated by `self._last_points_sample_time`. This means a card checking affordability usually already has a recent confirmed value on hand instead of needing a fresh, possibly-failing read at the exact moment it fires — more snapshots over the course of the match means more chances to catch a valid read. Cost is one extra screenshot + a small OCR crop every `screen_poll_interval_seconds` (~tens of ms), the same order of magnitude as the loop's existing per-iteration screen checks.

Trade-off, unchanged from the original fix: if OCR is broken for the rest of the match (never produces a single valid read again, from either the background sampling or a card's own wait), that card — and every card after it, since they fire sequentially — will simply never fire rather than firing blind. That's the deliberate choice here (never misplay for lack of confirmed points), not an oversight.

**`read_director_points`'s failed-parse log downgraded to debug (2026-09-09):** `game/ocr.py` used to log `logger.warning("Could not parse director points from OCR text: ...")` on every OCR miss — harmless on its own, but once points started being sampled continuously every `screen_poll_interval_seconds` (above), a normal, expected miss now fires many times per match, cluttering the log at a level that's supposed to mean "look at this." `_update_points_reading()` already treats a `None` read as ordinary noise and logs it at `debug` (see its own docstring) — this line was the one remaining inconsistency, now matching.

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

### Zone Logic (`zones/`) — **dormant since 2026-08-30, see below**

7-zone hex grid with fixed adjacency. `valid_closeable_zones()` returns all zones currently in `OPEN` state — no connectivity filtering. The bot shuffles the list and tries each zone in order, relying on the verification step (slot pixel delta check) to detect game rejections rather than pre-filtering.

The connectivity logic (`can_close_zone`, `open_zones_stay_connected`, BFS) has been removed. `neighbor_count()` is kept for the strategy classes which use it for weighting.

**Zone close flow in `_attempt_zone_close_legacy()`** (not called — see below for the live path):
- Grab zone_close card → read zone states from screenshot → `valid_closeable_zones()` → `random.shuffle()` → try each until slot pixel verifies or list exhausted
- The game itself enforces any rules about which zones can actually be closed

Zone strategy is pluggable via `config.json → zone_selection_strategy` but the live path currently ignores the strategy and uses random shuffle directly. Adding a new strategy: create a file in `zones/strategies/`, subclass `BaseZoneStrategy`, add to `STRATEGIES` dict in `strategy_factory.py`.

**Superseded by the static drop-area approach (2026-08-30):** a new in-game drop area resolves zone_close to a random valid zone on the game's own side, so none of the machinery on this page runs in a live match anymore — `MatchRunner._attempt_zone_close()` now just drags the card to one static point (`_ZONE_CLOSE_DROP_TARGET`, a hardcoded constant — moved out of config.json 2026-09-07) and marks it played, no zone-map read, no `ZoneState` tracking, no strategy selection, and (per an explicit choice) no tray-pixel verification either — regardless of `verify_card_plays`, since the drop area makes "did it pick a valid zone" the game's problem, not the bot's. Everything described in this section — `zones/`, `_zone_states`, `valid_closeable_zones`, `zone_selection_strategy`, `_ZONE_MAP_SAMPLE_POINTS`, `zone_color_thresholds` — is left in the codebase **disconnected, not deleted** (same treatment as `/deck`, see below), preserved as `MatchRunner._attempt_zone_close_legacy()` / `_attempt_zone_close_bypass()` / `_update_zone_states_from_screenshot()` / `_vote_zone_state()`, in case the new drop area needs to be rolled back. `_ZONE_CLOSE_DROP_TARGET` is a single `(x, y)` coordinate — calibrated to `(1750, 1000)` (2026-08-30), inside the "SPECTATORS — LET THEM DECIDE" cyan corner triangle at 1920×1080. That triangle's top edge is diagonal and its label text/robot icon break up the color in places (e.g. white text glyphs, the mech icon in the upper-left of the shape) — `y ≥ 975` is solid cyan across the full `x` range in that corner with no such gaps, which is why the calibrated point sits there rather than nearer the diagonal edge.

**No "Closing a zone" TTS line (2026-09-06):** `_attempt_zone_close()` no longer speaks a post-drag confirmation. Since this drag is never verified (see above — no tray-pixel check regardless of `verify_card_plays`), that line always spoke as if the zone close had been confirmed even though nothing was actually checked, the same "implicit success" mismatch `verify_card_plays: false` created for tray cards. "Deploying Zone Close" (spoken before the drag) still announces the attempt; nothing announces the outcome now.

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

> **Note:** `noble-hopper/` is gitignored — it is not tracked in this repo. `noble-hopper/state.json` contains captured auth tokens and must never be committed.
>
> **Decoupled from bot startup (2026-08-07):** `main.py` no longer spawns or manages the noble-hopper process — it used to launch `noble-hopper/launcher.py` as a subprocess on startup and kill it via `atexit`, but that coupling was removed so noble-hopper can be run, restarted, and eventually retired independently of the bot. Run it manually (`python launcher.py` from inside `noble-hopper/`) whenever deck-sync features are needed. If it isn't running, deck sync degrades gracefully — `_try_force_sync()` catches the connection failure and logs a warning, and `/deck` still reads/writes `state.json` directly (it just won't push to the live game until noble-hopper is up and the game re-requests its profile).


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
3. `_update_zone_states_from_screenshot()` — votes across 4 `_ZONE_MAP_SAMPLE_POINTS` per tile; majority wins
4. `valid_closeable_zones()` → pick zone (live path uses random shuffle across valid zones, not the configured strategy — intentional for now)
5. `complete_drag(keep_shift=True)` to target zone → after-screenshot → verify slot pixel changed → `shift_up()`, or `mouseUp()+shift_up()` if nothing closeable
In bypass mode, uses cached `_zone_states` (all OPEN initially) and calls `play_card(bypass_mode=True)`.

**Zone_close slot verification threshold — 80, not 16:**
The inline pixel delta check in `_attempt_zone_close` uses `delta > 80` (not the global threshold used by `_verify_card_removed`). This is intentional.

The zone_close card's tray position varies by profile. In the Blood profile (custom_a), beach_party is never played, so the tray always has one extra card. This pushes zone_close from x=814 (Standard/Everything) to x=852. The game world background bleeds slightly through the card art at x=852, causing a consistent small delta of ~56-58 even when the card returns to its slot after a rejection. This is not a timing issue — it is a specific background bleed at that screen coordinate. Real plays produce delta of 240+; the false-positive bleed produces delta 56-58. Threshold 80 sits safely between them.

**`_verify_card_removed` threshold lowered 40 → 16 (2026-08-28):** `_TRAY_VERIFY_DELTA_THRESHOLD` in `match_runner.py`. Log analysis across months of matches (`logs/darwin_bot.log`) found a recurring false-negative band at delta 17-39, always on the first tray-card play of the match or the first play right after a card naturally unlocks mid-match (`Electromania at 2:30`, `Beach Party at 4:00`, `Telepathy at 4:30`/`10:00`) — the tray recenters and the sampled pixel shifts to a different-but-similar card color instead of going stark. A missed verification here never adds the card's `deck_position` to `self._deck_played`, so every later `_deck_pos_to_screen()` call computes one card-width off for the rest of the match — this is the "tray out of sync" cascade: the next card's play *and* its own verification land on the wrong slot, which usually reads near-zero delta since nothing meaningful is at that wrong pixel (this also explains why zone_close attempts sometimes fail on every zone in the same match — its slot is computed the same way). Genuine non-plays across the same log are all delta ≤ 15 (mostly 0-3, one ambiguous 10); genuine clean detections are all delta ≥ 41. 16 sits in the untouched gap between the two populations, so this only reclassifies the confirmed false-negative band and leaves every previously-correct detection unchanged.

**Zone visual states on the big map (what to calibrate against):**
- **OPEN**: plain medium blue/teal hex, no border glow
- **CLOSING**: visibly darker (navy/dim) hex, no orange border — tile darkens as lava begins but orange outline has not appeared yet
- **CLOSED**: dark red/maroon hex with a bright orange border glow at the edges

Sample points are placed at ~110px from each tile center (near the hex edge, well outside the player icon area at center). At this distance, colors are: OPEN=blue, CLOSING=dark navy, CLOSED=dark red. The orange border glow is right at the very edge and would require sampling at ~120-125px to catch; the interior color differences are sufficient for three-way distinction. Calibrate `zone_color_thresholds` using `calibrate_zone_colors.py` while holding a zone_close card with known zone states visible.

**`verify_card_plays` toggle (2026-08-30) — bypasses verification/retries without deleting them:** `config.json → verify_card_plays` (default `true`). Card plays have proven reliable enough day-to-day that the pixel-verify-and-retry safety net is no longer needed. Setting it to `false` does **not** remove any verification code — `_verify_card_removed` is untouched — it just skips calling it:
- **Tray cards** (zone-targeted, player-targeted, and plain drop-target cards): all three now funnel through one shared `MatchRunner._play_tray_card()` helper (extracted from what used to be three near-identical ~90-line blocks in `_fire_card_event`, one per card kind). `_play_tray_card` checks, in order: `self._bypass` (ahk_bypass_mode dry-run) → `not self._verify_plays` (play once, no before/after screenshots, no retry, trust it worked) → the original verify-with-up-to-2-attempts path. `self._verify_plays` is set once in `MatchRunner.__init__` from `verify_card_plays` and is a distinct concept from `ahk_bypass_mode`: bypass mode never sends real input, `verify_card_plays: false` still plays every card for real.
- **Zone close is not governed by this toggle at all** — as of the same date it has its own static drop-area mechanism (see the Zone Logic section above) that always plays once with no verification, regardless of `verify_card_plays`. The zone_close-specific `delta > 80` check mentioned above only still runs inside the disconnected `_attempt_zone_close_legacy()`.

### Video Recorder (`game/video_recorder.py`)

Records match footage in a background thread. Started after the match countdown, stopped in a `try/finally` so the file is always finalized regardless of how the match ends.

**`recording_enabled` toggle (2026-08-31), off by default for now:** `config.json → recording_enabled` (default `true` if the key is absent, but currently set to `false` in this repo's `config.json`). When `false`, `MatchRunner.run()` never constructs or starts a `VideoRecorder` at all — `recorder` stays `None`, the `finally` block skips calling `recorder.stop()`, and `recording_path` stays `None` for the rest of the match. One toggle covers both "no local recording file" and "no upload attempt" — `discord_bot.py`'s `if recording_path: asyncio.ensure_future(self._upload_recording(...))` check downstream naturally never fires when there's no file to begin with, so nothing needed to change there.

- **Format:** H.264 MP4 (`avc1`) — all three FOURCC options tested True on this machine ⚠️ **`cv2.VideoWriter.isOpened() == True` is not a reliable success signal for `avc1`** — see the OpenH264 gotcha below, where it stayed `True` while silently writing a ~1KB broken file with the real codec missing. If recordings are ever coming back empty/corrupt, check for the OpenH264 load error in console output before assuming it's a crash/try-finally issue.
- **FPS:** 4 (1 frame every 0.25 seconds)
- **Crop:** `_CROP_REGION` constant in `game/video_recorder.py`, `[755, 175, 410, 200]` (tight center band focused on the kill feed area) — moved out of config.json (2026-09-07)
- **Output:** `screenshots/recordings/match_{timestamp}.mp4` — ~60-65 MB for a 20-min match at these settings (roughly 8x the file size of the old 0.5fps setting)
- **Upload:** `_upload_recording(path)` fires as a detached async task after the match. The actual upload is still a stub (TODO — wire up when `recording_api_endpoint` is set in config). **The local recording file is deleted unconditionally at the end of `_upload_recording()` regardless of whether an upload happened** — this was an explicit choice to reclaim disk space now, accepting that until the upload is actually implemented, recordings aren't preserved anywhere once deleted.

**Safety:** process crash will leave the file corrupt (VideoWriter MOOV atom not flushed). All other exit paths (normal end, `/quit`, exceptions, asyncio timeout) are covered by the `try/finally`.

**OpenH264 DLL gotcha (Windows) (2026-08-30):** OpenCV's ffmpeg backend doesn't bundle Cisco's `libopenh264` codec — it's dynamically loaded at runtime and must be downloaded separately (H.264 patent-licensing reasons; same category of issue as the `pip-system-certs` SSL gotcha in Ladder Ingestion below). Symptom in logs: `Failed to load OpenH264 library: openh264-1.8.0-win64.dll` / `Incorrect library version loaded` / `Could not open codec libopenh264` right when a match starts (`VideoRecorder.start()`). The exact required filename/version is stated in the error itself — for this OpenCV build (opencv-python 4.13.0) it's `openh264-1.8.0-win64.dll` from Cisco's official binary host, `http://ciscobinary.openh264.org/openh264-1.8.0-win64.dll.bz2` (`.bz2`-compressed; verify the decompressed DLL's Authenticode signature is `Cisco WebEx LLC` before trusting it — the signing cert being expired is normal for a 2018-era release and doesn't invalidate a timestamped signature). **Placement matters and is not where you'd expect:** putting it next to `cv2`'s own `opencv_videoio_ffmpeg*.dll` in `site-packages/cv2/` does **not** work — that folder isn't on the DLL search path FFmpeg uses for this dependency. It has to go in **the same directory as `python.exe`** and/or **the process's current working directory** (the repo root, since that's where `main.py` runs from) — both were populated for redundancy here. Gitignored via `openh264-*.dll` since it's a machine-specific runtime binary, not source, matching the `templates/` and `noble-hopper/` precedent above.

### Ladder Ingestion (`game/ingest.py` + `game/ds_lifecycle.py`)

The bot keeps one **draft** (an unpublished set) open on the **`darwinstalker.com`** scrim ladder per lobby and drives it through four calls — open at `/custom`, roster at match start, screenshot after each game, close on every path back to IDLE. The full contract, config, file map, log lines to grep, and what the LIVE tab should show when are in **`docs/DS_LIFECYCLE_HANDOFF.md`** — read that before touching any of this. Summary:

- `game/ingest.py` — the three HTTP calls (`open_set_draft`, `close_set_draft`, `post_results_screenshot`). Fire-and-forget: log and swallow, never retry, never raise. `open_set_draft` ALWAYS sends when called (an empty roster is a legitimate "lobby forming" open); deciding whether to call is the lifecycle's job.
- `game/ds_lifecycle.py::DraftLifecycle` — pure-Python state machine holding the draft id: `open_lobby()`, `on_match_start(names)`, `post_results(png, roster)`, `close(reason)`. Takes an injectable `transport` so it is tested headlessly (`tests/test_ds_lifecycle.py`). `DirectorCog._ds` is the single instance; `MatchRunner` gets it via the `draft_lifecycle` constructor arg. **Nothing else may call `game.ingest` directly.**
- Wiring: `DirectorCog.custom` → `_open_ds_draft()` right after the `IN_CUSTOM` transition; `MatchRunner.run()` → `on_match_start_async(self._player_names)` after `_init_player_bar()` (empty OCR = WARNING, not a silent skip — that silent skip is why the Twitch embed never lit for a week); `_post_results_to_ingest` → `post_results`; `_reset_session(reason)` → `_close_ds_draft(reason)`.
- **`on_match_start` runs on a fire-and-forget daemon thread, not inline (2026-09-07 fix):** found live — `/start` was visibly delayed before the B-press, introduced by the draft-lifecycle feature landing the day before. `on_match_start()` itself stayed synchronous (`open_set_draft`'s `_OPEN_DRAFT_TIMEOUT_SECONDS = 5` in `game/ingest.py`, blocking) and is still called directly wherever that's safe (headless tests, anything already off the match thread), but `MatchRunner.run()`'s call site — which runs on the match thread right before `_press("b")` — now calls `DraftLifecycle.on_match_start_async(names)` instead: a thin wrapper that spawns a daemon thread (`threading.Thread(target=self.on_match_start, ...)`) and returns immediately, same fire-and-forget convention as `announce()`/OBS/ds_ingest elsewhere in this codebase, just on a thread instead of `asyncio.ensure_future` since `MatchRunner` isn't on the event loop. Safe because nothing at that call site used the return value, and `self._draft_id`/`self._lobby`/etc. are read elsewhere (via `event()`'s closure, `post_results()`, `snap_names()`) with enough natural delay before the next read that a brief in-flight window is harmless — no lock was added, matching this codebase's existing no-retry/no-lock fire-and-forget style. Covered by `tests/test_ds_lifecycle.py::test_match_start_async_does_not_block_the_caller` (a gated fake transport proves the call returns before the network call completes, not just that it eventually completes).
- Live match events (2026-09-06): `MatchRunner` emits `match_start`, `first_blood`, `eliminated` (the player bar is now polled ALL match, not just until first blood), `match_end`, `card_play`, `status`, `aborted`; `/say` emits `say`. Also `detector_v2`/`eliminated_v2` (shadow/v2 detector mode) and, since 2026-09-09, `slot_map` (diagnostic — see docs/PLAYER_BAR_CALIBRATION.md §8) — full field list in docs/DS_LIFECYCLE_HANDOFF.md §3a. All go through `DraftLifecycle.event()` → `game/ds_relay.py` (daemon thread, batched POSTs to `/api/ingest/events`, never blocks the match loop). Game index: 1 at `open_lobby`, +1 after each `post_results` (which flushes first), 0 at `close` (which flushes first).
- Bot log relay: `game/ds_log_handler.py::DsLogHandler` (installed on the root logger in `DirectorCog.__init__`) forwards WARNING+ from any logger and INFO from `game.ds_lifecycle`/`game.ingest` to `/api/ingest/log` → shown as `bot.*` on the ladder's `/admin/observability`. `game.ds_relay*` loggers are excluded to prevent recursion. 60/min rate limit.
- Tournament mode: `/tournament on slug:<slug>` persists `ds_ingest_tournament_slug`; `_open_ds_draft` sends it as `tournament_slug` only while `tournament_mode` is on. Unknown slug ⇒ server 400, draft not opened.
- Tests: `pip install -r requirements-dev.txt && pytest tests -q` (headless; `tests/conftest.py` stubs the screen/Windows libs). The env-gated contract test (`tests/test_contract_darwin_stalker.py`) runs the real client against a LOCAL darwin-stalker only — it creates real drafts.
- Success response of the screenshot call: `{"draft_id": ..., "game_index": ..., "ocr_error": ...}` — logged at INFO. `ocr_error: null` means the server's OCR read the scorecard cleanly.

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
- `speak(text, broadcast=True)` — queues audio; if `broadcast=True`, the worker checks cooldown, presses G, plays audio (standalone use — not used in the match loop; `/say` is the one caller, via `broadcast=in_match`).
- `try_open_broadcast()` + `speak_cable(...)` + `queue_close_broadcast()` — the correct pattern for in-match card announcements.

**`/say` profanity filter (2026-09-04):** now that OBS's `Mic/Aux` input (carrying TTS audio) is unmuted, anything spoken via `/say` goes out live on stream, not just in-game. `bot/discord_bot.py`'s `say()` checks the message against `better_profanity.profanity.contains_profanity()` before calling `tts.speak()` and rejects it with an ephemeral error (nothing spoken, nothing sent to Discord's response either) if it trips — blocking outright rather than censoring, since a partially-`****`-masked string has no reliable TTS pronunciation. `/say` is already gated by `_role_check` (`discord_required_role`), so this guards against an admin's typo/joke going out live, not against arbitrary viewer abuse — there's no viewer-facing text-input path into TTS yet (the potential channel-points "Say This" reward discussed but not built would need the same check). `better_profanity` is a new dependency (`requirements.txt`).

**Tuning the filter's strictness:** `main.py` configures the library's module-level `profanity` singleton once at startup (same pattern as `tts.configure()`/`obs_control.configure()` right above it) from two optional config keys, both empty by default:
- `tts_profanity_whitelist` — words to exempt from the library's default word list (loosens it — e.g. a mild word this stream is fine with that the default list flags).
- `tts_profanity_extra_words` — additional words/slang to always block on top of the default list (tightens it — e.g. community-specific terms not in the library's generic English list).

Both take effect for the whole process since `profanity` is a shared singleton — `bot/discord_bot.py`'s `say()` doesn't configure anything itself, it just calls `contains_profanity()` against whatever `main.py` set up.

**`broadcast` mode on cooldown now falls back to CABLE-only instead of dropping the phrase entirely (2026-08-31 fix):** in `_worker_loop()`, the `mode == "broadcast"` branch used to `continue` when `_broadcast_available_at` hadn't passed yet — silently discarding the queued phrase with no audio played at all. Found via `/say` mid-match: whenever the megaphone was on cooldown, players heard nothing, since the G-press gate was also gating the audio itself rather than just the G press. It now still calls `_speak_on_cable(text)` (no G press, CABLE Input only) in that case before `continue`-ing, so the phrase is always audible — the cooldown only ever withholds the G press/broadcast-window opening, never the audio.

**Waiting-on-points broadcast close:**
If `_wait_for_points()` needs to wait and the broadcast window is open, it queues `speak_cable("Waiting on points for X")` then `queue_close_broadcast()` immediately — the 90s cooldown starts ticking while waiting for points. When points arrive, `_fire_card_event` tries `try_open_broadcast()` again (may fail if cooldown hasn't expired).

**Pre-caching:** `precache_async(phrases)` fires a background thread that generates and caches all TTS audio via edge-tts before cards fire, so every queued phrase hits the cache instead of making a live network request.

**Discord voice mirroring:** `/voice join` connects the bot to the user's voice channel — all TTS audio then plays concurrently to both CABLE Input and the Discord channel. Set/cleared via `tts.set_voice_client(vc)`.

### OBS Twitch Streaming (`game/obs_control.py`) (2026-08-30)

Controls OBS Studio remotely via **obs-websocket** (built into OBS 28+, enabled under Tools → WebSocket Server Settings) to start/stop a Twitch stream around the automated Director session. **This does not launch or configure OBS** — OBS Studio must already be running, with its own Settings → Stream panel already pointed at Twitch with a stream key entered, and its websocket server enabled. The bot only ever sends `StartStream`/`StopStream` over the websocket — it never sees or handles the Twitch stream key itself.

**Lifecycle:**
- **Starts** when `/custom` successfully creates the lobby (`bot/discord_bot.py`, the `if lobby_code:` branch) — this is deliberately the *only* driven-lobby's session, since only one of the two scrim lobbies runs the AI Director (see the Scrim Signup System section — the second lobby is run manually and never touches this bot).
- **Stops** inside `_reset_session()`, not at individual call sites. `_reset_session()` is the one function invoked on every path back to `IDLE` — `/quit`'s confirmation (`_EndConfirmView._do_end`), the 10-minute main-menu idle-close in `_screen_watcher()` (`_IDLE_CLOSE_SECONDS = 600`, see the Discord Bot background-watcher code), and every other failure/timeout reset (`/custom` timeout, `/launch` failure, match-runner timeout, etc.). Hooking the single shared reset function means the stream reliably stops on the two triggers that were asked for (`/quit`, 10-min idle) plus every other path that already resets the session, without duplicating the stop call at each site.
- Runs continuously across the whole session, including idle time at the main menu between matches — it is not restarted per match. Only one `/custom`→`/quit` (or →idle-timeout) cycle drives one continuous stream.

**Config-gated, off by default:** `obs_stream_enabled` (bool, default `false`) is the master switch — `game/obs_control.py`'s `is_enabled()` gates every call, so leaving it `false` makes this feature fully inert (no connection attempts at all). `obs_websocket_host`/`obs_websocket_port`/`obs_websocket_password` must match what's configured in OBS's WebSocket Server Settings (defaults: `localhost` / `4455` / no password — OBS 28+'s own defaults).

**Best-effort, matching the `ds_ingest`/noble-hopper convention elsewhere in this codebase:** `start_stream()` and `stop_stream()` never raise — any connection failure (OBS not running, wrong port/password) is logged as a warning and swallowed. A failed `start_stream()` does not block lobby creation; the `/custom` response embed just shows "⚠️ Could not start" instead of "🔴 Live" in that case. Both functions also check `GetStreamStatus` first and no-op (return `True`) if the stream is already in the requested state, so a spurious extra start/stop call is harmless.

**Threading:** `obsws_python.ReqClient` does blocking websocket I/O (connect, send, disconnect all block). The `/custom` call site wraps `start_stream()` in `run_in_executor` to keep it off the event loop; `stop_stream()` inside `_reset_session()` is called directly (synchronous, un-executor-wrapped) — `_reset_session()` itself is a plain sync method called from many places, some of which (e.g. `_EndConfirmView._do_end`'s `close_game()` call) already accept a brief direct blocking call in the same spot, so this follows existing precedent rather than threading every one of `_reset_session()`'s ~12 call sites through an executor.

**Anti-cheat minimap cover (2026-08-31, show moved earlier same day):** an OBS source named `obs_minimap_cover_source` (config, default `"Map Cover"`) keeps the Director's minimap off-stream from lobby creation through the first `obs_minimap_cover_seconds` (config, default `120`) of the match, so early positions aren't visible to viewers. The source itself — an image, sized/positioned over the minimap — is set up manually in OBS; the bot only ever toggles its visibility via `obs_control.set_source_visible(source_name, visible)`, which looks up `GetCurrentProgramScene()` fresh on every call rather than hardcoding a scene name, so it keeps working if the scene is ever renamed.
- **Show** happens in `discord_bot.py`, right after `/custom`'s success embed (with the lobby code) is sent — not at match start. The minimap is potentially visible as soon as the lobby exists, and `/start` may not be called for a while after `/custom`, so covering it early is the safer anti-cheat default. Wrapped in `run_in_executor` since `set_source_visible` is a blocking websocket call.
- **Hide** is still owned by `MatchRunner`'s main loop, timed off actual match elapsed time (not lobby time) — a one-shot check (`self._minimap_uncovered`) fires once `elapsed >= obs_minimap_cover_seconds` into the match itself. Precision is bounded by `screen_poll_interval_seconds`, not exact to the second, which is fine for this purpose.
- Gated the same way as the rest of this section: no-ops entirely if `obs_stream_enabled` is `false`.

**Discord presence mirrors live status (2026-09-04):** `DirectorCog._set_streaming_presence()` switches the Discord bot's own presence to `discord.Streaming(name=..., url="https://twitch.tv/<channel>")` — the special purple "Streaming" indicator Discord clients only show for an activity whose `url` matches a recognized Twitch/YouTube watch URL (a plain custom status text does not trigger it). Called right after each of the two stream-start paths confirms the stream is actually live: the immediate `obs_control.start_stream()` call in `/custom` (only when `stream_started` is `True`) and `_delayed_stream_start()`'s tournament-mode version (only when `started` is `True`) — never called speculatively before Twitch confirms the stream is up. `_clear_streaming_presence()` (revert to no activity) is called from `_reset_session()` via `asyncio.ensure_future` (that method is sync, called from many places — same reasoning as `stop_stream()` being called directly there), so presence reverts on every path back to `IDLE`, matching the stream's own stop lifecycle exactly. Reuses `ds_ingest_twitch_channel` (config) for the channel name rather than adding a second key for the same fact — no-ops if that key isn't set. Both presence functions are best-effort and never raise.

## Custom Lobby Creation Flow (`_do_create_custom`)

Complete sequence triggered by `/custom <region>` from `IN_MENU` state. All coordinates are for **1920×1080**.

```
Step 0 — Region setup (skipped entirely if last_selected_region already matches — see below)
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
  persist last_selected_region = wanted

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
  snapshot clipboard                             remember what's there BEFORE the click — see below
  click(center[0]+316, center[1]-9)             clipboard icon offset from label center
  poll clipboard until it differs from the snapshot (timeout 10s) → lobby code
  press_key("escape")                           close the lobby menu
  press_key("shift")                            dismiss the initial tray display
```

**Region setup skipped entirely when the cached region already matches (2026-09-09):** `last_selected_region` (config, persisted via `_persist_config_value()` — same read-modify-write helper `/tournament` and the ladder tournament slug use) remembers the region the last successful `/custom` left the game set to. If it equals the region requested this time, Step 0's entire PLAY-screen round trip — open PLAY, template-check the region, click BACK, wait for main menu — is skipped outright and `_do_create_custom` goes straight to clicking CUSTOM on the main menu; nothing in the log even mentions a PLAY screen in this case. This was the common case anyway (the region rarely changes between matches) and that whole round trip was pure overhead on every single `/custom` even when nothing needed to change — several template waits and two hover-click delays, all for a check that almost always confirmed "already correct." Only on an actual mismatch (or before this key is ever set) does the full original flow run — visit PLAY, template-check, change the region if the screen itself disagrees with what was cached, click BACK — and `last_selected_region` is updated to the requested region right before returning to the main menu.

**Trade-off, accepted deliberately:** if the game's region is ever changed by something other than this bot (manual play through the game's own menus, a `/custom` that got interrupted mid region-change on a previous run) `last_selected_region` goes stale, and since the cache-hit path never opens the PLAY screen, there is no check left to catch that drift — the match would run in the cached region rather than the game's actual one until the next time an intentional region switch resyncs it. Acceptable here because this bot is the only thing that ever touches the region setting in the deployed setup; if that stops being true, the fix is either dropping the whole skip (revert to always checking) or having it re-verify on-screen every Nth call instead of every call.

**Clipboard step polls for a change instead of a flat 10s sleep, and checks freshness against the pre-click value (2026-09-09):** the old code slept a flat 10 seconds after the Director lobby appeared ("game can lag here and a clipboard copy attempted too early will return empty"), then read `pyperclip.paste()` exactly once. That paid the full 10s on every single `/custom` even when the lobby settled in 1-2s. It's now `previous_clipboard = pyperclip.paste().strip()` captured **before** clicking the copy icon, then a poll loop (`_CLIPBOARD_POLL_TIMEOUT_SECONDS` = 10.0 ceiling, `_CLIPBOARD_POLL_INTERVAL_SECONDS` = 0.3) that clicks, then keeps re-reading the clipboard until it sees a non-empty value that's **different from `previous_clipboard`**, returning as soon as that happens rather than waiting out the full ceiling. The pre-click snapshot matters for correctness, not just speed: `pyperclip.paste()` only ever reports whatever the OS clipboard currently holds, with no way to know whether *this* click actually landed yet — reading it once, immediately, with no comparison point would read a previous lobby's leftover code as a perfectly valid (but wrong) success. Comparing against the snapshot works regardless of what happened to be in the clipboard beforehand — a previous lobby code, empty, or something unrelated the user copied earlier — the only thing that matters is that the value changed after the click. The 10s ceiling is kept as a worst-case fallback matching the old sleep's budget, so if it's ever hit, `Custom: clipboard never changed after copy button` logs the stale value it saw instead of the old ambiguous "clipboard empty."

**Two `wait_for_template_center` calls tightened to a 0.5s poll interval (2026-09-09):** `create_custom_match.png` (step 2) and `lobby_password_label.png` (step 9) previously omitted `poll_interval`, falling back to the function's own default of 2.0s — every other wait in this flow already passes `poll_interval=0.5` explicitly. Both now match; a screen that becomes ready just after a check is caught up to 1.5s sooner on average.

**Key offsets calibrated at 1920×1080:**
- Clipboard icon: `lobby_password_label` center + (316px right, 9px up)
- DIRECTOR card click: `choose_role_screen` template center − 175px vertically
- Region popup rows: NA=(740, 468), EU=(720, 511) — hardcoded, not template-matched (game highlights active row white which breaks matching)

### UE5 update UI redesign (2026-08-07)

The game's UE5 update reworked several screens in this flow. Templates were recaptured and verified (self-match ≥0.95, cross-screen <0.8) against the live game; **`_do_create_custom`'s code was not modified** — only template images changed, and only where the old one actually stopped matching (see per-template list below). Screens not listed here (`choose_role_screen`, the Director lobby / `lobby_password_label`, `create_custom_match`, `privacy_private`, `start_button`, `region_popup_header`) were re-verified and still match fine — no changes.

- **PLAY screen (region setup)** — same "CHANGE REGION" concept, but now a single pill button reading e.g. `US EAST (N. VIRGINIA)` instead of separate flag-style indicators. Recaptured: `play_screen_region.png` (now just the "CHANGE REGION" label, static), `region_na.png` / `region_eu.png` (now the button's text portion, cropped to exclude the live ping value per the existing "no ping in region templates" rule), `play_screen_back.png` (BACK button moved/restyled — still doubles as the region-popup BACK button, score 0.99, same dual-purpose behavior as before).
- **Region popup** — redesigned with a 3rd option, **Asia Pacific (Singapore)**, alongside US East and EU. `region_popup_header.png` ("CHOOSE YOUR REGION" banner) is unchanged and still matches (0.93). New row templates captured for documentation/reference (not used for matching, same as before): `region_row_na.png`, `region_row_eu.png`, `region_row_apac.png`. New button-text template `region_apac.png` also captured (`ASIA PACIFIC (SINGAPORE)`, same crop box as NA/EU).
  - **`/custom` now has an APAC choice** (`app_commands.Choice(name="APAC — Singapore", value="APAC")`) and `_do_create_custom` detects/selects it via `region_apac.png` and `_REGION_ROW["APAC"]`. While touching `_REGION_ROW`, all three entries were moved off the old off-center hardcoded coordinates to center-x 960 (which sits inside every row regardless of region): NA=(960, 480), EU=(960, 525), APAC=(960, 568) — the old NA/EU values (740,468)/(720,511) happened to still land inside their rows post-redesign, but 960 is more robust long-term.
- **CREATE MATCH screen** — this is the screen that changed the most. Mode cards (DUEL / SOLO CLASSIC / DUO CLASSIC / DUO / SOLO) are now much larger and repositioned; SOLO CLASSIC moved from wherever it was in the old layout to a top-row card centered at screen center. Recaptured: `solo_classic_label.png` (now the label strip at the bottom of the SOLO CLASSIC card, centered ~(960, 440), 435×42px — much bigger than the old crop). `privacy_private.png` and `start_button.png` on this screen were unaffected and still match.
- **Not independently verified:** `lobby_countdown_label.png` (the "lobby_open" screen signature) — this needs the lobby to actually open with a countdown active to test, which wasn't exercised during this pass. If screen detection seems to misfire around lobby-open state, check this template first.

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

### How Claude Code captures the live game screen for calibration (2026-08-30)

When a human needs to hold a card/menu open so Claude can read off coordinates (e.g. calibrating `zone_close_auto_drop_target`), the `mcp__computer-use__*` tools **do not work for this game** — two separate problems, both bypassed by going straight to Bash instead:

1. **`request_access` grants the wrong process.** The Start Menu entry "Darwin Project" resolves to the Steam launch shortcut (`bundleId: steam://rungameid/544920`), but the actual running window belongs to `Darwin-Win64-Shipping.exe` — a different process spawned by Steam (same stub-vs-real-exe split documented in the Game Launcher section above). Any `left_click` on the game window fails with "`Darwin-win64-shipping` is not in the allowed applications", and re-requesting access under "Darwin Project" doesn't fix it — the grant always resolves to the same launcher bundle id, never the real process.
2. **`computer_batch`'s own `screenshot` action comes back solid black** over the game window regardless of the above. Likely cause: the game (Unreal Engine, per the UE5 update notes above) stops presenting frames — or something in the capture path gets blocked — when the window doesn't have real OS focus, and computer-use's failed clicks mean focus never actually reached the game.

**The fix — use the bot's own working code path via Bash instead of computer-use:**
```bash
python -c "from game.card_actions import focus_darwin_window; print(focus_darwin_window())"
python -c "
from game.screen_detection import take_screenshot
import cv2
cv2.imwrite(r'<scratchpad>\shot.png', take_screenshot())
"
```
Then `Read` the saved PNG directly — it renders as an image, no computer-use involved. This is exactly the `focus_darwin_window()` → `take_screenshot()` sequence the bot already calls before every real card play, so if it works during a live match it works here too. It also sidesteps the resolution mismatch noted in Template capture rules above (`take_screenshot()` is native 1920×1080, not computer-use's 1456×816).

Two side notes from doing this live: `pyautogui.screenshot()` alone only captures the **primary** monitor — if the game is on a secondary display, either move the game window first or grab the full virtual desktop and crop (`PIL.ImageGrab.grab(all_screens=True)`, then crop using `ctypes.windll.user32.GetSystemMetrics(76/77/78/79)` for the virtual-screen origin/size — the secondary monitor's region is `(0, 0, primary_width, primary_height)` in the combined image when it sits to the left of the primary at virtual x-origin `-primary_width`). And once a coordinate is picked from the screenshot, cross-check it with a quick pixel-color scan (`img[y, x]`, BGR order from `cv2.imread`) across a small grid rather than eyeballing it — that's how the exact `y ≥ 975` boundary for the zone_close drop area (see Zone Logic section) was confirmed solid with no text/icon gaps before committing it to config.

### Match end detection pitfalls

`placement_badge.png` is the "MAIN MENU" button (white text on dark blue, 98×30px). It appears on the results screen and is also used by `_do_post_match_return()` to find and click MAIN MENU after the match.

- Threshold is **0.88** — lower values cause false positives from HUD elements during the match
- Double-confirm required: the template must match **twice, 2 seconds apart** before ending the match (`_match_has_ended()`)
- `poll_for_match_end()` saves a debug screenshot to `screenshots/errors/` on every positive detection (useful for debugging false positives)

### Match profiles (`game/profiles.py`)

Profiles define the card play schedule. Active profile is set in `config.json → active_profile`.

**Standard profile** (default): Electromania 2:30 · Beach Party 4:00 · Electromania 6:30 · Blood Moon 9:00

**Beach Shuffle profile** (2026-08-29): based on Standard, with two changes. First, Beach Party's play time is randomized once per match — a random 15s-aligned time between 4:00 and 6:00 (i.e. one of 4:00, 4:15, 4:30 ... 6:00); the two zone closes that used to bracket Beach Party in Standard are fixed at later times to make room regardless of the draw: 5:00 → 7:00, 8:00 → 9:00. Electromania at 6:30 is unaffected and can now fire before the (now 7:00) zone close on a late Beach Party draw — that reordering was a deliberate, explicit choice, not an oversight. Second, Telepathy at 10:00 is swapped for Blood Moon at 10:00.

Implemented via a profile-level `random_play_times` key: `{card_type: (min_seconds, max_seconds, step_seconds)}`. `resolve_profile()` deep-copies the profile and substitutes a `random.randrange()` draw into that card's `play_time_seconds` — resolved once per match (at `/custom` time, cached in `_resolved_profile`) so the same draw is used for both the Discord profile-summary display and the actual match schedule. `profile_summary()` shows the configured range (e.g. `Beach Party 4:00-6:00 (random)`) rather than a single time when a card has a `random_play_times` entry.

Adding new profiles: add an entry to `PROFILES` dict in `game/profiles.py`. The bot picks it up via `get_profile()` — no other changes needed. Add a `random_play_times` entry only if a card's time should vary per match.

## Config Reference (`config.json`)

```json
{
    "game_executable_path": "",          // Full path to DarwinProject.exe
    "discord_bot_token": "",             // Discord bot token (keep secret)
    "discord_required_role": "PC Scrim Admin",   // Intentionally the same value as scrim_admin_role — see merge note in Discord Bot section
    "discord_guild_ids": ["..."],        // Guild IDs for instant slash command sync — removing an ID here does NOT un-register commands already synced to that guild, see note above
    "zone_selection_strategy": "weighted_outer",
    "active_profile": "standard",        // Match card play profile (see game/profiles.py)
    "last_selected_region": null,        // Auto-persisted by _do_create_custom — last region /custom left the game set to;
                                          // skips the whole PLAY-screen region check when it matches the next request (2026-09-09)
    "tournament_mode": false,            // Toggled via /tournament — delays /custom's stream start 2 min, keeps minimap cover up all match
    "ahk_bypass_mode": false,            // true = log actions instead of executing
    "verify_card_plays": true,           // false = play once and trust it, skip pixel-verify/retries (kept, not deleted)
    "tts_device": "CABLE Input",         // Sounddevice output name for TTS; omit to disable TTS
    "tts_voice": "en-US-AriaNeural",  // edge-tts voice name
    "card_play_lead_time_seconds": 2,    // Fire card events this many seconds early to account for drag time

    // A batch of 1920×1080-calibrated coordinates used to live here and were moved to
    // hardcoded constants in game/match_runner.py (2026-09-07): card tray layout
    // (card_tray_center_x/card_tray_card_y/card_tray_card_width), cards.*.drop_target
    // (_CARD_DROP_TARGETS), zone_close_auto_drop_target (_ZONE_CLOSE_DROP_TARGET,
    // [1750, 1000]), zone_map_sample_points and zone_drop_coordinates (_ZONE_MAP_SAMPLE_POINTS
    // / _ZONE_DROP_COORDINATES, both dormant — _attempt_zone_close_legacy() only),
    // director_points_region (_DIRECTOR_POINTS_REGION, [808, 1002, 20, 24]) and
    // director_points_pips (_DIRECTOR_POINTS_PIPS, dormant while director_points_use_pips
    // is false). Unlike the pixel-region calibration data still below, none of these vary
    // per deployment (this bot only supports 1920×1080) and all have proven stable, so
    // there was nothing left for config.json to buy by keeping them editable —
    // recalibrating any of them now means editing the constant in code directly.
    // director_points_use_pips itself stays here — it's a genuine toggle, not calibration data.
    "director_points_use_pips": false,   // false = OCR-only (pip pixel sampling was overcounting, see Director points reading section); kept, not deleted

    // player_target_coordinates and zone_color_thresholds are omitted entirely rather
    // than kept as null placeholders (2026-09-07) — both are read via `.get(key, default)`
    // with a fallback that already matches "uncalibrated" (`[]` / `{}`), so an absent key
    // behaves identically to an explicit null and there was nothing config.json gained by
    // keeping them. Still genuinely uncalibrated, still gate real (if currently inert)
    // features — set them again if either is ever calibrated:
    //   "player_target_coordinates": [[x, y], ...]   — picks one at random per play
    //   "zone_color_thresholds": {"open": [r,g,b], "closing": [r,g,b], "closed": [r,g,b]}

    "screen_poll_interval_seconds": 12,
    "launch_timeout_seconds": 180,

    // Player bar (the in-match HUD card strip). Names, first blood, eliminations and the
    // ladder's live feed ALL start here. NOT SET = the bot logs "no slots detected — player
    // tracking disabled" and sends none of it. Calibrate with `python calibrate_player_bar.py
    // <frame.png>` (offline, any machine) or F8 in calibrate.py (live) — docs/PLAYER_BAR_CALIBRATION.md.
    "player_bar_detector": "v1",         // "v1" = separator scan (needs the keys below), "v2" = geometry detector (game/player_cards_v2.py,
                                         //   no calibration at 1920×1080, proven on VOD frames), "shadow" = v1 drives, v2 reports
                                         //   detector_v2 / eliminated_v2 events for an A/B. See docs/PLAYER_BAR_CALIBRATION.md.
    "player_bar_region": null,           // [x0, y0, x1, y1] of the whole card strip at 1920×1080 — REQUIRED for v1 tracking
    "player_separator_threshold": 25,    // max-channel brightness below which a column counts as a card separator
    "player_portrait_y_in_bar": 35,      // y offset inside the bar where one portrait pixel is sampled for alive/dead
    "player_saturation_threshold": 40,   // HSV saturation above which that pixel means "alive" (eliminated portraits go grey)
    "player_name_y_in_bar": 62,          // y offset inside the bar of the nameplate text strip
    "player_name_h": 14,                 // height of that strip (tesseract OCR crop)
    "kill_notification_region": null,    // [x, y, w, h] of the kill-feed text, OCR'd once at first blood (optional)

    // Video recording. Crop region [755, 175, 410, 200] moved to a hardcoded constant
    // (_CROP_REGION in game/video_recorder.py, 2026-09-07) — same rationale as the
    // match_runner.py constants above.
    "recording_enabled": false,          // false = no local recording at all, and therefore no upload attempt either — currently off
    // recording_api_endpoint omitted (2026-09-07) — read via `.get("recording_api_endpoint", "")`,
    // so an absent key behaves the same as the empty string it held before. Upload is still a
    // TODO stub regardless (see Video Recorder section); set this key again once it's implemented.

    // Ladder ingestion (darwinstalker.com, formerly ds.xdos.ai) — see game/ingest.py
    "ds_ingest_base_url": "https://darwinstalker.com",
    "ds_ingest_token": "",                // Bearer token, issued out of band — leave empty to skip ingest
    "ds_ingest_platform": "pc",           // "pc" | "xbox"
    "ds_ingest_twitch_channel": "",       // Twitch channel name for the director's stream. The LIVE tab's Twitch embed ONLY appears when this is non-empty (and the bot restarted after editing) — empty means the open-draft call omits it and the bot logs "opened WITHOUT a twitch_channel" at every /custom
    "ds_ingest_tournament_slug": "",      // darwinstalker.com tournament slug; written by /tournament on slug:<slug>, sent on open-draft only while tournament_mode is true

    // Scrim signup system
    "scrim_signup_channel_id": "1520517054988419123",
    "scrim_signup_message_id": null,     // Auto-persisted by bot on startup — do not edit manually
    "scrim_region_message_id": null,     // Auto-persisted — live region-breakdown message posted below the signup message
    "scrim_player_role": "PC Scrim Player",
    "scrim_player_role_2": "PC Scrim Player 2", // optional — second-lobby role for signups 11-20
    "scrim_admin_role": "PC Scrim Admin", // same value as discord_required_role — see merge note above
    "scrim_min_players": 8,
    "scrim_reaction_emoji": "✅",
    "region_role_na": "NA",               // Discord role name checked by the live region-breakdown message
    "region_role_eu": "EU",

    // OBS Twitch streaming — see game/obs_control.py. Off by default; OBS must already
    // be running with Stream settings pointed at Twitch and websocket server enabled.
    "obs_stream_enabled": false,
    "obs_websocket_host": "localhost",
    "obs_websocket_port": 4455,
    "obs_websocket_password": "",
    "obs_minimap_cover_source": "Map Cover", // OBS source name toggled to hide the minimap for the first obs_minimap_cover_seconds
    "obs_minimap_cover_seconds": 120
}
```

**config.json is gitignored** (contains the bot token). Set it up manually on each machine.

## Templates Directory

All templates captured at **1920×1080** via pyautogui. Centers listed are for the current calibration machine.

| Template | Purpose | Center (approx) |
|---|---|---|
| `play_button.png` | Main menu detection + PLAY button click | (355, 258) |
| `play_screen_region.png` | PLAY screen gate ("CHANGE REGION" label text) | (75, 1028) |
| `play_screen_back.png` | BACK button on PLAY screen and region popup (dark blue border) | (1868, 1051) |
| `region_na.png` | Detects NA (US East) is currently selected (button text, ping excluded) | (135, 1057) |
| `region_eu.png` | Detects EU (Frankfurt) is currently selected (button text, ping excluded) | (135, 1057) |
| `region_apac.png` | Detects Asia Pacific (Singapore) is currently selected (button text) | (135, 1057) |
| `region_popup_header.png` | "CHOOSE YOUR REGION" popup detection | (955, 416) |
| `region_row_na.png` | US East row in popup (reference only — not used for matching) | (960, 482) |
| `region_row_eu.png` | EU row in popup (reference only — not used for matching) | (960, 525) |
| `region_row_apac.png` | Asia Pacific row in popup (reference only — not used for matching) | (960, 570) |
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
- [x] Card tray layout and per-card drop targets — hardcoded constants in `game/match_runner.py` (`_CARD_TRAY_CENTER_X`/`_CARD_TRAY_CARD_Y`/`_CARD_TRAY_CARD_WIDTH`, `_CARD_DROP_TARGETS`), not config, see Config Reference above
- [x] `_ZONE_CLOSE_DROP_TARGET` — hardcoded constant, calibrated to `(1750, 1000)` (2026-08-30), inside the "SPECTATORS — LET THEM DECIDE" corner triangle
- [x] `_ZONE_MAP_SAMPLE_POINTS` / `_ZONE_DROP_COORDINATES` (all 7 zones) — hardcoded constants in `game/match_runner.py`, dormant (only needed if `_attempt_zone_close_legacy()` is ever restored)
- [ ] ~~`zone_close_card_slot`~~ / `zone_color_thresholds` — never calibrated; only needed if `_attempt_zone_close_legacy()` is ever restored. `zone_color_thresholds` (and `player_target_coordinates`, `recording_api_endpoint`) are omitted from config.json entirely rather than kept as null placeholders (2026-09-07) — see Config Reference above for how to set any of them again
- [x] ~~`zone_sample_coordinates`~~ / ~~`results_ocr_regions`~~ — deleted from config.json (2026-09-07): both dead, read by zero lines of code
- [x] `templates/placement_badge.png` captured (MAIN MENU button, 98×30px — self-match 1.0, in-game HUD 0.50)
- [x] `_DIRECTOR_POINTS_REGION` — hardcoded constant, calibrated to `(808, 1002, 20, 24)` (2-digit numerator only)
- [x] `_DIRECTOR_POINTS_PIPS` — hardcoded constant in `game/match_runner.py`, dormant while `director_points_use_pips` (config) is `false`
- [x] `tts_device` set to `"CABLE Input"` in config.json
- [ ] `player_bar_region` + the five player-bar keys — never calibrated on any machine as of 2026-09-06 (they were added to the code without a template entry); without `player_bar_region` all player tracking is off. Do it with `calibrate_player_bar.py` / F8 — docs/PLAYER_BAR_CALIBRATION.md

## Testing Requirements

Custom matches in Darwin Project require **Director + minimum 2 players** to start. Cannot start a match with fewer.

**Test phases (in order):**
1. No game needed — zone logic, state machine, Discord commands, bypass mode all work now
2. Director client only — menu navigation, deck check, custom match creation, lobby code capture ✅ **complete**
3. Director + 2 players on separate machines — first full live match test

**EAC + VM note:** EAC actively detects and blocks virtualized environments. Running player clients in VMs on the same machine will not work. Players must be on separate physical machines.

## Pending Implementation (TODOs)

- `bot/discord_bot.py` — `_do_launch()`: replace template path placeholder with real captured template
- In-game calibration: zone pixel coordinates, zone color thresholds, OCR regions (requires live Director match with 2+ players) — card slot coordinates are done, see Calibration Checklist above

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

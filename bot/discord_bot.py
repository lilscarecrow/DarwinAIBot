import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from game.match_runner import MatchRunner
from session.state import SessionState, BotState

# Path to noble-hopper state.json — one level up from bot/, into noble-hopper/
_STATE_JSON = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "noble-hopper", "state.json")
)

# Director cards available in the deck builder (display label → ItemType value)
_DIRECTOR_CARDS = [
    ("Empty",               "ItemType_Null"),
    ("Zone Closing",        "ItemType_SDP_ZoneClosing"),
    ("Open Zone",           "ItemType_SDP_OpenZone"),
    ("Lava Zone",           "ItemType_SDP_LavaZone"),
    ("Nuclear Blast",       "ItemType_SDP_NuclearBlast"),
    ("Anti-Grav Storm",     "ItemType_SDP_AntiGravStorm"),
    ("Spawn Electronic",    "ItemType_SDP_ActivatePylon"),
    ("Electromania",        "ItemType_SDP_ActivateAllPylons"),
    ("Warm Up",             "ItemType_SDP_WarmUp"),
    ("Speed Boost",         "ItemType_SDP_SpeedBoost"),
    ("Beach Party",         "ItemType_SDP_NakedAll"),
    ("Man Hunt",            "ItemType_SDP_ManHunt"),
    ("Favorite Player",     "ItemType_SDP_FavoritePlayer"),
    ("Give Wood",           "ItemType_SDP_GiveWood"),
    ("Give Leather",        "ItemType_SDP_GiveLeather"),
    ("Telepathy",           "ItemType_SDP_Telepathy"),
    ("Expose",              "ItemType_SDP_MutualVision"),
    ("Blood Moon",          "ItemType_SDP_Hecatombe"),
]
_CARD_LABEL = {value: label for label, value in _DIRECTOR_CARDS}


def _read_deck() -> list:
    """Read the current 11-slot deck from noble-hopper state.json."""
    try:
        if os.path.exists(_STATE_JSON):
            with open(_STATE_JSON, "r", encoding="utf-8") as f:
                loaded = json.load(f).get("directorDeck", [])
            return (loaded + ["ItemType_Null"] * 11)[:11]
    except Exception:
        pass
    return ["ItemType_Null"] * 11


def _write_deck(deck: list) -> None:
    """Write deck to noble-hopper state.json, preserving all other proxy fields."""
    state = {}
    if os.path.exists(_STATE_JSON):
        with open(_STATE_JSON, "r", encoding="utf-8") as f:
            state = json.load(f)
    state["directorDeck"] = deck
    state.pop("lastSyncedDeck", None)   # force proxy to re-sync on next game action
    state["needsSync"] = True           # signal proxy to sync on next game API request
    with open(_STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _try_force_sync() -> bool:
    """POST to noble-hopper's force-sync endpoint. Returns True if the deck was applied live."""
    try:
        import urllib.request as _urlreq
        req = _urlreq.Request(
            "http://localhost:3000/api/force-sync-deck",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if not result.get("success"):
                logger.warning("Force sync failed: %s", result.get("error", "unknown error"))
            return result.get("success", False)
    except Exception as e:
        logger.warning("Force sync unavailable: %s", e)
        return False


# ------------------------------------------------------------------
# Deck editor UI components
# ------------------------------------------------------------------

class _SlotSelect(discord.ui.Select):
    def __init__(self, slot: int, current: str, row: int):
        options = [
            discord.SelectOption(label=label, value=value, default=(value == current))
            for label, value in _DIRECTOR_CARDS
        ]
        super().__init__(placeholder=f"Slot {slot + 1}: {_CARD_LABEL.get(current, current)}", options=options, row=row)
        self.slot = slot

    async def callback(self, interaction: discord.Interaction):
        view: DeckEditorView = self.view
        view.deck[self.slot] = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class _NavButton(discord.ui.Button):
    def __init__(self, label: str, direction: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: DeckEditorView = self.view
        view.page += self.direction
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class _SaveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save Deck", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction):
        view: DeckEditorView = self.view
        try:
            _write_deck(view.deck)
        except Exception as ex:
            await interaction.response.send_message(f"Save failed: {ex}", ephemeral=True)
            return
        view.stop()
        lines = [f"`{i + 1:2}.` {_CARD_LABEL.get(c, c)}" for i, c in enumerate(view.deck)]
        e = discord.Embed(title="Deck Saved", description="\n".join(lines), color=_COLOR_OK)
        e.set_footer(text="Deck saved — will apply on next launch.")
        await interaction.response.edit_message(embed=e, view=None)


class DeckEditorView(discord.ui.View):
    _SLOTS_PER_PAGE = 4

    def __init__(self, deck: list):
        super().__init__(timeout=120)
        self.deck = list(deck)
        self.page = 0
        self._rebuild()

    @property
    def _total_pages(self) -> int:
        return (11 + self._SLOTS_PER_PAGE - 1) // self._SLOTS_PER_PAGE  # ceil(11/4) = 3

    def _rebuild(self):
        self.clear_items()
        start = self.page * self._SLOTS_PER_PAGE
        end = min(start + self._SLOTS_PER_PAGE, 11)
        for i in range(start, end):
            self.add_item(_SlotSelect(i, self.deck[i], row=i - start))
        if self.page > 0:
            self.add_item(_NavButton("← Prev", -1))
        self.add_item(_SaveButton())
        if self.page < self._total_pages - 1:
            self.add_item(_NavButton("Next →", +1))

    def build_embed(self) -> discord.Embed:
        lines = [f"`{i + 1:2}.` {_CARD_LABEL.get(c, c)}" for i, c in enumerate(self.deck)]
        e = discord.Embed(title="Director Deck", description="\n".join(lines), color=_COLOR_OK)
        e.set_footer(text=f"Page {self.page + 1}/{self._total_pages} · Edit slots below then Save")
        return e

logger = logging.getLogger(__name__)

# Hard safety ceilings for each long-running operation.
_LAUNCH_TIMEOUT = 420.0    # 7 min: game start + splash + menu detection (slow HDD installs)
_CUSTOM_TIMEOUT = 180.0    # 3 min: menu navigation to lobby (lobby load can be slow)
_MATCH_TIMEOUT  = 3600.0   # 60 min: safety net — real matches can exceed 20 min

# Embed accent colors
_COLOR_OK      = 0x2ECC71  # green   — success
_COLOR_FAIL    = 0xE74C3C  # red     — failure / error
_COLOR_ACTIVE  = 0x3498DB  # blue    — launching / in-progress
_COLOR_WARN    = 0xE67E22  # orange  — active match / warning
_COLOR_NEUTRAL = 0x95A5A6  # gray    — idle / neutral


class _EndConfirmView(discord.ui.View):
    """Two-button Yes/No prompt for /quit."""

    def __init__(self, cog: "DirectorCog"):
        super().__init__(timeout=30)
        self._cog = cog

    async def _do_end(self, interaction: discord.Interaction):
        self._cog._stop_event.set()
        if self._cog._active_runner is not None:
            self._cog._active_runner.stop()
        from game.launcher import close_game
        close_game()
        self._cog._reset_session()
        self.stop()
        embed = discord.Embed(title="Session Ended", description="Game closed. Bot reset to IDLE.", color=_COLOR_OK)
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Yes, close game", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_end(interaction)

    @discord.ui.button(label="No, keep running", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="Cancelled", description="Session continues.", color=_COLOR_NEUTRAL),
            view=None,
        )

    async def on_timeout(self):
        # View expires silently — no edit possible without storing the message reference
        self.stop()


class _ProfileButton(discord.ui.Button):
    def __init__(self, key: str, display_name: str, is_active: bool):
        super().__init__(
            label=display_name,
            style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        view: "_ProfileSelectView" = self.view
        await view.select_profile(interaction, self.key)


class _ProfileSelectView(discord.ui.View):
    def __init__(self, cog: "DirectorCog", active_key: str):
        super().__init__(timeout=60)
        self._cog = cog
        self._active = active_key
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        from game.profiles import PROFILES
        for key, p in PROFILES.items():
            self.add_item(_ProfileButton(key, p["display_name"], key == self._active))

    async def select_profile(self, interaction: discord.Interaction, key: str):
        self._active = key
        self._cog.bot.config["active_profile"] = key
        try:
            import json as _json
            from pathlib import Path as _Path
            cfg_path = _Path("config.json")
            with cfg_path.open(encoding="utf-8") as f:
                on_disk = _json.load(f)
            on_disk["active_profile"] = key
            with cfg_path.open("w", encoding="utf-8") as f:
                _json.dump(on_disk, f, indent=4)
        except Exception as ex:
            logger.warning("Could not persist active_profile to config.json: %s", ex)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def build_embed(self) -> discord.Embed:
        from game.profiles import PROFILES, get_profile, profile_summary
        p = get_profile(self._active)
        embed = discord.Embed(
            title="Match Profile",
            description=f"Active: **{p['display_name']}**",
            color=_COLOR_OK,
        )
        for prof in PROFILES.values():
            embed.add_field(name=prof["display_name"], value=profile_summary(prof), inline=False)
        if self._cog.bot.session.state == BotState.MATCH_IN_PROGRESS:
            embed.set_footer(text="Takes effect on next /start")
        return embed


class DarwinBot(commands.Bot):
    def __init__(self, config: dict, session: SessionState):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.session = session

    async def setup_hook(self):
        cog = DirectorCog(self)
        await self.add_cog(cog)
        # discord_guild_ids accepts a list or a single id string/int.
        # Falls back to the legacy discord_guild_id key for compatibility.
        guild_ids = self.config.get("discord_guild_ids") or self.config.get("discord_guild_id")
        if guild_ids:
            if not isinstance(guild_ids, list):
                guild_ids = [guild_ids]
            for gid in guild_ids:
                guild = discord.Object(id=int(gid))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("Slash commands synced to guild %s (instant)", gid)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to 1 hour to propagate)")
        self.loop.create_task(cog._screen_watcher())

    async def on_ready(self):
        logger.info("Logged in as %s (id %d)", self.user, self.user.id)
        from game import tts
        tts.set_event_loop(asyncio.get_event_loop())


class DirectorCog(commands.Cog):
    def __init__(self, bot: DarwinBot):
        self.bot = bot
        # Asyncio lock prevents two long-running operations from running simultaneously.
        # State validation already rejects bad states, but this guards against
        # concurrent calls that arrive before a state transition completes.
        self._session_lock = asyncio.Lock()
        # Holds the active MatchRunner so /end can stop it mid-match.
        self._active_runner: Optional[MatchRunner] = None
        # Shared stop signal for _do_launch and _do_create_custom threads.
        # /end sets this; each thread checks it between steps to exit early.
        self._stop_event = threading.Event()
        # Background task that fires the match runner when the lobby auto-start
        # timer expires. Cancelled immediately if /start is used manually.
        self._auto_start_task: Optional[asyncio.Task] = None
        # Absolute monotonic time when the lobby expires (set by auto-start watcher).
        self._lobby_expiry: Optional[float] = None
        # Profile resolved at /custom time so the same pick is used at /start.
        self._resolved_profile: Optional[dict] = None

    # Screen name → (BotState, label) used by the background screen watcher
    _SCREEN_STATES = {
        "main_menu":       (BotState.IN_MENU,   "Main menu"),
        "custom_browser":  (BotState.IN_MENU,   "Custom match browser"),
        "create_match":    (BotState.IN_MENU,   "Create Match screen"),
        "director_lobby":  (BotState.IN_CUSTOM, "Director lobby"),
        "lobby_open":      (BotState.IN_CUSTOM, "Director lobby (open)"),
        "director_splash": (BotState.LAUNCHING, "Latest Updates splash"),
    }

    _STATE_COLORS = {
        BotState.IDLE:              _COLOR_NEUTRAL,
        BotState.LAUNCHING:         _COLOR_ACTIVE,
        BotState.IN_MENU:           _COLOR_OK,
        BotState.IN_CUSTOM:         _COLOR_OK,
        BotState.MATCH_IN_PROGRESS: _COLOR_WARN,
        BotState.MATCH_ENDED:       _COLOR_NEUTRAL,
    }

    # ------------------------------------------------------------------
    # Embed helpers — each reads session state at call time, so always
    # call after any state transitions to get the correct footer.
    # ------------------------------------------------------------------

    def _embed(self, title: str, description: str = "", *, color: int) -> discord.Embed:
        e = discord.Embed(title=title, description=description, color=color)
        state_label = self.bot.session.state.name.replace("_", " ").title()
        e.set_footer(text=f"State: {state_label}")
        return e

    def _ok(self, title: str, description: str = "") -> discord.Embed:
        return self._embed(title, description, color=_COLOR_OK)

    def _fail(self, title: str, description: str = "") -> discord.Embed:
        return self._embed(title, description, color=_COLOR_FAIL)

    def _info(self, title: str, description: str = "") -> discord.Embed:
        color = self._STATE_COLORS.get(self.bot.session.state, _COLOR_NEUTRAL)
        return self._embed(title, description, color=color)

    # ------------------------------------------------------------------
    # Background screen watcher
    # ------------------------------------------------------------------

    async def _screen_watcher(self):
        """Background task: detect current screen every 15 s and sync bot state."""
        from game.screen_detection import detect_current_screen
        from game.launcher import is_game_running

        _WATCHER_INTERVAL = 15  # seconds between checks

        while True:
            await asyncio.sleep(_WATCHER_INTERVAL)
            try:
                # Skip while any operation is actively running — avoids mid-flight interference
                if self._session_lock.locked():
                    continue
                # No point checking if IDLE and game isn't running
                if self.bot.session.state == BotState.IDLE:
                    loop = asyncio.get_running_loop()
                    running = await loop.run_in_executor(None, is_game_running)
                    if not running:
                        continue

                loop = asyncio.get_running_loop()
                screen = await loop.run_in_executor(None, detect_current_screen)

                if screen not in self._SCREEN_STATES:
                    continue

                new_state, label = self._SCREEN_STATES[screen]
                current = self.bot.session.state

                if new_state != current:
                    logger.info(
                        "Screen watcher: detected '%s' — correcting %s → %s",
                        screen, current.name, new_state.name,
                    )
                    self.bot.session.transition(
                        new_state,
                        last_action=f"Auto-detected: {label}",
                        next_action="Awaiting command",
                    )

                # Idle-close: if we've been in the main menu for > 10 minutes, close the game
                _IDLE_CLOSE_SECONDS = 600
                if (
                    self.bot.session.state == BotState.IN_MENU
                    and self.bot.session.state_duration_seconds() > _IDLE_CLOSE_SECONDS
                ):
                    logger.info("Screen watcher: IN_MENU idle > 10 min — closing game")
                    from game.launcher import close_game
                    await loop.run_in_executor(None, close_game)
                    self._reset_session()
            except Exception as e:
                logger.debug("Screen watcher error: %s", e)

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _has_role(self, interaction: discord.Interaction) -> bool:
        required = self.bot.config.get("discord_required_role", "")
        return any(r.name == required for r in interaction.user.roles)

    def _reset_session(self):
        """Reset session state and clear any lobby-scoped cached values."""
        if self._auto_start_task and not self._auto_start_task.done():
            self._auto_start_task.cancel()
            logger.info("Auto-start watcher cancelled by session reset")
        self._auto_start_task = None
        self._resolved_profile = None
        self._lobby_expiry = None
        from game import tts
        tts.stop()
        self.bot.session.reset()

    async def _role_check(self, interaction: discord.Interaction) -> bool:
        """Silent ignore if user lacks the required role (per spec)."""
        return self._has_role(interaction)

    async def _state_check(self, interaction: discord.Interaction, command: str) -> bool:
        if not self.bot.session.is_command_valid(command):
            await interaction.response.send_message(
                self.bot.session.invalid_command_message(command), ephemeral=True
            )
            return False
        return True

    async def _lock_check(self, interaction: discord.Interaction) -> bool:
        """Reject if another operation is already in progress."""
        if self._session_lock.locked():
            await interaction.response.send_message(
                f"Another operation is already running. {self.bot.session.status_message()}",
                ephemeral=True,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # /launch
    # ------------------------------------------------------------------

    @app_commands.command(name="launch", description="Launch the game and wait for the menu screen")
    async def launch(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return
        if not await self._state_check(interaction, "launch"):
            return
        if not await self._lock_check(interaction):
            return

        await interaction.response.defer(thinking=True)

        async with self._session_lock:
            self._stop_event.clear()
            self.bot.session.transition(
                BotState.LAUNCHING,
                last_action="Launch initiated",
                next_action="Detect menu screen",
            )
            loop = asyncio.get_running_loop()
            try:
                success = await asyncio.wait_for(
                    loop.run_in_executor(None, self._do_launch),
                    timeout=_LAUNCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._stop_event.set()
                self._reset_session()
                await interaction.followup.send(embed=self._fail(
                    "Launch Timed Out",
                    f"No menu screen detected after {int(_LAUNCH_TIMEOUT // 60)} minutes.\n"
                    "Check `darwin_bot.log` for details.",
                ))
                return

        if success:
            self.bot.session.transition(
                BotState.IN_MENU,
                last_action="Menu screen detected",
                next_action="Await /deck or /custom",
            )
            await interaction.followup.send(embed=self._ok(
                "Game Ready",
                "Menu screen detected. Run `/custom` to create a match.",
            ))
        else:
            self._reset_session()
            await interaction.followup.send(embed=self._fail(
                "Launch Failed",
                "Failed to launch or detect the menu screen. Bot reset to IDLE.\n"
                "Check `darwin_bot.log` for details.",
            ))

    def _do_launch(self) -> bool:
        import pyautogui
        from game.launcher import launch_game
        from game.screen_detection import wait_for_template, wait_for_template_center, save_error_screenshot

        exe = self.bot.config.get("game_executable_path", "")
        timeout = self.bot.config.get("launch_timeout_seconds", 60)

        # Sync deck to the server BEFORE launching, using the previous session's token.
        # If this succeeds, the game's startup profile load will already return the new deck
        # so the correct cards appear without needing a second launch.
        if _try_force_sync():
            logger.info("Pre-launch deck sync succeeded — game will load correct deck at startup")
        else:
            logger.warning("Pre-launch deck sync failed (token expired?) — deck may require a second launch to reflect")

        if not launch_game(exe, timeout):
            return False

        if self._stop_event.is_set():
            return False

        # Poll for splash or main menu — whichever appears first.
        # Polling both avoids a hard timeout on the splash that either misses it
        # (too short) or wastes time on machines that skip straight to the menu.
        logger.info("Waiting for splash screen or main menu...")
        import time as _time
        from game.screen_detection import find_template, take_screenshot
        splash_center = None
        at_menu = False
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            ss = take_screenshot()
            splash = find_template(ss, "templates/latest_updates_continue.png")
            if splash:
                splash_center = splash
                break
            if find_template(ss, "templates/play_button.png"):
                at_menu = True
                break
            _time.sleep(1.0)

        if self._stop_event.is_set():
            return False

        if at_menu:
            logger.info("Main menu detected directly — no splash to dismiss")
        elif splash_center:
            logger.info("Splash screen detected — clicking Continue at %s", splash_center)
            try:
                pyautogui.moveTo(*splash_center)
                _time.sleep(1.0)
                pyautogui.click()
                _time.sleep(0.5)
            except Exception as e:
                logger.error("Mouse action failed during launch: %s", e)
                save_error_screenshot("click_failed_launch")
                return False
        else:
            logger.warning("Neither splash nor main menu detected within %ds", timeout)
            return False

        if self._stop_event.is_set():
            return False

        if not at_menu:
            remaining = max(10, int(deadline - _time.monotonic()))
            if not wait_for_template("templates/play_button.png", timeout=remaining):
                logger.error("Main menu not detected after dismissing splash")
                save_error_screenshot("menu_not_found_after_splash")
                return False

        if self._stop_event.is_set():
            return False

        return True

    # ------------------------------------------------------------------
    # /deck
    # ------------------------------------------------------------------

    @app_commands.command(name="deck", description="View and edit the Director deck")
    async def deck(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return

        view = DeckEditorView(_read_deck())
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    # ------------------------------------------------------------------
    # /profile
    # ------------------------------------------------------------------

    @app_commands.command(name="profile", description="View and set the active Director match profile")
    async def profile(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return
        active = self.bot.config.get("active_profile", "standard")
        view = _ProfileSelectView(self, active)
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    # ------------------------------------------------------------------
    # /custom
    # ------------------------------------------------------------------

    @app_commands.command(
        name="custom",
        description="Create a private custom match and return the lobby code",
    )
    @app_commands.describe(region="Server region for the match")
    @app_commands.choices(region=[
        app_commands.Choice(name="NA — US East (N. Virginia)", value="NA"),
        app_commands.Choice(name="EU — Frankfurt", value="EU"),
    ])
    async def custom(self, interaction: discord.Interaction, region: app_commands.Choice[str]):
        if not await self._role_check(interaction):
            return
        if not await self._state_check(interaction, "custom"):
            return
        if not await self._lock_check(interaction):
            return

        from game.profiles import resolve_profile
        from game.deck_utils import deck_layout_from_state, validate_profile_deck
        _active_key = self.bot.config.get("active_profile", "standard")
        self._resolved_profile = resolve_profile(_active_key)
        if _active_key == "randomizer":
            logger.info("Profile selected by randomizer: %s", self._resolved_profile["display_name"])
        else:
            logger.info("Profile: %s", self._resolved_profile["display_name"])

        _deck_layout = deck_layout_from_state()
        if _deck_layout:
            _warnings = validate_profile_deck(self._resolved_profile, _deck_layout)
            if _warnings:
                _name = self._resolved_profile["display_name"]
                self._resolved_profile = None
                await interaction.response.send_message(embed=self._fail(
                    "Deck / Profile Mismatch",
                    f"The **{_name}** profile requires cards not in the deck:\n"
                    + "\n".join(f"• {w}" for w in _warnings),
                ))
                return

        await interaction.response.defer(thinking=True)

        async with self._session_lock:
            self._stop_event.clear()
            loop = asyncio.get_running_loop()
            try:
                import functools
                lobby_code = await asyncio.wait_for(
                    loop.run_in_executor(None, functools.partial(self._do_create_custom, region.value)),
                    timeout=_CUSTOM_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._stop_event.set()
                self._reset_session()
                await interaction.followup.send(embed=self._fail(
                    "Custom Match Timed Out",
                    f"Match creation did not complete within {int(_CUSTOM_TIMEOUT)}s. Bot reset to IDLE.\n"
                    "Check `darwin_bot.log` for details.",
                ))
                return

        if lobby_code:
            self.bot.session.transition(
                BotState.IN_CUSTOM,
                last_action="Custom match created",
                next_action="Await /start",
            )
            _profile_label = "Randomizer" if _active_key == "randomizer" else self._resolved_profile["display_name"]
            embed = self._ok("Custom Match Ready", "Private lobby created. Share the code with your players.")
            embed.add_field(name="Region", value=region.name, inline=True)
            embed.add_field(name="Profile", value=_profile_label, inline=True)
            embed.add_field(name="Lobby Code", value=f"```{lobby_code}```", inline=False)
            await interaction.followup.send(embed=embed)

            # Start a background watcher that fires the match runner if the lobby
            # auto-starts before /start is called.
            self._auto_start_task = asyncio.create_task(
                self._watch_for_auto_start(interaction.channel)
            )
        else:
            self._reset_session()
            await interaction.followup.send(embed=self._fail(
                "Custom Match Failed",
                "Could not complete match setup. Bot reset to IDLE.\n"
                "Check `darwin_bot.log` for details.",
            ))

    def _do_create_custom(self, region: str) -> Optional[str]:
        import time
        import pyautogui
        import pyperclip
        from game.screen_detection import wait_for_template_center, save_error_screenshot, take_screenshot, find_template

        # All coordinates calibrated for 1920×1080.
        # Sequence: PLAY → set region → BACK → CUSTOM → Create New → set PRIVATE → SOLO CLASSIC → START → DIRECTOR → copy password

        def stopped() -> bool:
            return self._stop_event.is_set()

        def hover_click(x: int, y: int, delay: float = 0.8) -> bool:
            """Move to (x, y), pause to trigger hover state, click, then wait."""
            if stopped():
                return False
            pyautogui.moveTo(x, y)
            time.sleep(0.2)          # let the game register MouseEnter / highlight
            pyautogui.click()
            time.sleep(delay)
            return not stopped()

        # Keep a simple alias for readability in the flow below.
        click = hover_click

        def click_until(x: int, y: int, verify_template: str, verify_timeout: int = 5,
                        max_attempts: int = 3, delay: float = 0.8):
            """
            Hover over (x, y), click, then verify the result by waiting for verify_template.
            On retry, moves the mouse to a neutral position first to force a fresh
            MouseEnter / hover-highlight on the next approach.
            Returns the matched center on success, None on failure or stop.
            """
            for attempt in range(max_attempts):
                if stopped():
                    return None
                if attempt:
                    logger.warning("click_until: retry %d/%d at (%d, %d) for %s",
                                   attempt, max_attempts - 1, x, y, verify_template)
                    # Move away first so the game fires a fresh hover event on re-approach
                    pyautogui.moveTo(960, 300)
                    time.sleep(0.3)
                pyautogui.moveTo(x, y)
                time.sleep(0.2)      # hover pause — game highlights the button
                pyautogui.click()
                time.sleep(delay)
                if stopped():
                    return None
                result = wait_for_template_center(verify_template, timeout=verify_timeout, poll_interval=0.5)
                if result:
                    return result
            logger.error("click_until: no result after %d attempts at (%d, %d)", max_attempts, x, y)
            return None

        try:
            # 0. Navigate to PLAY screen and verify/set the region
            logger.info("Custom: opening PLAY screen for region check (want %s)", region)
            if not hover_click(355, 258):   # orange PLAY button on main menu
                return None
            if not wait_for_template_center("templates/play_screen_region.png", timeout=8, poll_interval=0.5):
                logger.error("Custom: PLAY screen not detected")
                save_error_screenshot("play_screen_not_found")
                return None

            screenshot = take_screenshot()
            na_match = find_template(screenshot, "templates/region_na.png")
            eu_match = find_template(screenshot, "templates/region_eu.png")
            wants_na = region.upper() == "NA"
            region_correct = (wants_na and na_match) or (not wants_na and eu_match)
            logger.info("Custom: current region — NA=%s EU=%s  correct=%s", bool(na_match), bool(eu_match), region_correct)

            if not region_correct:
                # Open the region popup and select the right row.
                # Row positions are fixed — we use hardcoded coords rather than
                # template-matching because the game highlights the active row
                # white, which breaks template detection.
                _REGION_ROW = {"NA": (740, 468), "EU": (720, 511)}
                logger.info("Custom: changing region to %s", region)
                if not hover_click(215, 1045, delay=0.5):   # CHANGE REGION button (bottom-left)
                    return None
                if not wait_for_template_center("templates/region_popup_header.png", timeout=8, poll_interval=0.5):
                    logger.error("Custom: region popup not detected")
                    save_error_screenshot("region_popup_not_found")
                    return None
                # Move cursor away from the popup rows so the hover highlight
                # doesn't interfere before we make our deliberate click.
                pyautogui.moveTo(960, 700)
                time.sleep(0.3)
                row_x, row_y = _REGION_ROW[region.upper()]
                # Click the row — popup closes automatically and region updates
                if not click_until(row_x, row_y, "templates/play_screen_region.png", verify_timeout=6):
                    logger.error("Custom: PLAY screen not detected after region selection")
                    save_error_screenshot("play_screen_not_found_after_region")
                    return None
            else:
                logger.info("Custom: region already %s, no change needed", region)

            # Return to main menu from PLAY screen
            logger.info("Custom: clicking BACK to return to main menu")
            back_center = wait_for_template_center("templates/play_screen_back.png", timeout=5, poll_interval=0.5)
            if not back_center:
                back_center = (1840, 1044)
            if not click_until(*back_center, "templates/play_button.png", verify_timeout=8):
                logger.error("Custom: main menu not detected after BACK from PLAY screen")
                save_error_screenshot("main_menu_not_found_after_play")
                return None

            # 1. Click CUSTOM button on main menu
            logger.info("Custom: clicking CUSTOM button")
            if not click(226, 333):
                return None

            # 2. Wait for the match browser, then click CREATE NEW CUSTOM MATCH
            logger.info("Custom: waiting for match browser")
            center = wait_for_template_center("templates/create_custom_match.png", timeout=10)
            if not center or stopped():
                logger.error("Custom: match browser not detected")
                save_error_screenshot("custom_browser_not_found")
                return None
            if not click(*center):
                return None

            # 3. Wait for Create Match screen — SOLO CLASSIC card is always visible here
            #    regardless of the privacy setting, making it a reliable screen gate.
            logger.info("Custom: waiting for Create Match screen")
            center_sc = wait_for_template_center("templates/solo_classic_label.png", timeout=10, poll_interval=0.5)
            if not center_sc or stopped():
                logger.error("Custom: Create Match screen not detected")
                save_error_screenshot("create_match_not_found")
                return None

            # 4. Ensure Privacy is PRIVATE — only toggle if not already set
            logger.info("Custom: checking privacy setting")
            if find_template(take_screenshot(), "templates/privacy_private.png"):
                logger.info("Custom: already PRIVATE, skipping toggle")
            else:
                logger.info("Custom: not PRIVATE — clicking arrow to set PRIVATE")
                if not click(90, 119):
                    return None

            # 5+6. Click SOLO CLASSIC and verify START button lights up.
            # Retries up to 3x if the click doesn't register — START only activates
            # after a mode is selected, so it's the definitive confirmation.
            logger.info("Custom: selecting SOLO CLASSIC")
            center_start = click_until(*center_sc, "templates/start_button.png", verify_timeout=5)
            if not center_start or stopped():
                logger.error("Custom: START button did not activate after SOLO CLASSIC")
                save_error_screenshot("start_button_not_found")
                return None

            # 6+7. Click START and verify Choose Role screen appears.
            # Retries up to 3x if the click doesn't register.
            logger.info("Custom: clicking START")
            role_center = click_until(*center_start, "templates/choose_role_screen.png", verify_timeout=8)
            if not role_center or stopped():
                logger.error("Custom: Choose Role screen not detected after START")
                save_error_screenshot("choose_role_not_found")
                return None

            # 8. Click DIRECTOR role card.
            # Template matches the label strip at ~y=575; card body is ~175px above.
            director_x = role_center[0]
            director_y = role_center[1] - 175
            logger.info("Custom: clicking DIRECTOR card at (%d, %d)", director_x, director_y)
            if not click(director_x, director_y):
                return None

            # 9. Wait for Director lobby with MATCH PASSWORD label
            logger.info("Custom: waiting for Director lobby")
            center = wait_for_template_center("templates/lobby_password_label.png", timeout=80)
            if not center or stopped():
                logger.error("Custom: Director lobby not detected")
                save_error_screenshot("lobby_not_found")
                return None

            # Wait for the lobby to fully settle — game can lag here and a clipboard
            # copy attempted too early will return empty even if the icon is visible.
            time.sleep(10.0)
            if stopped():
                return None

            # Clipboard icon is 316 px right and 9 px above the MATCH PASSWORD label center
            clipboard_x = center[0] + 316
            clipboard_y = center[1] - 9
            logger.info("Custom: clicking clipboard icon at (%d, %d)", clipboard_x, clipboard_y)
            if not click(clipboard_x, clipboard_y, delay=0.5):
                return None

            code = pyperclip.paste().strip()
            if not code:
                logger.error("Custom: clipboard empty after copy button")
                save_error_screenshot("empty_lobby_code")
                return None

            logger.info("Custom: lobby code = %s", code)

            # Close the password menu and dismiss the initial card tray
            from game.card_actions import press_key
            press_key("escape")
            time.sleep(0.3)
            press_key("shift")

            return code

        except Exception as e:
            logger.error("_do_create_custom failed: %s", e, exc_info=True)
            save_error_screenshot("create_custom_exception")
            return None

    # ------------------------------------------------------------------
    # /menu
    # ------------------------------------------------------------------

    @app_commands.command(
        name="menu",
        description="Navigate back to the main menu from any custom-match screen",
    )
    async def menu(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return
        if not await self._state_check(interaction, "menu"):
            return
        if not await self._lock_check(interaction):
            return

        await interaction.response.defer(thinking=True)

        async with self._session_lock:
            self._stop_event.clear()
            loop = asyncio.get_running_loop()
            try:
                success = await asyncio.wait_for(
                    loop.run_in_executor(None, self._do_go_to_menu),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                self._stop_event.set()
                self._reset_session()
                await interaction.followup.send(embed=self._fail(
                    "Menu Navigation Timed Out",
                    "Did not reach the main menu within 60s. Bot reset to IDLE.\n"
                    "Check `darwin_bot.log` for details.",
                ))
                return

        if success:
            self.bot.session.transition(
                BotState.IN_MENU,
                last_action="Returned to main menu",
                next_action="Await /deck or /custom",
            )
            await interaction.followup.send(embed=self._ok(
                "Back at Main Menu",
                "Lobby closed. Run `/custom` to create a new match.",
            ))
        else:
            self._reset_session()
            await interaction.followup.send(embed=self._fail(
                "Menu Navigation Failed",
                "Could not return to the main menu. Bot reset to IDLE.\n"
                "Check `darwin_bot.log` for details.",
            ))

    def _do_go_to_menu(self) -> bool:
        import time
        import pyautogui
        from game.screen_detection import (
            detect_current_screen, wait_for_template_center,
            find_template, take_screenshot, save_error_screenshot,
        )
        from game.card_actions import press_key

        def stopped() -> bool:
            return self._stop_event.is_set()

        def hover_click(x: int, y: int, delay: float = 0.8) -> bool:
            if stopped():
                return False
            pyautogui.moveTo(x, y)
            time.sleep(0.2)
            pyautogui.click()
            time.sleep(delay)
            return not stopped()

        try:
            for step in range(8):
                if stopped():
                    return False

                screen = detect_current_screen()
                logger.info("Menu: step %d — screen detected: %s", step, screen)

                if screen == "main_menu":
                    return True

                if screen == "lobby_open":
                    # Lobby with password overlay dismissed — press ESC to reopen it,
                    # then the next loop iteration handles it as director_lobby.
                    press_key("escape")
                    time.sleep(1.5)
                    continue

                if screen == "director_lobby":
                    # Lobby has a dedicated MAIN MENU button instead of BACK
                    center = wait_for_template_center("templates/main_menu_button.png", timeout=5)
                    if not center or stopped():
                        logger.error("Menu: MAIN MENU button not found in lobby")
                        save_error_screenshot("main_menu_button_not_found")
                        return False
                    if not hover_click(*center):
                        return False
                    # Confirmation popup
                    yes_center = wait_for_template_center("templates/yes_button.png", timeout=8)
                    if not yes_center or stopped():
                        logger.error("Menu: YES button not found in confirmation popup")
                        save_error_screenshot("menu_confirm_not_found")
                        return False
                    if not hover_click(*yes_center):
                        return False

                elif screen == "director_splash":
                    # Splash screen — click CONTINUE to get past it first
                    center = wait_for_template_center("templates/latest_updates_continue.png", timeout=5)
                    if not center or stopped():
                        logger.error("Menu: CONTINUE button not found on splash screen")
                        save_error_screenshot("splash_continue_not_found")
                        return False
                    if not hover_click(*center):
                        return False

                elif screen == "region_popup":
                    # Region selection popup — BACK closes it and returns to play_screen.
                    # play_screen_back.png also matches the popup's BACK button (score 0.91).
                    center = wait_for_template_center("templates/play_screen_back.png", timeout=5)
                    if not center:
                        center = (960, 657)
                    if not hover_click(*center):
                        return False

                elif screen == "play_screen":
                    # PLAY mode-selection screen — has a different dark-bordered BACK button
                    center = wait_for_template_center("templates/play_screen_back.png", timeout=5)
                    if not center:
                        center = (1840, 1044)
                    if not hover_click(*center):
                        return False

                else:
                    # choose_role, create_match, custom_browser — all have a BACK button
                    # bottom-right. Fall through to BACK for any unrecognised screen too.
                    center = wait_for_template_center("templates/back_button.png", timeout=5)
                    if not center:
                        # Template didn't match (different hover state?) — use known position
                        logger.warning("Menu: back_button template not matched, using fixed position")
                        center = (1815, 1017)
                    if not hover_click(*center):
                        return False

                # Allow the screen transition to settle before re-detecting
                time.sleep(1.5)

            logger.error("Menu: step limit reached without detecting main menu")
            save_error_screenshot("menu_step_limit")
            return False

        except Exception as e:
            logger.error("_do_go_to_menu failed: %s", e, exc_info=True)
            save_error_screenshot("go_to_menu_exception")
            return False

    def _do_post_match_return(self) -> bool:
        """Click MAIN MENU on the results screen and wait for the main menu to appear."""
        import time
        import pyautogui
        from game.screen_detection import (
            find_template, take_screenshot, wait_for_template_center, save_error_screenshot,
        )
        from game.card_actions import focus_darwin_window

        try:
            focus_darwin_window()
            time.sleep(0.3)

            screenshot = take_screenshot()
            center = find_template(screenshot, "templates/placement_badge.png", threshold=0.88)
            if not center:
                logger.warning("Post-match: MAIN MENU button not found — skipping return to menu")
                save_error_screenshot("post_match_main_menu_not_found")
                return False

            pyautogui.moveTo(*center)
            time.sleep(0.2)
            pyautogui.click()
            logger.info("Post-match: clicked MAIN MENU at %s", center)

            menu = wait_for_template_center("templates/play_button.png", timeout=20)
            if menu:
                logger.info("Post-match: main menu detected")
                return True
            logger.warning("Post-match: timed out waiting for main menu")
            save_error_screenshot("post_match_menu_timeout")
            return False

        except Exception as e:
            logger.error("_do_post_match_return failed: %s", e, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # /start
    # ------------------------------------------------------------------

    @app_commands.command(
        name="start",
        description="Start the match. Bot responds with results when the match ends.",
    )
    async def start(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return
        if not await self._state_check(interaction, "start"):
            return
        if not await self._lock_check(interaction):
            return

        # Manual start — cancel any pending auto-start watcher
        if self._auto_start_task and not self._auto_start_task.done():
            self._auto_start_task.cancel()
            self._auto_start_task = None

        from game.profiles import resolve_profile, profile_summary
        _profile = self._resolved_profile or resolve_profile(self.bot.config.get("active_profile", "standard"))

        _first = min(_profile["card_plays"], key=lambda p: p["play_time_seconds"])
        _m, _s = divmod(_first["play_time_seconds"], 60)
        _first_label = f"{_first['card'].replace('_', ' ').title()} at {_m}:{_s:02d}"
        self.bot.session.transition(
            BotState.MATCH_IN_PROGRESS,
            last_action="Match started",
            next_action=_first_label,
        )
        self.bot.session.start_match_timer()

        def on_action_update(last: str, next_: str):
            self.bot.session.transition(self.bot.session.state, last_action=last, next_action=next_)

        runner = MatchRunner(
            config=self.bot.config,
            session=self.bot.session,
            on_action_update=on_action_update,
            profile=self._resolved_profile,
        )
        self._resolved_profile = None
        self._active_runner = runner

        # Respond immediately — match runs in the background so the interaction
        # token never expires waiting for results.
        start_embed = self._info(
            "Match In Progress",
            f"Match started. First card: **{_first_label}**\n"
            "Results will be posted here when the match ends.",
        )
        await interaction.response.send_message(embed=start_embed)

        channel = interaction.channel

        async def _run_match():
            async with self._session_lock:
                loop = asyncio.get_running_loop()
                try:
                    results_text = await asyncio.wait_for(
                        loop.run_in_executor(None, runner.run),
                        timeout=_MATCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    if self._active_runner is not None:
                        self._active_runner.stop()
                    self._active_runner = None
                    self._reset_session()
                    await channel.send(embed=self._fail(
                        "Match Safety Timeout",
                        f"Match exceeded the {int(_MATCH_TIMEOUT // 60)}-minute safety limit. "
                        "Bot reset to IDLE.\nCheck `darwin_bot.log` for details.",
                    ))
                    return

            self._active_runner = None
            self.bot.session.transition(
                BotState.MATCH_ENDED,
                last_action="Match ended",
                next_action="Returning to menu",
            )
            if results_text.endswith(".png"):
                await channel.send(
                    embed=self._info("Match Complete", "Results screenshot attached."),
                    file=discord.File(results_text),
                )
            else:
                await channel.send(embed=self._info("Match Complete", results_text))

            loop = asyncio.get_running_loop()
            returned = await loop.run_in_executor(None, self._do_post_match_return)
            if returned:
                self.bot.session.transition(
                    BotState.IN_MENU,
                    last_action="Returned to main menu",
                    next_action="Await /custom",
                )
            else:
                self._reset_session()

        asyncio.ensure_future(_run_match())

    # ------------------------------------------------------------------
    # Auto-start watcher
    # ------------------------------------------------------------------

    async def _watch_for_auto_start(self, channel: discord.TextChannel):
        """
        Started after /custom creates a lobby. OCRs the on-screen countdown,
        sleeps until it expires, then fires the match runner automatically
        (skip_start=True — no B press needed since the game already started).
        Cancelled immediately if /start is called manually first.
        """
        try:
            loop = asyncio.get_running_loop()
            from game.screen_detection import take_screenshot
            from game.ocr import read_lobby_countdown

            screenshot = await loop.run_in_executor(None, take_screenshot)
            countdown = await loop.run_in_executor(
                None, lambda: read_lobby_countdown(screenshot, debug=True)
            )

            if countdown is None:
                logger.warning("Auto-start watcher: could not read lobby countdown — watcher inactive")
                return

            logger.info("Auto-start watcher: lobby expires in %ds", countdown)
            self._lobby_expiry = time.monotonic() + countdown
            await asyncio.sleep(max(0, countdown))
            self._lobby_expiry = None

            # Re-check — /start may have been called while we were sleeping
            if self.bot.session.state != BotState.IN_CUSTOM:
                logger.info("Auto-start watcher: state is %s — aborting", self.bot.session.state.name)
                return
            if self._session_lock.locked():
                logger.info("Auto-start watcher: session lock held — aborting")
                return

            logger.info("Auto-start watcher: firing match runner")
            await channel.send(embed=self._info(
                "Match Auto-Started",
                "Lobby timer expired — card timers are now running.",
            ))

            from game.profiles import resolve_profile
            _profile = resolve_profile(self.bot.config.get("active_profile", "standard"))
            _first = min(_profile["card_plays"], key=lambda p: p["play_time_seconds"])
            _m, _s = divmod(_first["play_time_seconds"], 60)
            _first_label = f"{_first['card'].replace('_', ' ').title()} at {_m}:{_s:02d}"

            self.bot.session.transition(
                BotState.MATCH_IN_PROGRESS,
                last_action="Match auto-started (lobby timer)",
                next_action=_first_label,
            )
            self.bot.session.start_match_timer()

            def on_action_update(last: str, next_: str):
                self.bot.session.transition(self.bot.session.state, last_action=last, next_action=next_)

            runner = MatchRunner(
                config=self.bot.config,
                session=self.bot.session,
                on_action_update=on_action_update,
                skip_start=True,
                profile=self._resolved_profile,
            )
            self._resolved_profile = None
            self._active_runner = runner

            async with self._session_lock:
                loop = asyncio.get_running_loop()
                try:
                    results_text = await asyncio.wait_for(
                        loop.run_in_executor(None, runner.run),
                        timeout=_MATCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    if self._active_runner is not None:
                        self._active_runner.stop()
                    self._active_runner = None
                    self._reset_session()
                    await channel.send(embed=self._fail(
                        "Match Safety Timeout",
                        f"Match exceeded the {int(_MATCH_TIMEOUT // 60)}-minute safety limit. "
                        "Bot reset to IDLE.",
                    ))
                    return

            self._active_runner = None
            self.bot.session.transition(
                BotState.MATCH_ENDED,
                last_action="Match ended",
                next_action="Returning to menu",
            )
            if results_text.endswith(".png"):
                await channel.send(
                    embed=self._info("Match Complete", "Results screenshot attached."),
                    file=discord.File(results_text),
                )
            else:
                await channel.send(embed=self._info("Match Complete", results_text))

            loop = asyncio.get_running_loop()
            returned = await loop.run_in_executor(None, self._do_post_match_return)
            if returned:
                self.bot.session.transition(
                    BotState.IN_MENU,
                    last_action="Returned to main menu",
                    next_action="Await /custom",
                )
            else:
                self._reset_session()

        except asyncio.CancelledError:
            self._lobby_expiry = None
            logger.info("Auto-start watcher cancelled (/start used manually)")

    # ------------------------------------------------------------------
    # /voice — Discord voice channel mirroring for remote TTS monitoring
    # ------------------------------------------------------------------

    voice = app_commands.Group(name="voice", description="Mirror TTS audio to a Discord voice channel")

    @voice.command(name="join", description="Join your voice channel to hear TTS announcements")
    async def voice_join(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                embed=self._fail(
                    "Not in Voice",
                    "Join a Discord voice channel first, then run this command.",
                ),
                ephemeral=True,
            )
            return

        channel = member.voice.channel

        # Disconnect from any existing voice channel before joining the new one
        existing = interaction.guild.voice_client
        if existing:
            from game import tts as _tts
            _tts.set_voice_client(None)
            await existing.disconnect(force=True)

        vc = await channel.connect()

        from game import tts
        tts.set_voice_client(vc)

        await interaction.response.send_message(
            embed=self._ok(
                "Voice Connected",
                f"Joined **{channel.name}**. All TTS audio will now play here.",
            ),
            ephemeral=True,
        )

    @voice.command(name="leave", description="Leave the voice channel")
    async def voice_leave(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return

        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message(
                embed=self._fail("Not Connected", "Bot is not in a voice channel."),
                ephemeral=True,
            )
            return

        from game import tts
        tts.set_voice_client(None)
        await vc.disconnect(force=True)

        await interaction.response.send_message(
            embed=self._ok("Voice Disconnected", "Left the voice channel."),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /say
    # ------------------------------------------------------------------

    @app_commands.command(name="say", description="Speak a message through in-game voice chat via TTS")
    @app_commands.describe(message="Text to speak aloud to players")
    async def say(self, interaction: discord.Interaction, message: str):
        if not await self._role_check(interaction):
            return

        from game import tts
        if not tts.is_enabled():
            await interaction.response.send_message(
                embed=self._fail("TTS Disabled", "No TTS device configured. Add `tts_device` to config.json."),
                ephemeral=True,
            )
            return

        in_match = self.bot.session.state == BotState.MATCH_IN_PROGRESS
        tts.speak(message, broadcast=in_match)
        context_note = "via broadcast (G key)" if in_match else "via game voice chat"
        embed = self._ok("Director Says", f"_{message}_")
        embed.add_field(name="Sent by", value=interaction.user.display_name, inline=True)
        embed.add_field(name="Method", value=context_note, inline=True)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------

    @app_commands.command(name="status", description="Show current bot state and last/next action")
    async def status(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return

        session = self.bot.session
        state = session.state
        color = self._STATE_COLORS.get(state, _COLOR_NEUTRAL)
        state_label = state.name.replace("_", " ").title()

        embed = discord.Embed(title="Darwin Director", color=color)
        embed.add_field(name="State", value=state_label, inline=True)
        if state == BotState.MATCH_IN_PROGRESS:
            embed.add_field(name="Match Timer", value=session.match_elapsed_display(), inline=True)
        if state == BotState.IN_CUSTOM and self._lobby_expiry is not None:
            remaining = max(0, self._lobby_expiry - time.monotonic())
            rm, rs = divmod(int(remaining), 60)
            embed.add_field(name="Lobby Expires", value=f"{rm}:{rs:02d}", inline=True)
        embed.add_field(name="Last Action", value=session.last_action, inline=False)
        embed.add_field(name="Next Action", value=session.next_action, inline=False)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /quit
    # ------------------------------------------------------------------

    @app_commands.command(
        name="quit",
        description="Force close the game and reset the bot to IDLE.",
    )
    async def quit(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return

        if self.bot.session.state == BotState.IDLE:
            await interaction.response.send_message(embed=self._info(
                "Nothing to End", "Bot is already IDLE."
            ))
            return

        in_match = self.bot.session.state == BotState.MATCH_IN_PROGRESS
        description = (
            "A match is currently in progress. Closing the game will forfeit the match.\n\n"
            "Are you sure?"
            if in_match else
            "This will close the game and reset the bot to IDLE.\n\nAre you sure?"
        )
        embed = self._embed("End Session", description, color=_COLOR_WARN)
        await interaction.response.send_message(
            embed=embed,
            view=_EndConfirmView(self),
        )

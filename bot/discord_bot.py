import asyncio
import logging
import threading
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from game.match_runner import MatchRunner
from session.state import SessionState, BotState

logger = logging.getLogger(__name__)

# Hard safety ceilings for each long-running operation.
_LAUNCH_TIMEOUT = 420.0    # 7 min: game start + splash + menu detection (slow HDD installs)
_CUSTOM_TIMEOUT = 180.0    # 3 min: menu navigation to lobby (lobby load can be slow)
_MATCH_TIMEOUT  = 1200.0   # 20 min: full match safety net

# Embed accent colors
_COLOR_OK      = 0x2ECC71  # green   — success
_COLOR_FAIL    = 0xE74C3C  # red     — failure / error
_COLOR_ACTIVE  = 0x3498DB  # blue    — launching / in-progress
_COLOR_WARN    = 0xE67E22  # orange  — active match / warning
_COLOR_NEUTRAL = 0x95A5A6  # gray    — idle / neutral


class DarwinBot(commands.Bot):
    def __init__(self, config: dict, session: SessionState):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.session = session

    async def setup_hook(self):
        cog = DirectorCog(self)
        await self.add_cog(cog)
        await self.tree.sync()
        logger.info("Slash commands synced")
        self.loop.create_task(cog._screen_watcher())

    async def on_ready(self):
        logger.info("Logged in as %s (id %d)", self.user, self.user.id)


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

    # Screen name → (BotState, label) used by the background screen watcher
    _SCREEN_STATES = {
        "main_menu":       (BotState.IN_MENU,   "Main menu"),
        "custom_browser":  (BotState.IN_MENU,   "Custom match browser"),
        "create_match":    (BotState.IN_MENU,   "Create Match screen"),
        "director_lobby":  (BotState.IN_CUSTOM, "Director lobby"),
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
            except Exception as e:
                logger.debug("Screen watcher error: %s", e)

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _has_role(self, interaction: discord.Interaction) -> bool:
        required = self.bot.config.get("discord_required_role", "")
        return any(r.name == required for r in interaction.user.roles)

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
                self.bot.session.reset()
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
                "Menu screen detected.\nRun `/deck` to check your deck or `/custom` to create a match.",
            ))
        else:
            self.bot.session.reset()
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

        if not launch_game(exe, timeout):
            return False

        if self._stop_event.is_set():
            return False

        # Short timeout — splash appears within seconds of launch or not at all.
        # Using full timeout here would block 3 minutes if the game is already at the menu.
        logger.info("Waiting for Latest Updates splash screen...")
        center = wait_for_template_center("templates/latest_updates_continue.png", timeout=20)

        if self._stop_event.is_set():
            return False

        if center:
            logger.info("Splash screen detected — focusing window then clicking Continue at %s", center)
            try:
                import time
                pyautogui.moveTo(*center)   # move first so the window activates
                time.sleep(1.0)             # wait for focus to settle before clicking
                pyautogui.click(*center)
                time.sleep(0.5)
            except Exception as e:
                logger.error("Mouse action failed during launch: %s", e)
                save_error_screenshot("click_failed_launch")
                return False
        else:
            logger.warning("Splash screen not detected — proceeding anyway")

        if self._stop_event.is_set():
            return False

        return wait_for_template("templates/play_button.png", timeout=timeout)

    # ------------------------------------------------------------------
    # /deck
    # ------------------------------------------------------------------

    @app_commands.command(
        name="deck",
        description="Check the Director deck. Optionally provide card names to swap in.",
    )
    @app_commands.describe(cards="Comma-separated card names to swap into the deck (optional)")
    async def deck(self, interaction: discord.Interaction, cards: Optional[str] = None):
        if not await self._role_check(interaction):
            return
        if not await self._state_check(interaction, "deck"):
            return
        if not await self._lock_check(interaction):
            return

        await interaction.response.defer(thinking=True)

        async with self._session_lock:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._do_deck_check, cards)

        await interaction.followup.send(embed=self._info("Director Deck", result))

    def _do_deck_check(self, cards: Optional[str]) -> str:
        # TODO: click Director Deck tab, screenshot, compare against config card list
        msg = "Deck check not yet implemented — UI navigation pending calibration."
        if cards:
            msg += f"\nRequested swap: `{cards}`"
        return msg

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
                self.bot.session.reset()
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
            embed = self._ok("Custom Match Ready", "Private lobby created. Share the code with your players.")
            embed.add_field(name="Region", value=region.name, inline=True)
            embed.add_field(name="Lobby Code", value=f"```{lobby_code}```", inline=False)
            await interaction.followup.send(embed=embed)
        else:
            self.bot.session.reset()
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
                self.bot.session.reset()
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
            self.bot.session.reset()
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

        await interaction.response.defer(thinking=True)

        self.bot.session.transition(
            BotState.MATCH_IN_PROGRESS,
            last_action="Match started",
            next_action="Electromania at 2:00",
        )
        self.bot.session.start_match_timer()

        def on_action_update(last: str, next_: str):
            self.bot.session.transition(self.bot.session.state, last_action=last, next_action=next_)

        runner = MatchRunner(
            config=self.bot.config,
            session=self.bot.session,
            on_action_update=on_action_update,
        )
        self._active_runner = runner

        # Match can run up to ~15 min. Discord followup tokens last exactly 15 min,
        # so this is tight. If matches regularly exceed that, switch to sending a
        # new channel message via interaction.channel.send() instead of followup.
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
                self.bot.session.reset()
                await interaction.followup.send(embed=self._fail(
                    "Match Safety Timeout",
                    f"Match exceeded the {int(_MATCH_TIMEOUT // 60)}-minute safety limit. "
                    "Bot reset to IDLE.\nCheck `darwin_bot.log` for details.",
                ))
                return

        self._active_runner = None
        self.bot.session.transition(
            BotState.MATCH_ENDED,
            last_action="Match ended",
            next_action="None",
        )
        await interaction.followup.send(embed=self._info("Match Complete", results_text))
        self.bot.session.reset()

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
        embed.add_field(name="Last Action", value=session.last_action, inline=False)
        embed.add_field(name="Next Action", value=session.next_action, inline=False)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /end
    # ------------------------------------------------------------------

    @app_commands.command(
        name="end",
        description="Force close the game. Requires confirm=True if a match is in progress.",
    )
    @app_commands.describe(confirm="Pass True to confirm force-close during an active match")
    async def end(self, interaction: discord.Interaction, confirm: bool = False):
        if not await self._role_check(interaction):
            return

        state = self.bot.session.state

        if state == BotState.IDLE:
            await interaction.response.send_message(embed=self._info(
                "Nothing to End", "Bot is already IDLE."
            ))
            return

        if state == BotState.MATCH_IN_PROGRESS and not confirm:
            await interaction.response.send_message(
                embed=self._embed(
                    "Confirm Required",
                    "A match is in progress. Run `/end confirm:True` to force-close.",
                    color=_COLOR_WARN,
                ),
                ephemeral=True,
            )
            return

        # Signal all running threads to stop.
        self._stop_event.set()
        if self._active_runner is not None:
            self._active_runner.stop()

        from game.launcher import close_game
        close_game()
        self.bot.session.reset()
        await interaction.response.send_message(embed=self._ok(
            "Session Ended", "Game closed. Bot reset to IDLE."
        ))

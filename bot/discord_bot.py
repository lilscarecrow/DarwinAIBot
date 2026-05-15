import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from game.match_runner import MatchRunner
from session.state import SessionState, BotState

logger = logging.getLogger(__name__)


class DarwinBot(commands.Bot):
    def __init__(self, config: dict, session: SessionState):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.session = session

    async def setup_hook(self):
        await self.add_cog(DirectorCog(self))
        await self.tree.sync()
        logger.info("Slash commands synced")

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
            self.bot.session.transition(
                BotState.LAUNCHING,
                last_action="Launch initiated",
                next_action="Detect menu screen",
            )
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(None, self._do_launch)

        if success:
            self.bot.session.transition(
                BotState.IN_MENU,
                last_action="Menu screen detected",
                next_action="Await /deck or /custom",
            )
            await interaction.followup.send(
                "Game launched. Menu screen detected.\nReady for `/deck` or `/custom`."
            )
        else:
            self.bot.session.reset()
            await interaction.followup.send(
                "Failed to launch or detect menu screen. Bot reset to IDLE. Check logs."
            )

    def _do_launch(self) -> bool:
        from game.launcher import launch_game
        from game.screen_detection import wait_for_template

        exe = self.bot.config.get("game_executable_path", "")
        timeout = self.bot.config.get("launch_timeout_seconds", 60)
        if not launch_game(exe, timeout):
            return False
        # TODO: replace with actual captured template path
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

        await interaction.followup.send(result)

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
    async def custom(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return
        if not await self._state_check(interaction, "custom"):
            return
        if not await self._lock_check(interaction):
            return

        await interaction.response.defer(thinking=True)

        async with self._session_lock:
            loop = asyncio.get_running_loop()
            lobby_code = await loop.run_in_executor(None, self._do_create_custom)

        if lobby_code:
            self.bot.session.transition(
                BotState.IN_CUSTOM,
                last_action="Custom match created",
                next_action="Await /start",
            )
            await interaction.followup.send(f"Custom match created. Lobby code: `{lobby_code}`")
        else:
            self.bot.session.reset()
            await interaction.followup.send(
                "Failed to create custom match. Bot reset to IDLE. Check logs."
            )

    def _do_create_custom(self) -> Optional[str]:
        # TODO: navigate Play → Custom → Create New → Solo Classic → Start → Director
        #       set private, copy lobby code from clipboard
        return None  # None signals failure until implemented

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
            results_text = await loop.run_in_executor(None, runner.run)

        self._active_runner = None
        self.bot.session.transition(
            BotState.MATCH_ENDED,
            last_action="Match ended",
            next_action="None",
        )
        await interaction.followup.send(results_text)
        self.bot.session.reset()

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------

    @app_commands.command(name="status", description="Show current bot state and last/next action")
    async def status(self, interaction: discord.Interaction):
        if not await self._role_check(interaction):
            return
        # Status is valid in all states — no state check needed
        await interaction.response.send_message(self.bot.session.status_message())

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
            await interaction.response.send_message("Bot is already IDLE — nothing to end.")
            return

        if state == BotState.MATCH_IN_PROGRESS and not confirm:
            await interaction.response.send_message(
                "Match is in progress. Run `/end confirm:True` to force close.",
                ephemeral=True,
            )
            return

        if self._active_runner is not None:
            self._active_runner.stop()

        from game.launcher import close_game
        close_game()
        self.bot.session.reset()
        await interaction.response.send_message("Game closed. Bot reset to IDLE.")

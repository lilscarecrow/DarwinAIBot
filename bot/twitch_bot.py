import asyncio
import functools
import logging

from twitchio import eventsub
from twitchio.authentication import UserTokenPayload
from twitchio.ext import commands

logger = logging.getLogger(__name__)

_VALID_POV_KEYS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "0"}


class DarwinTwitchBot(commands.Bot):
    """
    Twitch chat bot. Deliberately narrow scope: the only chat command is !pov
    (mod-only) — everything else (status, profile, etc.) is intentionally
    Discord-only and not mirrored here. Shares the same SessionState instance
    DarwinBot/DirectorCog use, so !pov's state gate can never drift out of sync
    with the actual automation state.
    """

    def __init__(self, config: dict, session):
        self._config = config
        self.session = session
        self._owner_id = str(config["twitch_owner_id"])
        super().__init__(
            client_id=config["twitch_client_id"],
            client_secret=config["twitch_client_secret"],
            bot_id=str(config["twitch_bot_id"]),
            owner_id=self._owner_id,
            prefix=config.get("twitch_command_prefix", "!"),
        )

    async def setup_hook(self) -> None:
        await self.add_component(PovComponent(self))
        # Best-effort on startup — with no token authorized yet (first run, before
        # the one-time browser OAuth step), this legitimately 403s. That must not
        # crash the whole bot (it's gathered together with the Discord bot in
        # main.py), since a crash here would take down Discord too and the user
        # would never reach the point of completing that OAuth step. The
        # subscription is retried in event_oauth_authorized() once a token actually
        # gets granted, so a fresh setup completes itself automatically.
        await self._subscribe_chat()
        logger.info("Twitch bot: setup complete, listening for !pov")

    async def _subscribe_chat(self) -> None:
        try:
            payload = eventsub.ChatMessageSubscription(broadcaster_user_id=self.owner_id, user_id=self.bot_id)
            await self.subscribe_websocket(payload=payload)
            logger.info("Twitch bot: subscribed to chat messages")
        except Exception as e:
            logger.warning(
                "Twitch bot: could not subscribe to chat yet (%s) — visit "
                "http://localhost:4343/oauth?scopes=user:bot+user:read:chat+user:write:chat+channel:bot "
                "to authorize; the subscription completes automatically afterward.",
                e,
            )

    async def event_oauth_authorized(self, payload: UserTokenPayload) -> None:
        await super().event_oauth_authorized(payload)
        await self._subscribe_chat()
        # Save immediately, during completely normal execution, rather than relying
        # on Client.close() to persist it at shutdown. That path turned out to be
        # fundamentally unreliable here: a Task that's mid-unwind from its own
        # CancelledError stays flagged "cancelling" for the whole unwind, so any
        # further await inside close() (including the one that reaches
        # save_tokens()) can get silently re-interrupted by that same pending
        # cancellation — no exception ever surfaces, since a cancelled task ending
        # in CancelledError is Python's normal outcome, not an error. Several
        # increasingly careful attempts at making close()-on-shutdown robust
        # (cancel-then-close, close-then-cancel, a shielded cleanup task) all still
        # raced under real Ctrl+C. Saving here sidesteps the whole problem: by the
        # time shutdown happens, the token's already on disk.
        try:
            await self.save_tokens()
            logger.info("Twitch bot: token saved to disk")
        except Exception:
            logger.exception("Twitch bot: failed to save token immediately after authorization")

    async def event_command_error(self, payload) -> None:
        """Silently ignore permission-guard failures (non-mod using !pov) — same
        silent-reject convention as the Discord bot's role check. Anything else
        still gets logged normally."""
        from twitchio.ext.commands import GuardFailure

        if isinstance(payload.exception, GuardFailure):
            logger.debug("Twitch bot: command rejected by guard (%s)", payload.exception)
            return
        logger.exception("Twitch bot: command error", exc_info=payload.exception)

    async def announce(self, text: str) -> None:
        """Best-effort: post a message to the broadcaster's chat. Never raises —
        mirrors the fire-and-forget convention used by ds_ingest/OBS elsewhere in
        this codebase, since a chat outage shouldn't affect anything else."""
        try:
            broadcaster = self.create_partialuser(user_id=self._owner_id)
            await broadcaster.send_message(text, sender=self.bot_id)
        except Exception as e:
            logger.warning("Twitch announce failed: %s", e)


class PovComponent(commands.Component):
    def __init__(self, bot: DarwinTwitchBot):
        self.bot = bot

    @commands.is_moderator()
    @commands.command(name="pov")
    async def pov(self, ctx: commands.Context, player: str) -> None:
        """!pov <1-9, 0> — switch the Director's camera to a player's point of view. Mod-only."""
        session = self.bot.session
        if not session.is_command_valid("pov"):
            await ctx.reply(f"Can't switch POV right now (state: {session.state.name}).")
            return

        key = player.strip()
        if key not in _VALID_POV_KEYS:
            await ctx.reply("Usage: !pov <1-9 or 0>")
            return

        from game.card_actions import press_key

        loop = asyncio.get_running_loop()
        sent = await loop.run_in_executor(
            None, functools.partial(press_key, key, no_focus_fallback=True)
        )
        if sent:
            await ctx.reply(f"Switched to player {key}'s POV.")
        else:
            await ctx.reply("Couldn't send that — Darwin window not found.")

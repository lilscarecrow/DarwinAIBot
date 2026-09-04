import asyncio
import functools
import logging
import time

from twitchio import eventsub
from twitchio.authentication import UserTokenPayload
from twitchio.ext import commands

logger = logging.getLogger(__name__)

_VALID_POV_KEYS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "0"}

# Global cooldown between channel-points-triggered POV changes from regular viewers
# (see event_custom_redemption_add). Does not apply to /pov or !pov — both are
# already mod/admin-gated with no cooldown, and mods/the broadcaster bypass this
# cooldown too when they redeem the channel-points reward themselves.
_POV_REDEMPTION_COOLDOWN_SECONDS = 60

# All scopes this bot ever requests, kept as one list so a single re-authorization
# grants everything at once instead of the user having to figure out which scope
# backs which feature. user:bot/user:read:chat/user:write:chat/channel:bot are the
# original chat-only grant; the rest were added for ads (channel:edit:commercial),
# sub/resub/gift-sub shoutouts (channel:read:subscriptions), cheer shoutouts
# (bits:read), the channel-points POV reward (channel:read:redemptions +
# channel:manage:redemptions, the latter so redemptions can be fulfilled/refunded
# instead of sitting UNFULFILLED forever in the dashboard), and checking whether a
# channel-points redeemer is a mod (moderation:read, for the cooldown bypass below).
_OAUTH_SCOPES = (
    "user:bot+user:read:chat+user:write:chat+channel:bot"
    "+channel:edit:commercial+channel:read:subscriptions+bits:read"
    "+channel:read:redemptions+channel:manage:redemptions+moderation:read"
)
_OAUTH_URL = f"http://localhost:4343/oauth?scopes={_OAUTH_SCOPES}"


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
        # monotonic timestamp: channel-points POV redemptions from non-mods are
        # rejected (refunded) until this passes — see event_custom_redemption_add.
        self._pov_redemption_available_at: float = 0.0
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
        await self._subscribe_events()
        logger.info("Twitch bot: setup complete, listening for !pov")

    async def _subscribe_chat(self) -> None:
        try:
            payload = eventsub.ChatMessageSubscription(broadcaster_user_id=self.owner_id, user_id=self.bot_id)
            await self.subscribe_websocket(payload=payload)
            logger.info("Twitch bot: subscribed to chat messages")
        except Exception as e:
            logger.warning(
                "Twitch bot: could not subscribe to chat yet (%s) — visit %s "
                "to authorize; the subscription completes automatically afterward.",
                e, _OAUTH_URL,
            )

    async def _subscribe_events(self) -> None:
        """Subscribe to the non-chat EventSub types this bot reacts to: subs, gift
        subs, resub messages, cheers, and channel-points redemptions. Each one
        subscribes independently (its own try/except) so a scope that hasn't been
        granted yet for one of them (e.g. re-authorization with the full
        _OAUTH_SCOPES list hasn't happened) doesn't block the others from working.
        Retried the same way as chat — best-effort here on startup (harmlessly
        failing pre-authorization) and again from event_oauth_authorized() once a
        token is actually granted, so nothing needs a restart once the scopes land.
        """
        subs = (
            (eventsub.ChannelSubscribeSubscription(broadcaster_user_id=self.owner_id), "new subs"),
            (eventsub.ChannelSubscriptionGiftSubscription(broadcaster_user_id=self.owner_id), "gift subs"),
            (eventsub.ChannelSubscribeMessageSubscription(broadcaster_user_id=self.owner_id), "resub messages"),
            (eventsub.ChannelCheerSubscription(broadcaster_user_id=self.owner_id), "cheers"),
            (eventsub.ChannelPointsRedeemAddSubscription(broadcaster_user_id=self.owner_id), "channel points redemptions"),
        )
        for sub_payload, label in subs:
            try:
                await self.subscribe_websocket(payload=sub_payload)
                logger.info("Twitch bot: subscribed to %s", label)
            except Exception as e:
                logger.warning(
                    "Twitch bot: could not subscribe to %s yet (%s) — visit %s to authorize.",
                    label, e, _OAUTH_URL,
                )

    async def event_oauth_authorized(self, payload: UserTokenPayload) -> None:
        await super().event_oauth_authorized(payload)
        await self._subscribe_chat()
        await self._subscribe_events()
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

    async def start_ad_break(self, length: int) -> bool:
        """Best-effort: trigger a Twitch ad break on the broadcaster's channel via the
        Start Commercial API. Requires Affiliate/Partner status, the channel currently
        live, and the broadcaster's token authorized with the channel:edit:commercial
        scope (part of _OAUTH_SCOPES above — visit _OAUTH_URL to authorize).

        Never raises — same fire-and-forget convention as announce()/OBS/ds_ingest
        elsewhere in this codebase. Twitch enforces its own cooldown between ad
        breaks (returned as retry_after); calling this again before that elapses
        just logs Twitch's rejection message rather than erroring.
        """
        try:
            broadcaster = self.create_partialuser(user_id=self._owner_id)
            result = await broadcaster.start_commercial(length=length, token_for=self._owner_id)
            logger.info(
                "Twitch ad break: %s (length=%ds, retry_after=%ds)",
                result.message, result.length, result.retry_after,
            )
            return True
        except Exception as e:
            logger.warning("Twitch ad break failed: %s", e)
            return False

    async def _shoutout(self, text: str) -> None:
        """Post a shoutout both to Twitch chat (announce()) and through the game's
        TTS pipeline (speak_cable — CABLE Input, no G-press/broadcast-window
        needed), so it's audible in-stream even if the megaphone broadcast window
        isn't open. Never raises — same fire-and-forget convention as the rest of
        this integration."""
        await self.announce(text)
        try:
            from game import tts
            tts.speak_cable(text)
        except Exception as e:
            logger.warning("Twitch shoutout TTS failed: %s", e)

    async def event_subscription(self, payload) -> None:
        """channel.subscribe — fires for both a brand-new sub and a gift
        recipient (payload.gift is True in the latter case). Gift recipients are
        skipped here since event_subscription_gift already announces the gift as
        one batch from the gifter's side — without this check both would fire and
        double-announce the same gift."""
        if payload.gift:
            return
        await self._shoutout(f"Thank you {payload.user.display_name} for subscribing!")

    async def event_subscription_gift(self, payload) -> None:
        gifter = payload.user.display_name if payload.user else "an anonymous gifter"
        plural = "sub" if payload.total == 1 else "subs"
        await self._shoutout(f"{gifter} just gifted {payload.total} {plural}! Thank you!")

    async def event_subscription_message(self, payload) -> None:
        """channel.subscription.message — a resub with a chat message attached;
        this is how resubs are announced (channel.subscribe explicitly excludes
        them, per Twitch's own docs)."""
        await self._shoutout(
            f"Thank you {payload.user.display_name} for resubscribing for {payload.cumulative_months} months!"
        )

    async def event_cheer(self, payload) -> None:
        cheerer = payload.user.display_name if payload.user else "an anonymous cheerer"
        await self._shoutout(f"{cheerer} cheered {payload.bits} bits! Thank you!")

    async def _is_mod_or_broadcaster(self, user_id: str) -> bool:
        """True if user_id is the broadcaster or a channel moderator — lets mods and
        the broadcaster bypass the channel-points POV cooldown below, matching how
        they already bypass everything via /pov and !pov (both mod/admin-gated with
        no cooldown at all).

        Requires the moderation:read scope (part of _OAUTH_SCOPES). Best-effort: on
        any lookup failure (scope not yet granted, API error), this treats the user
        as NOT privileged so the cooldown fails safe — still enforced — rather than
        silently letting everyone bypass it if the check itself is broken.
        """
        if str(user_id) == self._owner_id:
            return True
        try:
            broadcaster = self.create_partialuser(user_id=self._owner_id)
            async for _mod in broadcaster.fetch_moderators(user_ids=[user_id]):
                return True
            return False
        except Exception as e:
            logger.warning("Twitch bot: could not check moderator status for %s: %s", user_id, e)
            return False

    async def event_custom_redemption_add(self, payload) -> None:
        """channel.channel_points_custom_reward_redemption.add — only acts on the
        one reward whose title matches twitch_pov_reward_title (config, default
        'Change POV'); any other custom reward on the channel is left completely
        alone. That reward must be created manually in the Twitch Creator
        Dashboard with "require viewer to enter text" enabled, so payload.user_input
        carries the player number — same 1-9/0 choices as /pov and !pov.

        A regular viewer redeeming this is rate-limited to one POV change per
        _POV_REDEMPTION_COOLDOWN_SECONDS globally (not per-viewer) — this is enforced
        here in bot code, not via the reward's own Twitch-side cooldown setting,
        specifically so it can be skipped for mods/the broadcaster (Twitch's built-in
        per-reward cooldown has no concept of "mods bypass this"). /pov and !pov are
        untouched by any of this — they're already restricted to mods/admins with no
        cooldown, exactly as before.

        Redemptions default to UNFULFILLED and sit in the dashboard's queue
        forever unless explicitly updated, so this always calls fulfill() (on
        success) or refund() (cooldown active, invalid input, wrong game state, or
        the keystroke couldn't be sent) — both require channel:manage:redemptions.
        """
        reward_title = self._config.get("twitch_pov_reward_title", "Change POV")
        if payload.reward.title.strip().lower() != reward_title.strip().lower():
            return

        is_privileged = await self._is_mod_or_broadcaster(payload.user.id)

        if not is_privileged and time.monotonic() < self._pov_redemption_available_at:
            logger.info(
                "Twitch bot: POV redemption from %s rejected — cooldown active", payload.user.display_name
            )
            try:
                await payload.refund(token_for=self._owner_id)
            except Exception as e:
                logger.warning("Twitch bot: could not refund cooldown-blocked POV redemption: %s", e)
            return

        key = payload.user_input.strip()
        if not self.session.is_command_valid("pov") or key not in _VALID_POV_KEYS:
            try:
                await payload.refund(token_for=self._owner_id)
            except Exception as e:
                logger.warning("Twitch bot: could not refund invalid POV redemption: %s", e)
            return

        from game.card_actions import press_key

        loop = asyncio.get_running_loop()
        sent = await loop.run_in_executor(None, functools.partial(press_key, key, no_focus_fallback=True))

        # Only arms the cooldown once an actual POV change goes through — a failed
        # send (Darwin window not found) didn't really "use up" the cooldown window.
        if sent and not is_privileged:
            self._pov_redemption_available_at = time.monotonic() + _POV_REDEMPTION_COOLDOWN_SECONDS

        try:
            if sent:
                await payload.fulfill(token_for=self._owner_id)
            else:
                await payload.refund(token_for=self._owner_id)
        except Exception as e:
            logger.warning("Twitch bot: could not update POV redemption status: %s", e)


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

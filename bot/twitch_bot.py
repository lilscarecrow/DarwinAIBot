import asyncio
import functools
import logging
import re
import time
from typing import Optional

from twitchio import eventsub
from twitchio.authentication import UserTokenPayload
from twitchio.ext import commands

logger = logging.getLogger(__name__)

_VALID_POV_KEYS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "0"}

# Channel-points redemption text box has no command parsing — viewers used to
# typing chat commands often enter "!pov 5" or "pov 5" out of habit instead of
# the bare "5" the reward actually asks for. Strips an optional leading "!"
# and "pov" (any case, with/without a following space) before checking the
# remainder against _VALID_POV_KEYS, so "5", "!pov 5", "pov 5", and "POV5" all
# resolve the same way. Only used for the channel-points redemption path
# (event_custom_redemption_add) — !pov in chat is already parsed into
# command + argument by twitchio itself and never sees this.
_POV_PREFIX_RE = re.compile(r"^!?\s*pov\s*", re.IGNORECASE)


def _extract_pov_key(text: Optional[str]) -> Optional[str]:
    """Free-form redemption text -> a valid POV key, or None if it doesn't
    resolve to one after stripping an optional "!pov"/"pov" prefix."""
    if not text:
        return None
    cleaned = _POV_PREFIX_RE.sub("", text.strip(), count=1).strip()
    return cleaned if cleaned in _VALID_POV_KEYS else None

# How long _resubscribe_missing() waits after a reconnect before checking which
# subscriptions are enabled — gives TwitchIO's own internal resubscribe-on-reconnect
# time to finish first (observed taking a few seconds for 6 subscriptions), so we're
# checking real post-migration state rather than racing it.
_RESUBSCRIBE_GRACE_SECONDS = 8

# Global cooldown between channel-points-triggered POV changes from regular viewers
# (see event_custom_redemption_add). Does not apply to /pov or !pov — both are
# already mod/admin-gated with no cooldown, and mods/the broadcaster bypass this
# cooldown too when they redeem the channel-points reward themselves.
_POV_REDEMPTION_COOLDOWN_SECONDS = 30

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
        # True once the first EventSub websocket welcome has been seen — see
        # event_websocket_welcome(): setup_hook() already subscribes on that first
        # one, so re-subscribing there too would just race it and log a harmless but
        # noisy 409 on every single startup. Only welcomes after the first (i.e.
        # genuine reconnects) trigger the resubscribe safety net.
        self._seen_first_welcome: bool = False
        # Set once setup_hook() finishes its initial subscribe pass — main.py's
        # startup summary waits on this to report whether Twitch actually connected.
        # chat_subscribed / events_subscribed hold that pass's outcome for the report.
        self.startup_done = asyncio.Event()
        self.chat_subscribed: bool = False
        self.events_subscribed: tuple[int, int] = (0, 0)  # (succeeded, attempted)
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
        self.chat_subscribed = await self._subscribe_chat()
        self.events_subscribed = await self._subscribe_events()
        self.startup_done.set()
        logger.info("Twitch bot: setup complete, listening for !pov")

    async def event_websocket_welcome(self, payload) -> None:
        """Fires on every new EventSub websocket session — the initial connect AND
        every reconnect. Skips the very first welcome (setup_hook() already
        subscribes at that exact moment); every welcome after that is a genuine
        reconnect, handled by _resubscribe_missing() below.
        """
        if not self._seen_first_welcome:
            self._seen_first_welcome = True
            return
        await self._resubscribe_missing()

    async def _resubscribe_missing(self) -> None:
        """Reconnect safety net for event_websocket_welcome().

        An earlier version of this called _subscribe_chat()/_subscribe_events()
        unconditionally on every reconnect. That was found live to double every
        !pov reply, sub/cheer shoutout, and channel-points action: TwitchIO's own
        client already tries to migrate subscriptions to the new session on every
        reconnect internally, and blindly creating ours again raced that attempt.
        Once the old session's subscriptions are gone (the normal case — Twitch
        auto-revokes websocket-transport subscriptions when their connection
        closes), both the internal attempt and ours look like a fresh,
        non-conflicting creation, so both can succeed independently — leaving two
        live subscriptions for the same event instead of one.

        Fix: wait for TwitchIO's own attempt to finish, then check what's actually
        enabled before creating anything. Only subscription types that are missing
        get (re-)created — this is what actually recovers from the original failure
        mode (the internal migration 400ing and never being retried) without
        risking a duplicate when the internal migration already worked, which is
        the common case.
        """
        await asyncio.sleep(_RESUBSCRIBE_GRACE_SECONDS)

        enabled_types: set[str] | None = set()
        try:
            result = await self.fetch_eventsub_subscriptions(token_for=self.owner_id, user_id=self.owner_id)
            async for sub in result.subscriptions:
                if sub.status == "enabled":
                    enabled_types.add(sub.type)
        except Exception as e:
            logger.warning(
                "Twitch bot: could not check existing EventSub subscriptions after reconnect (%s) — "
                "resubscribing to everything as a fallback (may briefly duplicate events "
                "if the migration actually succeeded).",
                e,
            )
            enabled_types = None  # None means "couldn't check — assume nothing is enabled"

        if enabled_types is None or "channel.chat.message" not in enabled_types:
            await self._subscribe_chat()
        await self._subscribe_events(skip_types=enabled_types)

    async def _subscribe_chat(self) -> bool:
        try:
            payload = eventsub.ChatMessageSubscription(broadcaster_user_id=self.owner_id, user_id=self.bot_id)
            await self.subscribe_websocket(payload=payload)
            logger.info("Twitch bot: subscribed to chat messages")
            return True
        except Exception as e:
            logger.warning(
                "Twitch bot: could not subscribe to chat yet (%s) — visit %s "
                "to authorize; the subscription completes automatically afterward.",
                e, _OAUTH_URL,
            )
            return False

    async def _subscribe_events(self, *, skip_types: set[str] | None = None) -> tuple[int, int]:
        """Subscribe to the non-chat EventSub types this bot reacts to: subs, gift
        subs, resub messages, cheers, and channel-points redemptions. Each one
        subscribes independently (its own try/except) so a scope that hasn't been
        granted yet for one of them (e.g. re-authorization with the full
        _OAUTH_SCOPES list hasn't happened) doesn't block the others from working.
        Retried the same way as chat — best-effort here on startup (harmlessly
        failing pre-authorization) and again from event_oauth_authorized() once a
        token is actually granted, so nothing needs a restart once the scopes land.

        skip_types: subscription type strings (e.g. "channel.cheer") to leave alone
        because _resubscribe_missing() already confirmed they're enabled. None (the
        default, used by setup_hook()/event_oauth_authorized()) subscribes to all of
        them unconditionally, since nothing can already exist at those call sites.

        Returns (succeeded, attempted) counting only the ones actually attempted
        (skipped ones count toward neither) — main.py's startup summary uses this.
        """
        subs = (
            (eventsub.ChannelSubscribeSubscription(broadcaster_user_id=self.owner_id), "new subs"),
            (eventsub.ChannelSubscriptionGiftSubscription(broadcaster_user_id=self.owner_id), "gift subs"),
            (eventsub.ChannelSubscribeMessageSubscription(broadcaster_user_id=self.owner_id), "resub messages"),
            (eventsub.ChannelCheerSubscription(broadcaster_user_id=self.owner_id), "cheers"),
            (eventsub.ChannelPointsRedeemAddSubscription(broadcaster_user_id=self.owner_id), "channel points redemptions"),
        )
        succeeded = 0
        attempted = 0
        for sub_payload, label in subs:
            if skip_types and sub_payload.type in skip_types:
                continue
            attempted += 1
            try:
                await self.subscribe_websocket(payload=sub_payload)
                logger.info("Twitch bot: subscribed to %s", label)
                succeeded += 1
            except Exception as e:
                logger.warning(
                    "Twitch bot: could not subscribe to %s yet (%s) — visit %s to authorize.",
                    label, e, _OAUTH_URL,
                )
        return succeeded, attempted

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
        carries the player number — same 1-9/0 choices as /pov and !pov. Parsed
        via _extract_pov_key() rather than an exact match: viewers used to
        typing chat commands often type "!pov 5" or "pov 5" into the redemption
        box instead of the bare "5" it asks for, so those (and case variants)
        are accepted too. Anything that still doesn't resolve to a valid key
        is refunded, never fulfilled.

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

        key = _extract_pov_key(payload.user_input)
        if not self.session.is_command_valid("pov") or self.session.is_pov_locked() or key is None:
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
        if session.is_pov_locked():
            await ctx.reply("POV isn't available yet — the match is still starting up.")
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

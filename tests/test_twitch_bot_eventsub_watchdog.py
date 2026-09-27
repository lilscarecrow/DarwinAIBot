"""EventSub health watchdog (2026-09-27) — found live: a burst of 3 EventSub
reconnects inside ~30s ended with the connection never coming back at all. No
further "session_welcome", no further error, nothing logged for the EventSub
subsystem for the next 12 hours until a manual restart -- chat, channel-points
redemptions, and sub/cheer shoutouts were all silently dead the whole time.

The existing recovery path (_resubscribe_missing(), triggered by
event_websocket_welcome()) assumes a welcome eventually fires again after a
reconnect -- that's exactly the thing this incident didn't do. _eventsub_watchdog_loop()
doesn't wait on that signal: it re-checks subscription health on a fixed timer
for the life of the bot, independent of any reconnect event ever firing again.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from twitchio.ext import commands

from bot.twitch_bot import (
    DarwinTwitchBot,
    _EVENTSUB_HEALTH_CHECK_INTERVAL_SECONDS,
    _RESUBSCRIBE_GRACE_SECONDS,
)
from session.state import SessionState


def make_bot(**config_overrides):
    cfg = {
        "twitch_owner_id": "123",
        "twitch_client_id": "x",
        "twitch_client_secret": "y",
        "twitch_bot_id": "123",
    }
    cfg.update(config_overrides)
    return DarwinTwitchBot(config=cfg, session=SessionState())


def run(coro):
    return asyncio.run(coro)


def run_until_cancelled(coro):
    """The watchdog loop is `while True` -- tests break out of it by having a
    mocked asyncio.sleep raise CancelledError on some later call, then let that
    propagate normally (the same way a real task cancellation would)."""
    try:
        run(coro)
    except asyncio.CancelledError:
        pass


# ---- _eventsub_watchdog_loop ------------------------------------------------

def test_loop_sleeps_the_configured_interval_then_checks_subscriptions():
    bot = make_bot()
    bot._resubscribe_missing = AsyncMock()
    with patch("bot.twitch_bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
        sleep.side_effect = [None, asyncio.CancelledError()]
        run_until_cancelled(bot._eventsub_watchdog_loop())
    sleep.assert_any_call(_EVENTSUB_HEALTH_CHECK_INTERVAL_SECONDS)
    bot._resubscribe_missing.assert_called_once_with(grace=False)


def test_loop_survives_a_failed_check_and_keeps_going():
    """A health check that itself errors (e.g. a network blip during the
    check) must not kill the loop -- there's always a next cycle."""
    bot = make_bot()
    bot._resubscribe_missing = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("bot.twitch_bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
        sleep.side_effect = [None, None, asyncio.CancelledError()]
        run_until_cancelled(bot._eventsub_watchdog_loop())
    assert bot._resubscribe_missing.call_count == 2


# ---- _resubscribe_missing(grace=...) ----------------------------------------

def test_grace_true_sleeps_before_checking_default_behavior_unchanged():
    bot = make_bot()
    bot.fetch_eventsub_subscriptions = AsyncMock(side_effect=RuntimeError("network error"))
    bot._subscribe_chat = AsyncMock()
    bot._subscribe_events = AsyncMock()
    with patch("bot.twitch_bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
        run(bot._resubscribe_missing())
    sleep.assert_called_once_with(_RESUBSCRIBE_GRACE_SECONDS)


def test_grace_false_skips_the_sleep_for_the_periodic_watchdog():
    bot = make_bot()
    bot.fetch_eventsub_subscriptions = AsyncMock(side_effect=RuntimeError("network error"))
    bot._subscribe_chat = AsyncMock()
    bot._subscribe_events = AsyncMock()
    with patch("bot.twitch_bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
        run(bot._resubscribe_missing(grace=False))
    sleep.assert_not_called()


# ---- setup_hook() starts the watchdog; close() cancels it -------------------

def test_setup_hook_starts_the_watchdog_task():
    bot = make_bot()
    bot.add_component = AsyncMock()
    bot._subscribe_chat = AsyncMock(return_value=True)
    bot._subscribe_events = AsyncMock(return_value=(5, 5))
    bot._ensure_pov_reward = AsyncMock()
    bot._ensure_favorite_reward = AsyncMock()
    with patch("bot.twitch_bot.asyncio.create_task") as create_task:
        run(bot.setup_hook())
    create_task.assert_called_once()
    # the coroutine passed to create_task is the watchdog loop's own coroutine
    assert create_task.call_args[0][0].__name__ == "_eventsub_watchdog_loop"
    create_task.call_args[0][0].close()  # avoid a "coroutine was never awaited" warning


def test_close_cancels_the_watchdog_task_before_the_usual_teardown():
    bot = make_bot()
    fake_task = MagicMock()
    bot._eventsub_watchdog_task = fake_task
    with patch.object(commands.Bot, "close", new_callable=AsyncMock) as super_close:
        run(bot.close())
    fake_task.cancel.assert_called_once()
    super_close.assert_called_once()


def test_close_is_safe_when_no_watchdog_task_was_ever_started():
    bot = make_bot()
    assert bot._eventsub_watchdog_task is None
    with patch.object(commands.Bot, "close", new_callable=AsyncMock) as super_close:
        run(bot.close())  # must not raise
    super_close.assert_called_once()

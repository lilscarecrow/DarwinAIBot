"""Channel-points "Change POV" reward — _handle_pov_redemption() in
bot/twitch_bot.py. No dedicated coverage existed before 2026-09-20 (only
mocked out in test_twitch_bot_favorite_reward.py's dispatch tests).

Added alongside the Crowd Favorite fix for the same live issue: a refunded
redemption gave the viewer no idea why (see _refund()'s docstring). Each
distinct refusal here now carries its own specific, viewer-facing reason
instead of the old combined "invalid input or wrong game state" catch-all.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from bot.twitch_bot import DarwinTwitchBot
from session.state import SessionState, BotState


def make_bot(**config_overrides):
    cfg = {
        "twitch_owner_id": "123",
        "twitch_client_id": "x",
        "twitch_client_secret": "y",
        "twitch_bot_id": "123",
    }
    cfg.update(config_overrides)
    return DarwinTwitchBot(config=cfg, session=SessionState())


def make_payload(user_input, user_id="999", display_name="viewer"):
    payload = MagicMock()
    payload.reward.title = "Change POV"
    payload.user_input = user_input
    payload.user.id = user_id
    payload.user.display_name = display_name
    payload.refund = AsyncMock()
    payload.fulfill = AsyncMock()
    return payload


def run(coro):
    return asyncio.run(coro)


def mock_announce(bot):
    return patch.object(bot, "announce", new_callable=AsyncMock)


def ready_bot(**config_overrides):
    """A bot whose session is in a POV-valid, unlocked state, and whose
    cooldown has already elapsed — the baseline every refusal test starts
    from before flipping the one thing it's testing."""
    bot = make_bot(**config_overrides)
    bot.session.transition(BotState.MATCH_IN_PROGRESS)
    bot._pov_redemption_available_at = 0.0
    return bot


def test_refunds_with_cooldown_reason_for_a_non_privileged_viewer():
    bot = ready_bot()
    bot._pov_redemption_available_at = time.monotonic() + 30
    bot._is_mod_or_broadcaster = AsyncMock(return_value=False)
    payload = make_payload("5")
    with mock_announce(bot) as announce:
        run(bot._handle_pov_redemption(payload))
    payload.refund.assert_called_once()
    payload.fulfill.assert_not_called()
    assert "cooldown" in announce.call_args[0][0]


def test_mods_bypass_the_cooldown():
    bot = ready_bot()
    bot._pov_redemption_available_at = time.monotonic() + 30
    bot._is_mod_or_broadcaster = AsyncMock(return_value=True)
    with patch("game.card_actions.press_key", return_value=True):
        payload = make_payload("5")
        run(bot._handle_pov_redemption(payload))
    payload.fulfill.assert_called_once()
    payload.refund.assert_not_called()


def test_refunds_with_invalid_input_reason():
    bot = ready_bot()
    bot._is_mod_or_broadcaster = AsyncMock(return_value=True)
    payload = make_payload("not a player number")
    with mock_announce(bot) as announce:
        run(bot._handle_pov_redemption(payload))
    payload.refund.assert_called_once()
    assert "valid player number" in announce.call_args[0][0]


def test_refunds_with_wrong_state_reason():
    bot = make_bot()  # left at the default IDLE state — pov isn't valid there
    bot._pov_redemption_available_at = 0.0
    bot._is_mod_or_broadcaster = AsyncMock(return_value=True)
    payload = make_payload("5")
    with mock_announce(bot) as announce:
        run(bot._handle_pov_redemption(payload))
    payload.refund.assert_called_once()
    message = announce.call_args[0][0]
    assert "state: IDLE" in message


def test_refunds_with_pov_locked_reason():
    bot = ready_bot()
    bot.session.lock_pov()
    bot._is_mod_or_broadcaster = AsyncMock(return_value=True)
    payload = make_payload("5")
    with mock_announce(bot) as announce:
        run(bot._handle_pov_redemption(payload))
    payload.refund.assert_called_once()
    assert "locked" in announce.call_args[0][0]


def test_refunds_with_keystroke_failure_reason():
    bot = ready_bot()
    bot._is_mod_or_broadcaster = AsyncMock(return_value=True)
    with patch("game.card_actions.press_key", return_value=False):
        payload = make_payload("5")
        with mock_announce(bot) as announce:
            run(bot._handle_pov_redemption(payload))
    payload.refund.assert_called_once()
    payload.fulfill.assert_not_called()
    assert "couldn't reach the game" in announce.call_args[0][0]


def test_fulfills_on_a_successful_switch():
    bot = ready_bot()
    bot._is_mod_or_broadcaster = AsyncMock(return_value=False)
    with patch("game.card_actions.press_key", return_value=True):
        payload = make_payload("5")
        run(bot._handle_pov_redemption(payload))
    payload.fulfill.assert_called_once()
    payload.refund.assert_not_called()

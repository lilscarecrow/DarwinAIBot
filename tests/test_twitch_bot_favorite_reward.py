"""Crowd Favorite channel-points reward (2026-09-10): reward creation via
API (mirroring the POV reward fix) and the redemption handler that queues
game.match_runner.MatchRunner.try_queue_favorite_reward().

No pytest-asyncio in this project's dev deps — async methods are driven
directly with asyncio.run() from plain sync test functions instead.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.twitch_bot import DarwinTwitchBot
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


def make_payload(title, user_input, user_id="999", display_name="viewer"):
    payload = MagicMock()
    payload.reward.title = title
    payload.user_input = user_input
    payload.user.id = user_id
    payload.user.display_name = display_name
    payload.refund = AsyncMock()
    payload.fulfill = AsyncMock()
    return payload


def run(coro):
    return asyncio.run(coro)


# ---- event_custom_redemption_add: dispatch by title ------------------------

def test_dispatches_pov_titled_redemption_to_the_pov_handler():
    bot = make_bot()
    payload = make_payload("Change POV", "5")
    with patch_handler(bot, "_handle_pov_redemption") as pov, \
         patch_handler(bot, "_handle_favorite_redemption") as favorite:
        run(bot.event_custom_redemption_add(payload))
    pov.assert_called_once_with(payload)
    favorite.assert_not_called()


def test_dispatches_favorite_titled_redemption_to_the_favorite_handler():
    bot = make_bot()
    payload = make_payload("Crowd Favorite", "5")
    with patch_handler(bot, "_handle_pov_redemption") as pov, \
         patch_handler(bot, "_handle_favorite_redemption") as favorite:
        run(bot.event_custom_redemption_add(payload))
    favorite.assert_called_once_with(payload)
    pov.assert_not_called()


def test_ignores_any_other_reward_title():
    bot = make_bot()
    payload = make_payload("Some Other Reward", "hello")
    with patch_handler(bot, "_handle_pov_redemption") as pov, \
         patch_handler(bot, "_handle_favorite_redemption") as favorite:
        run(bot.event_custom_redemption_add(payload))
    pov.assert_not_called()
    favorite.assert_not_called()


def test_dispatch_is_case_and_whitespace_insensitive():
    bot = make_bot()
    payload = make_payload("  crowd favorite  ", "5")
    with patch_handler(bot, "_handle_favorite_redemption") as favorite:
        run(bot.event_custom_redemption_add(payload))
    favorite.assert_called_once()


def patch_handler(bot, name):
    from unittest.mock import patch
    return patch.object(bot, name, new_callable=AsyncMock)


# ---- _handle_favorite_redemption --------------------------------------------

def test_refunds_invalid_input():
    bot = make_bot()
    payload = make_payload("Crowd Favorite", "not a number")
    run(bot._handle_favorite_redemption(payload))
    payload.refund.assert_called_once()
    payload.fulfill.assert_not_called()


def test_refunds_when_no_match_is_in_progress():
    bot = make_bot()
    bot.active_runner = None
    payload = make_payload("Crowd Favorite", "5")
    run(bot._handle_favorite_redemption(payload))
    payload.refund.assert_called_once()
    payload.fulfill.assert_not_called()


def test_queues_and_fulfills_on_acceptance():
    bot = make_bot()
    bot.active_runner = MagicMock()
    bot.active_runner.try_queue_favorite_reward.return_value = True
    payload = make_payload("Crowd Favorite", "5")
    run(bot._handle_favorite_redemption(payload))
    # key "5" -> index_for_slot_number("5") == 4
    bot.active_runner.try_queue_favorite_reward.assert_called_once_with(4)
    payload.fulfill.assert_called_once()
    payload.refund.assert_not_called()


def test_refunds_when_the_runner_rejects_the_queue_attempt():
    bot = make_bot()
    bot.active_runner = MagicMock()
    bot.active_runner.try_queue_favorite_reward.return_value = False
    payload = make_payload("Crowd Favorite", "5")
    run(bot._handle_favorite_redemption(payload))
    payload.refund.assert_called_once()
    payload.fulfill.assert_not_called()


def test_accepts_pov_style_prefixed_input_too():
    """Same _extract_pov_key parser as the POV reward — a viewer typing
    "!pov 3" out of habit still resolves."""
    bot = make_bot()
    bot.active_runner = MagicMock()
    bot.active_runner.try_queue_favorite_reward.return_value = True
    payload = make_payload("Crowd Favorite", "!pov 3")
    run(bot._handle_favorite_redemption(payload))
    bot.active_runner.try_queue_favorite_reward.assert_called_once_with(2)  # "3" -> index 2


def test_slot_zero_maps_to_index_nine():
    bot = make_bot()
    bot.active_runner = MagicMock()
    bot.active_runner.try_queue_favorite_reward.return_value = True
    payload = make_payload("Crowd Favorite", "0")
    run(bot._handle_favorite_redemption(payload))
    bot.active_runner.try_queue_favorite_reward.assert_called_once_with(9)


# ---- _ensure_custom_reward / _ensure_favorite_reward ------------------------

def test_ensure_favorite_reward_skips_creation_when_advanced_cards_is_off():
    bot = make_bot(advanced_cards=False)
    broadcaster = MagicMock()
    broadcaster.fetch_custom_rewards = AsyncMock(return_value=[])
    broadcaster.create_custom_reward = AsyncMock()
    bot.create_partialuser = MagicMock(return_value=broadcaster)

    run(bot._ensure_favorite_reward())

    broadcaster.create_custom_reward.assert_not_called()
    broadcaster.fetch_custom_rewards.assert_not_called()  # short-circuits before even checking


def test_ensure_favorite_reward_creates_it_when_missing():
    bot = make_bot(twitch_favorite_reward_title="Crowd Favorite", twitch_favorite_reward_cost=500)
    broadcaster = MagicMock()
    broadcaster.fetch_custom_rewards = AsyncMock(return_value=[])
    broadcaster.create_custom_reward = AsyncMock()
    bot.create_partialuser = MagicMock(return_value=broadcaster)

    run(bot._ensure_favorite_reward())

    broadcaster.create_custom_reward.assert_called_once()
    _, kwargs = broadcaster.create_custom_reward.call_args
    assert kwargs["title"] == "Crowd Favorite"
    assert kwargs["cost"] == 500


def test_ensure_favorite_reward_is_idempotent_when_already_manageable():
    bot = make_bot()
    existing = MagicMock()
    existing.title = "Crowd Favorite"
    broadcaster = MagicMock()
    broadcaster.fetch_custom_rewards = AsyncMock(return_value=[existing])
    broadcaster.create_custom_reward = AsyncMock()
    bot.create_partialuser = MagicMock(return_value=broadcaster)

    run(bot._ensure_favorite_reward())

    broadcaster.create_custom_reward.assert_not_called()


def test_ensure_favorite_reward_never_raises_on_failure():
    bot = make_bot()
    broadcaster = MagicMock()
    broadcaster.fetch_custom_rewards = AsyncMock(side_effect=RuntimeError("boom"))
    bot.create_partialuser = MagicMock(return_value=broadcaster)

    run(bot._ensure_favorite_reward())  # must not raise

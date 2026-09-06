import asyncio
import json
import logging
import sys
from pathlib import Path

from session.state import SessionState
from zones.strategy_factory import valid_strategy_names

CONFIG_PATH = Path("config.json")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/darwin_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# obsws_python logs "Connecting with parameters: host='...' port=... password='...'"
# at INFO level on every connect — that would print the websocket password in
# plain text to the console and darwin_bot.log. Raise it to WARNING to suppress
# that (and its other INFO/DEBUG noise) while still surfacing real errors.
logging.getLogger("obsws_python").setLevel(logging.WARNING)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error("config.json not found at %s", CONFIG_PATH.resolve())
        sys.exit(1)
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def validate_config(config: dict) -> list[str]:
    errors = []

    exe = config.get("game_executable_path", "")
    if not exe:
        errors.append("game_executable_path is missing or empty")
    elif not Path(exe).exists():
        errors.append(f"game_executable_path does not exist on disk: {exe}")

    if not config.get("discord_bot_token"):
        errors.append("discord_bot_token is missing or empty")

    if not config.get("discord_required_role"):
        errors.append("discord_required_role is missing or empty")

    strategy = config.get("zone_selection_strategy", "")
    if strategy not in valid_strategy_names():
        errors.append(
            f"zone_selection_strategy '{strategy}' is unknown. "
            f"Valid options: {valid_strategy_names()}"
        )

    if config.get("twitch_enabled", False):
        for key in ("twitch_client_id", "twitch_client_secret", "twitch_bot_id", "twitch_owner_id"):
            if not config.get(key):
                errors.append(f"{key} is missing or empty (required because twitch_enabled is true)")

    return errors


_STARTUP_SUMMARY_TIMEOUT_SECONDS = 30.0


async def _log_startup_summary(discord_bot, twitch_bot, config: dict) -> None:
    """Waits for each service to report ready (or times out) and logs one clear,
    easy-to-grep block so it's obvious whether the bot came up fully connected or
    something specific failed — the individual connect/subscribe attempts already
    log their own detail above this; this is just the at-a-glance summary.
    """
    from game import obs_control

    try:
        await asyncio.wait_for(discord_bot.startup_done.wait(), timeout=_STARTUP_SUMMARY_TIMEOUT_SECONDS)
        discord_line = f"Discord: OK (logged in as {discord_bot.user})"
    except asyncio.TimeoutError:
        discord_line = "Discord: TIMED OUT — did not finish connecting (see errors above)"

    twitch_enabled = config.get("twitch_enabled", False)
    if not twitch_enabled:
        twitch_lines = ["Twitch: disabled (twitch_enabled is false)"]
    elif twitch_bot is None:
        twitch_lines = ["Twitch: FAILED — bot crashed before setup completed (see errors above)"]
    else:
        try:
            await asyncio.wait_for(twitch_bot.startup_done.wait(), timeout=_STARTUP_SUMMARY_TIMEOUT_SECONDS)
            chat_status = "OK" if twitch_bot.chat_subscribed else "FAILED (see warning above)"
            ok_n, total_n = twitch_bot.events_subscribed
            events_status = f"{ok_n}/{total_n} OK" if total_n else "OK"
            if ok_n < total_n:
                events_status += " — see warnings above"
            twitch_lines = [
                f"Twitch chat (!pov): {chat_status}",
                f"Twitch events (subs/cheers/channel points): {events_status}",
            ]
        except asyncio.TimeoutError:
            twitch_lines = ["Twitch: TIMED OUT — setup did not finish (see errors above)"]

    obs_check = obs_control.last_check_ok()
    if obs_check is None:
        obs_line = "OBS: disabled (obs_stream_enabled is false)"
    else:
        obs_line = "OBS: OK (see connection check above)" if obs_check else "OBS: FAILED (see warning above)"

    lines = ["=== Startup Summary ===", discord_line, *twitch_lines, obs_line, "========================"]
    for line in lines:
        logger.info(line)


async def _run(config: dict, session: SessionState):
    from bot.discord_bot import DarwinBot
    discord_bot = DarwinBot(config=config, session=session)

    try:
        if config.get("twitch_enabled", False):
            from bot.twitch_bot import DarwinTwitchBot
            logger.info("Connecting to Twitch and Discord...")

            async def _run_twitch(bot) -> None:
                # Never let a Twitch-side failure take down the Discord bot — they're
                # started together below, and asyncio.gather() would otherwise propagate
                # the first exception and cancel the other task. Twitch chat is a nice-to-
                # have on top of the core Discord automation, not a dependency of it.
                try:
                    await bot.start()
                except Exception:
                    logger.exception("Twitch bot crashed — Discord bot continues without it")

            async with DarwinTwitchBot(config=config, session=session) as twitch_bot:
                discord_bot.twitch_bot = twitch_bot
                await asyncio.gather(
                    discord_bot.start(config["discord_bot_token"]),
                    _run_twitch(twitch_bot),
                    _log_startup_summary(discord_bot, twitch_bot, config),
                )
        else:
            logger.info("Connecting to Discord...")
            await asyncio.gather(
                discord_bot.start(config["discord_bot_token"]),
                _log_startup_summary(discord_bot, None, config),
            )
    finally:
        # aiohttp's SSL connections need a moment to finish their own async teardown
        # after close() returns — without this, asyncio.run() closes the event loop
        # first, and a leftover ClientResponse object finalized by the garbage
        # collector afterward tries to use the now-closed loop, printing a
        # harmless-but-noisy "Exception ignored in: ClientResponse.__del__ ...
        # RuntimeError: Event loop is closed" to the console. This is aiohttp's own
        # documented mitigation for that; it's just a timing pause in a plain
        # finally (not an except), so it doesn't suppress or interfere with
        # whatever exception is propagating — no coupling to the cancellation/
        # shutdown logic that was reverted above.
        await asyncio.sleep(0.25)


def main():
    logger.info("Darwin Bot starting up")

    import console_window
    console_window.restore_position()
    console_window.start_position_tracker()

    config = load_config()

    errors = validate_config(config)
    if errors:
        logger.error("Config validation failed with %d error(s):", len(errors))
        for err in errors:
            logger.error("  - %s", err)
        sys.exit(1)

    logger.info("Config validated successfully")

    from game import tts
    tts.configure(
        device_name=config.get("tts_device"),
        voice=config.get("tts_voice", "en-US-AriaNeural"),
        bypass=config.get("ahk_bypass_mode", False),
    )

    from game import obs_control
    obs_control.configure(
        enabled=config.get("obs_stream_enabled", False),
        host=config.get("obs_websocket_host", "localhost"),
        port=config.get("obs_websocket_port", 4455),
        password=config.get("obs_websocket_password", ""),
    )
    obs_control.check_connection()

    # /say's profanity filter (bot/discord_bot.py) — tunable without a code change.
    # tts_profanity_whitelist loosens it (exempt specific default-flagged words, e.g.
    # a mild word this stream is fine with); tts_profanity_extra_words tightens it
    # (block additional words/slang not in the library's default list). Both optional
    # and empty by default — omitting them leaves the library's default word list as-is.
    from better_profanity import profanity
    profanity.load_censor_words(whitelist_words=config.get("tts_profanity_whitelist") or [])
    extra_words = config.get("tts_profanity_extra_words") or []
    if extra_words:
        profanity.add_censor_words(extra_words)

    session = SessionState()

    try:
        asyncio.run(_run(config, session))
    except KeyboardInterrupt:
        logger.info("Darwin Bot shut down")


if __name__ == "__main__":
    main()

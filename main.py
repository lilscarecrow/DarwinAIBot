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

    card_slots = config.get("card_slots", {})
    if len(card_slots) != 10:
        errors.append(f"card_slots must have exactly 10 entries, found {len(card_slots)}")

    return errors


def main():
    logger.info("Darwin Bot starting up")
    config = load_config()

    errors = validate_config(config)
    if errors:
        logger.error("Config validation failed with %d error(s):", len(errors))
        for err in errors:
            logger.error("  - %s", err)
        sys.exit(1)

    logger.info("Config validated successfully")

    session = SessionState()

    from bot.discord_bot import DarwinBot
    bot = DarwinBot(config=config, session=session)

    token = config["discord_bot_token"]
    logger.info("Connecting to Discord...")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()

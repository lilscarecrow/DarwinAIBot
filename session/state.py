from enum import Enum, auto
import time
import logging

logger = logging.getLogger(__name__)


class BotState(Enum):
    IDLE = auto()
    LAUNCHING = auto()
    IN_MENU = auto()
    IN_CUSTOM = auto()
    MATCH_IN_PROGRESS = auto()
    MATCH_ENDED = auto()


# Which commands are valid in each state
VALID_COMMANDS: dict[BotState, list[str]] = {
    BotState.IDLE:              ["launch", "status", "end"],
    BotState.LAUNCHING:         ["status", "end"],
    BotState.IN_MENU:           ["deck", "custom", "status", "end"],
    BotState.IN_CUSTOM:         ["start", "menu", "status", "end"],
    BotState.MATCH_IN_PROGRESS: ["status", "end"],
    BotState.MATCH_ENDED:       ["status", "end"],
}


class SessionState:
    def __init__(self):
        self._state = BotState.IDLE
        self._last_action: str = "None"
        self._next_action: str = "None"
        self._match_start_time: float | None = None

    @property
    def state(self) -> BotState:
        return self._state

    @property
    def last_action(self) -> str:
        return self._last_action

    @property
    def next_action(self) -> str:
        return self._next_action

    def transition(self, new_state: BotState, last_action: str = "", next_action: str = ""):
        logger.info("State transition: %s -> %s", self._state.name, new_state.name)
        self._state = new_state
        if last_action:
            self._last_action = last_action
        if next_action:
            self._next_action = next_action

    def start_match_timer(self):
        self._match_start_time = time.monotonic()

    def match_elapsed_seconds(self) -> float:
        if self._match_start_time is None:
            return 0.0
        return time.monotonic() - self._match_start_time

    def match_elapsed_display(self) -> str:
        elapsed = int(self.match_elapsed_seconds())
        return f"{elapsed // 60}:{elapsed % 60:02d}"

    def is_command_valid(self, command: str) -> bool:
        return command in VALID_COMMANDS.get(self._state, [])

    def invalid_command_message(self, command: str) -> str:
        valid_in = [s.name for s, cmds in VALID_COMMANDS.items() if command in cmds]
        return (
            f"Cannot use `/{command}` in state **{self._state.name}**. "
            f"Valid in: {', '.join(valid_in) or 'none'}."
        )

    def reset(self):
        self._state = BotState.IDLE
        self._last_action = "None"
        self._next_action = "None"
        self._match_start_time = None
        logger.info("Session state reset to IDLE")

    def status_message(self) -> str:
        lines = [
            "**Darwin Bot Status**",
            f"State: {self._state.name.replace('_', ' ').title()}",
        ]
        if self._state == BotState.MATCH_IN_PROGRESS:
            lines.append(f"Timer: {self.match_elapsed_display()}")
        lines.append(f"Last Action: {self._last_action}")
        lines.append(f"Next Action: {self._next_action}")
        return "\n".join(lines)

import random
from zones.base_strategy import BaseZoneStrategy


class RandomZoneStrategy(BaseZoneStrategy):
    """Randomly selects from valid closeable zones."""

    def select_zone(self, valid_zones: list[int], zone_states: dict[int, str]) -> int | None:
        if not valid_zones:
            return None
        return random.choice(valid_zones)

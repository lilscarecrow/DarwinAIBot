from zones.base_strategy import BaseZoneStrategy
from zones.zone_logic import neighbor_count


class OuterFirstStrategy(BaseZoneStrategy):
    """Always closes the zone with the fewest neighbors first (pushes players inward)."""

    def select_zone(self, valid_zones: list[int], zone_states: dict[int, str]) -> int | None:
        if not valid_zones:
            return None
        return min(valid_zones, key=neighbor_count)

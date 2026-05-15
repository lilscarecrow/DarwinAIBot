import random
from zones.base_strategy import BaseZoneStrategy
from zones.zone_logic import neighbor_count


class WeightedOuterStrategy(BaseZoneStrategy):
    """
    Prefers outer zones (fewer neighbors) but occasionally picks others.
    Zones are weighted inversely by neighbor count so outer zones are chosen more often.
    """

    def select_zone(self, valid_zones: list[int], zone_states: dict[int, str]) -> int | None:
        if not valid_zones:
            return None
        # Weight = 1 / neighbor_count so zones with fewer neighbors are preferred
        max_neighbors = max(neighbor_count(z) for z in valid_zones)
        weights = [(max_neighbors + 1) - neighbor_count(z) for z in valid_zones]
        return random.choices(valid_zones, weights=weights, k=1)[0]

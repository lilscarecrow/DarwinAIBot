# 7-zone hex grid adjacency map (1-indexed)
ADJACENCY: dict[int, list[int]] = {
    1: [2, 3, 4],
    2: [1, 4, 5],
    3: [1, 4, 6],
    4: [1, 2, 3, 5, 6, 7],
    5: [2, 4, 7],
    6: [3, 4, 7],
    7: [4, 5, 6],
}

ALL_ZONES = set(ADJACENCY.keys())


class ZoneState:
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"



def valid_closeable_zones(zone_states: dict[int, str]) -> list[int]:
    """Return all zones that are currently OPEN."""
    return [z for z in ALL_ZONES if zone_states.get(z) == ZoneState.OPEN]


def neighbor_count(zone_id: int) -> int:
    return len(ADJACENCY[zone_id])

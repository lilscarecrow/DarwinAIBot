from collections import deque

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


def _bfs_reachable(start: int, open_zones: set[int]) -> set[int]:
    visited = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in ADJACENCY[node]:
            if neighbor in open_zones and neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return visited


def open_zones_stay_connected(open_zones: set[int]) -> bool:
    """Return True if all open zones form a single connected group."""
    if len(open_zones) <= 1:
        return True
    start = next(iter(open_zones))
    return _bfs_reachable(start, open_zones) == open_zones


def can_close_zone(zone_id: int, zone_states: dict[int, str]) -> bool:
    """
    Return True if zone_id can be closed given current zone states.
    Rules:
    - Zone must currently be OPEN
    - Cannot close the last remaining open zone
    - Remaining open zones must stay connected after the close
    """
    if zone_states.get(zone_id) != ZoneState.OPEN:
        return False

    open_zones = {z for z, s in zone_states.items() if s == ZoneState.OPEN}

    if len(open_zones) <= 1:
        return False

    remaining = open_zones - {zone_id}
    return open_zones_stay_connected(remaining)


def valid_closeable_zones(zone_states: dict[int, str]) -> list[int]:
    """Return all zones that can currently be closed."""
    return [z for z in ALL_ZONES if can_close_zone(z, zone_states)]


def neighbor_count(zone_id: int) -> int:
    return len(ADJACENCY[zone_id])

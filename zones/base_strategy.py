from abc import ABC, abstractmethod


class BaseZoneStrategy(ABC):
    """Abstract base class for zone selection strategies."""

    @abstractmethod
    def select_zone(self, valid_zones: list[int], zone_states: dict[int, str]) -> int | None:
        """
        Select which zone to close next.

        Args:
            valid_zones: List of zone IDs that can currently be closed.
            zone_states: Current state of all zones (open/closing/closed).

        Returns:
            Zone ID to close, or None if no valid zones available.
        """

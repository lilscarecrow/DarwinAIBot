from zones.base_strategy import BaseZoneStrategy
from zones.strategies.outer_first import OuterFirstStrategy
from zones.strategies.random_zone import RandomZoneStrategy
from zones.strategies.weighted_outer import WeightedOuterStrategy

STRATEGIES: dict[str, type[BaseZoneStrategy]] = {
    "outer_first":    OuterFirstStrategy,
    "random":         RandomZoneStrategy,
    "weighted_outer": WeightedOuterStrategy,
}


def get_strategy(name: str) -> BaseZoneStrategy:
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(f"Unknown zone strategy '{name}'. Valid options: {list(STRATEGIES)}")
    return cls()


def valid_strategy_names() -> list[str]:
    return list(STRATEGIES.keys())

import random

PROFILES: dict[str, dict] = {
    "standard": {
        "display_name": "Standard",
        "card_plays": [
            {"card": "electromania", "play_time_seconds": 150},
            {"card": "beach_party",  "play_time_seconds": 240},
            {"card": "zone_close",   "play_time_seconds": 300},
            {"card": "electromania", "play_time_seconds": 390},
            {"card": "zone_close",   "play_time_seconds": 480},
            {"card": "telepathy",    "play_time_seconds": 600},
            {"card": "zone_close",   "play_time_seconds": 720},
        ],
    },
    "custom_a": {
        "display_name": "Blood",
        "card_plays": [
            {"card": "electromania", "play_time_seconds": 150},
            {"card": "telepathy",    "play_time_seconds": 270},
            {"card": "electromania", "play_time_seconds": 390},
            {"card": "zone_close",   "play_time_seconds": 480},
            {"card": "blood_moon",   "play_time_seconds": 600},
            {"card": "zone_close",   "play_time_seconds": 720},
        ],
    },
    "custom_b": {
        "display_name": "Everything",
        "card_plays": [
            {"card": "electromania", "play_time_seconds": 150},
            {"card": "beach_party",  "play_time_seconds": 240},
            {"card": "zone_close",   "play_time_seconds": 300},
            {"card": "electromania", "play_time_seconds": 390},
            {"card": "zone_close",   "play_time_seconds": 480},
            {"card": "blood_moon",   "play_time_seconds": 600},
            {"card": "zone_close",   "play_time_seconds": 720},
        ],
    },
    "randomizer": {
        "display_name": "Randomizer",
        "randomizer": True,
    },
}


def get_profile(name: str) -> dict:
    """Return the raw profile entry (may be a randomizer meta-profile)."""
    return PROFILES.get(name) or PROFILES["standard"]


def resolve_profile(name: str) -> dict:
    """Return a concrete profile with card_plays, resolving randomizer at call time."""
    profile = get_profile(name)
    if profile.get("randomizer"):
        pool = [p for k, p in PROFILES.items() if not p.get("randomizer")]
        return random.choice(pool) if pool else PROFILES["standard"]
    return profile


def profile_summary(profile: dict) -> str:
    """Card play schedule as a single readable line, or pool description for randomizer."""
    if profile.get("randomizer"):
        names = [p["display_name"] for p in PROFILES.values() if not p.get("randomizer")]
        return "Picks randomly from: " + ", ".join(names)
    plays = sorted(profile.get("card_plays", []), key=lambda p: p["play_time_seconds"])
    parts = []
    for p in plays:
        m, s = divmod(p["play_time_seconds"], 60)
        label = p["card"].replace("_", " ").title()
        parts.append(f"{label} {m}:{s:02d}")
    return " · ".join(parts) if parts else "No cards scheduled"

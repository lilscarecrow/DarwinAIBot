PROFILES: dict[str, dict] = {
    "standard": {
        "display_name": "Standard",
        "card_plays": [
            {"card": "electromania", "play_time_seconds": 150},
            {"card": "beach_party",  "play_time_seconds": 240},
            {"card": "zone_close",   "play_time_seconds": 300},
            {"card": "electromania", "play_time_seconds": 390},
            {"card": "zone_close",   "play_time_seconds": 480},
            {"card": "blood_moon",   "play_time_seconds": 540},
        ],
    },
}


def get_profile(name: str) -> dict:
    """Return the named profile, falling back to 'standard' if not found."""
    return PROFILES.get(name) or PROFILES["standard"]


def profile_summary(profile: dict) -> str:
    """Card play schedule as a single readable line."""
    plays = sorted(profile.get("card_plays", []), key=lambda p: p["play_time_seconds"])
    parts = []
    for p in plays:
        m, s = divmod(p["play_time_seconds"], 60)
        label = p["card"].replace("_", " ").title()
        parts.append(f"{label} {m}:{s:02d}")
    return " · ".join(parts)

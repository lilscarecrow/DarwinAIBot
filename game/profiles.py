import copy
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
    "beach_shuffle": {
        "display_name": "Beach Shuffle",
        # Same as Standard, but Beach Party's time is picked at resolve time (see
        # random_play_times below) and the two zone closes that used to bracket it
        # are fixed at later times to leave room: 5:00 -> 7:00, 8:00 -> 9:00.
        "card_plays": [
            {"card": "electromania", "play_time_seconds": 150},
            {"card": "beach_party",  "play_time_seconds": 240},
            {"card": "zone_close",   "play_time_seconds": 420},
            {"card": "electromania", "play_time_seconds": 390},
            {"card": "zone_close",   "play_time_seconds": 540},
            {"card": "blood_moon",   "play_time_seconds": 600},
            {"card": "zone_close",   "play_time_seconds": 720},
        ],
        # card -> (min_seconds, max_seconds, step_seconds). Resolved once per match
        # in resolve_profile(); the chosen value overwrites that card's play_time_seconds.
        "random_play_times": {
            "beach_party": (240, 360, 15),
        },
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
    """Return a concrete profile with card_plays, resolving randomizer and any
    per-card random play times at call time. Safe to call once per match — the
    result is a fresh copy when randomization applies, so PROFILES is never mutated."""
    profile = get_profile(name)
    if profile.get("randomizer"):
        pool = [p for k, p in PROFILES.items() if not p.get("randomizer")]
        profile = random.choice(pool) if pool else PROFILES["standard"]

    random_times = profile.get("random_play_times")
    if random_times:
        profile = copy.deepcopy(profile)
        for card_name, (lo, hi, step) in random_times.items():
            chosen = random.randrange(lo, hi + 1, step)
            for play in profile["card_plays"]:
                if play["card"] == card_name:
                    play["play_time_seconds"] = chosen
                    break
    return profile


def profile_summary(profile: dict) -> str:
    """Card play schedule as a single readable line, or pool description for randomizer."""
    if profile.get("randomizer"):
        names = [p["display_name"] for p in PROFILES.values() if not p.get("randomizer")]
        return "Picks randomly from: " + ", ".join(names)
    random_times = profile.get("random_play_times", {})
    plays = sorted(profile.get("card_plays", []), key=lambda p: p["play_time_seconds"])
    parts = []
    for p in plays:
        label = p["card"].replace("_", " ").title()
        random_range = random_times.get(p["card"])
        if random_range:
            lo, hi, _step = random_range
            lo_m, lo_s = divmod(lo, 60)
            hi_m, hi_s = divmod(hi, 60)
            parts.append(f"{label} {lo_m}:{lo_s:02d}-{hi_m}:{hi_s:02d} (random)")
        else:
            m, s = divmod(p["play_time_seconds"], 60)
            parts.append(f"{label} {m}:{s:02d}")
    return " · ".join(parts) if parts else "No cards scheduled"

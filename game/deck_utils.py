import json
import os
from collections import Counter

_STATE_JSON = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "noble-hopper", "state.json")
)

# Maps ItemType strings (game API) to bot card type keys used in profiles and position logic
_ITEMTYPE_TO_CARD_KEY: dict[str, str] = {
    "ItemType_SDP_ZoneClosing":       "zone_close",
    "ItemType_SDP_ActivateAllPylons": "electromania",
    "ItemType_SDP_NakedAll":          "beach_party",
    "ItemType_SDP_Hecatombe":         "blood_moon",
    "ItemType_SDP_OpenZone":          "open_zone",
    "ItemType_SDP_LavaZone":          "lava_zone",
    "ItemType_SDP_NuclearBlast":      "nuclear_blast",
    "ItemType_SDP_AntiGravStorm":     "anti_grav_storm",
    "ItemType_SDP_ActivatePylon":     "spawn_electronic",
    "ItemType_SDP_WarmUp":            "warm_up",
    "ItemType_SDP_SpeedBoost":        "speed_boost",
    "ItemType_SDP_ManHunt":           "man_hunt",
    "ItemType_SDP_FavoritePlayer":    "favorite_player",
    "ItemType_SDP_GiveWood":          "give_wood",
    "ItemType_SDP_GiveLeather":       "give_leather",
    "ItemType_SDP_Telepathy":         "telepathy",
    "ItemType_SDP_MutualVision":      "expose",
}

# Director points required to play each card type
CARD_POINT_COSTS: dict[str, int] = {
    "zone_close":      3,
    "electromania":    3,
    "beach_party":     5,
    "blood_moon":      5,
    "open_zone":       5,
    "lava_zone":       5,
    "nuclear_blast":   5,
    "anti_grav_storm": 5,
    "spawn_electronic": 2,
    "warm_up":         1,
    "speed_boost":     1,
    "man_hunt":        5,
    "favorite_player": 0,
    "give_wood":       1,
    "give_leather":    1,
    "telepathy":       3,
    "expose":          3,
}


def deck_layout_from_state(state_json_path: str = _STATE_JSON) -> list[str]:
    """
    Read directorDeck from noble-hopper state.json and return a deck_layout list.
    Empty slots (ItemType_Null) are omitted. Unrecognised cards map to 'other'.
    Returns an empty list if state.json is missing or unreadable.
    """
    try:
        with open(state_json_path, encoding="utf-8") as f:
            deck = json.load(f).get("directorDeck", [])
    except Exception:
        return []

    layout = []
    for item_type in deck:
        if item_type == "ItemType_Null":
            continue
        layout.append(_ITEMTYPE_TO_CARD_KEY.get(item_type, "other"))
    return layout


def validate_profile_deck(profile: dict, deck_layout: list[str]) -> list[str]:
    """
    Return warning strings for any profile card plays that exceed the deck supply.
    Empty list means the deck fully covers the profile.
    """
    needed = Counter(p["card"] for p in profile.get("card_plays", []))
    available = Counter(c for c in deck_layout if c != "other")

    warnings = []
    for card_type, count in needed.items():
        have = available[card_type]
        if have < count:
            warnings.append(
                f"{card_type}: profile needs {count} but deck only has {have}"
            )
    return warnings

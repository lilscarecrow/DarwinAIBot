from game.name_snap import NameSnapper, ocr_fold


LOBBY = [
    {"discord_id": "1", "player": "Caution", "persona": "Robocop", "names": ["Caution", "Robocop"]},
    {"discord_id": "2", "player": "Saibu", "persona": "Saibu", "names": ["Saibu"]},
    {"discord_id": "3", "player": "ayitbunny", "persona": "ayitbunny", "names": ["ayitbunny"]},
    {"discord_id": "4", "player": "MILL", "persona": "M", "names": ["MILL", "M"]},
    {"discord_id": "5", "player": "shifty", "persona": "shifty", "names": ["shifty"]},
    {"discord_id": "6", "player": "Shiftv", "persona": "Shiftv", "names": ["Shiftv"]},
]


def test_fold_matches_the_ladder():
    assert ocr_fold("Rob0cop") == ocr_fold("Robocop") == "robocop"
    assert ocr_fold("‘saibu") == "salbu"
    assert ocr_fold("F0 ayitbunny") == "foavltbunnv"


def test_snaps_glyph_confusions_and_aliases_to_canonical():
    s = NameSnapper(LOBBY)
    assert s.snap("Rob0cop") == "Caution"       # persona/alias → canonical
    assert s.snap("Robocoo") == "Robocoo"       # not a fold match, left alone
    assert s.snap("‘saibu") == "Saibu"          # punctuation dropped by the fold
    assert s.snap("simt32") == "simt32"         # unknown stays verbatim
    assert s.snap("M") == "M"                   # too short to snap


def test_stream_tag_prefix_is_stripped_when_unique():
    s = NameSnapper(LOBBY)
    assert s.snap("F0 ayitbunny") == "ayitbunny"
    assert s.snap("FØ ayitbunny") == "ayitbunny"


def test_ambiguous_folds_never_snap():
    s = NameSnapper(LOBBY)
    assert s.snap("shifty") == "shifty"   # shifty vs Shiftv share a fold → exact read kept
    assert s.snap("shiftv") == "shiftv"


def test_snap_all_claims_each_player_once():
    s = NameSnapper(LOBBY)
    assert s.snap_all(["Rob0cop", "Robocop", "‘saibu", ""]) == ["Caution", "Robocop", "Saibu", ""]


def test_empty_lobby_is_identity():
    s = NameSnapper(None)
    assert len(s) == 0
    assert s.snap_all(["Rob0cop"]) == ["Rob0cop"]

from game.name_snap import NameSnapper, find_winning_slot, ocr_fold


LOBBY = [
    {"discord_id": "1", "player": "Caution", "persona": "Robocop", "names": ["Caution", "Robocop"]},
    {"discord_id": "2", "player": "Saibu", "persona": "Saibu", "names": ["Saibu"]},
    {"discord_id": "3", "player": "ayitbunny", "persona": "ayitbunny", "names": ["ayitbunny"]},
    {"discord_id": "4", "player": "MILL", "persona": "M", "names": ["MILL", "M"]},
    {"discord_id": "5", "player": "shifty", "persona": "shifty", "names": ["shifty"]},
    {"discord_id": "6", "player": "Shiftv", "persona": "Shiftv", "names": ["Shiftv"]},
    {"discord_id": "7", "player": "AlexanderTheGreat", "persona": "AlexanderTheGreat", "names": ["AlexanderTheGreat"]},
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


# ---- snap_all()'s fuzzy pass (2026-09-09) ----------------------------------

def test_snap_all_relaxes_a_near_miss_that_snap_alone_refuses():
    s = NameSnapper(LOBBY)
    assert s.snap("Robocoo") == "Robocoo"          # snap() alone stays conservative
    assert s.snap_all(["Robocoo"]) == ["Caution"]  # snap_all()'s fuzzy pass relaxes it


def test_snap_all_matches_a_scrolled_or_cut_off_name():
    # missing both the leading "Al" and the trailing "t" — a contiguous chunk
    # of the true name, the shape a scrolling/truncated nameplate would leave.
    s = NameSnapper(LOBBY)
    assert s.snap_all(["exanderTheGrea"]) == ["AlexanderTheGreat"]


def test_snap_all_leaves_an_unrecognizable_read_verbatim():
    s = NameSnapper(LOBBY)
    assert s.snap_all(["zzqqxx99"]) == ["zzqqxx99"]


def test_snap_all_fuzzy_pass_still_refuses_a_genuine_tie():
    # "shifty" and "Shiftv" already share an exact fold (see
    # test_ambiguous_folds_never_snap) — the fuzzy pass must not break that
    # tie either, for the same reason: no signal distinguishes them.
    s = NameSnapper(LOBBY)
    assert s.snap_all(["shifty", "shiftv"]) == ["shifty", "shiftv"]


def test_snap_all_fuzzy_pass_resolves_highest_confidence_first():
    # Both reads are plausible typos of "Robocop", but "Robocoo" is a
    # one-character miss (similarity ~0.857) while "Rxbocwp" is noisier
    # (~0.714, still above the confidence floor). The single player must go
    # to the more confident read regardless of input order, and the other
    # stays verbatim rather than guessed once its only real candidate is
    # already taken.
    s = NameSnapper(LOBBY)
    assert s.snap_all(["Rxbocwp", "Robocoo"]) == ["Rxbocwp", "Caution"]
    assert s.snap_all(["Robocoo", "Rxbocwp"]) == ["Caution", "Rxbocwp"]


def test_snap_all_fuzzy_pass_combines_with_exact_and_unknown_reads():
    s = NameSnapper(LOBBY)
    out = s.snap_all(["Robocoo", "‘saibu", "exanderTheGrea", "zzqqxx99", "shifty"])
    assert out == ["Caution", "Saibu", "AlexanderTheGreat", "zzqqxx99", "shifty"]


# ---- find_winning_slot() — results-screen winner -> captured slot (2026-09-09) ----

SLOT_MAP = {"1": "pefiss", "2": "Drowsy", "3": "Remix", "4": "nekrosu", "5": "S1lent", "6": "nick"}


def test_find_winning_slot_exact_match():
    assert find_winning_slot("pefiss", SLOT_MAP) == "1"


def test_find_winning_slot_near_miss_ocr_read():
    assert find_winning_slot("Drowsv", SLOT_MAP) == "2"  # y/v fold confusion


def test_find_winning_slot_refuses_unknown_name():
    assert find_winning_slot("totallyunknown", SLOT_MAP) is None


def test_find_winning_slot_refuses_empty_or_missing():
    assert find_winning_slot("", SLOT_MAP) is None
    assert find_winning_slot(None, SLOT_MAP) is None


def test_find_winning_slot_empty_map_refuses():
    assert find_winning_slot("pefiss", {}) is None


def test_find_winning_slot_refuses_a_genuine_tie():
    # Two slots close enough in name that a garbled read can't be called cleanly.
    tied = {"1": "abcde", "2": "abcdf"}
    assert find_winning_slot("abcd_", tied) is None

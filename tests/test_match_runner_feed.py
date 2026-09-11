"""_poll_damage_feed() — damage/kill feed OCR -> keyword-matched live events (2026-09-10).

_poll_damage_feed() only decides whether to spawn a background thread (see
its own tests below); the actual OCR/pattern-matching logic lives in
_poll_damage_feed_worker(), called directly here for deterministic,
non-threaded tests.
"""
import os
from unittest.mock import patch

import cv2
import pytest

from game.match_runner import MatchRunner
from session.state import SessionState


class FakeDraftLifecycle:
    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))

    def resolve_alias(self, name):
        """No ladder alias data in this fake — always falls through to
        find_winning_slot()'s fuzzy match, same as before resolve_alias()
        existed. See tests/test_ds_lifecycle.py for the real method's own
        coverage; this fake just needs to satisfy the interface."""
        return None


def make_runner(names):
    ds = FakeDraftLifecycle()
    runner = MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=ds)
    runner._player_names = list(names)
    return runner, ds


ROSTER = ["SteffKnight", "Hellcrying", "Guts", "M", ""]


def run_worker(runner, feed_text):
    """Drive _poll_damage_feed_worker() with a canned OCR read, without
    touching the real screen."""
    with patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.ocr.ocr_feed_text", return_value=feed_text):
        runner._poll_damage_feed_worker()


# ---- alias-first resolution via DraftLifecycle.resolve_alias (2026-09-11) --
#
# Real live incident: the feed clearly read "CONNOR" as a first-blood killer
# — a clean, correct OCR read — but that player's slot had resolved to a
# different display name ("Mojo") for this match. find_winning_slot()'s
# fuzzy score against slot_map alone matched an unrelated player ("Coen")
# instead, and the reward went to the wrong person. _resolve_feed_name() now
# tries DraftLifecycle.resolve_alias() (an exact match against every known
# alias for a player, not just their one already-decided slot name) first.

class AliasFakeDraftLifecycle(FakeDraftLifecycle):
    """resolve_alias() actually resolves one specific raw name to a
    canonical one, to prove that result wins over find_winning_slot()'s
    fuzzy match against slot_map."""
    def __init__(self, aliases):
        super().__init__()
        self._aliases = aliases

    def resolve_alias(self, name):
        return self._aliases.get(name)


def test_alias_resolution_wins_over_the_fuzzy_fallback():
    runner, _ = make_runner(["SteffKnight", "Hellcrying", "Guts"])
    runner._ds = AliasFakeDraftLifecycle({"CONNOR": "Guts"})
    run_worker(runner, "CONNOR DREW FIRST BLOOD FROM HELLCRYING")
    kind, fields = runner._ds.events[0]
    assert fields["killer_slot"] == "2"  # Guts, via the alias — not a fuzzy guess


def test_falls_back_to_fuzzy_matching_when_no_alias_resolves():
    runner, ds = make_runner(["SteffKnight", "Hellcrying", "Guts"])
    run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    kind, fields = ds.events[0]
    assert fields["killer_slot"] == "0"  # unchanged behavior — the fake never resolves an alias


def test_resolves_killer_and_victim_from_a_clean_line():
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    assert len(ds.events) == 1
    kind, fields = ds.events[0]
    assert kind == "feed_first_blood"
    assert fields["killer_slot"] == "0" and fields["victim_slot"] == "1"


def test_ignores_unrelated_adjacent_feed_lines():
    """The feed stacks several lines in one crop — an OCR block with a zone
    event and an elimination line on either side of the real one must not let
    those words bleed into the captured killer/victim names."""
    runner, ds = make_runner(ROSTER)
    text = "ZONE CLOSING\nSTEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING\nGUTS ELIMINATED M"
    run_worker(runner, text)
    _, fields = ds.events[0]
    assert fields["killer_raw"] == "STEFFKNIGHT"
    assert fields["victim_raw"] == "HELLCRYING"


def test_unresolvable_name_reports_none_not_a_guess():
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "ZZQQXX DREW FIRST BLOOD FROM HELLCRYING")
    _, fields = ds.events[0]
    assert fields["killer_slot"] is None
    assert fields["victim_slot"] == "1"


# ---- feed_kill: "X KILLED Y BY Z" (2026-09-10) -----------------------------

def test_feed_kill_resolves_killer_victim_and_method():
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "STEFFKNIGHT KILLED HELLCRYING BY AXE")
    assert len(ds.events) == 1
    kind, fields = ds.events[0]
    assert kind == "feed_kill"
    assert fields["killer_slot"] == "0" and fields["victim_slot"] == "1"
    assert fields["method"] == "AXE"


def test_feed_kill_matches_without_a_method():
    """The trailing "BY <method>" is optional — a line missing it (a
    genuinely method-less kill, or OCR clipping the tail) must still match,
    just without a method field."""
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "STEFFKNIGHT KILLED HELLCRYING")
    kind, fields = ds.events[0]
    assert kind == "feed_kill"
    assert "method" not in fields


def test_feed_kill_does_not_match_the_first_blood_line():
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    assert len(ds.events) == 1
    assert ds.events[0][0] == "feed_first_blood"


def test_feed_kill_does_not_queue_a_first_blood_reward():
    """Only feed_first_blood's killer_slot feeds the reward queue —
    feed_kill must never touch it, even though it also has a killer_slot
    field."""
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "STEFFKNIGHT KILLED HELLCRYING BY AXE")
    assert runner._first_blood_reward_slot is None


def test_no_matching_line_does_not_fire():
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "ZONE CLOSING IN 30 SECONDS")
    assert ds.events == []


def test_empty_ocr_read_does_not_fire():
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "")
    assert ds.events == []


def test_keeps_firing_on_later_polls_for_genuinely_new_lines():
    """Continuous polling (2026-09-10): unlike the first draft, which stopped
    forever after the first match, the same pattern must still be able to
    fire again on a later poll for NEW content — kills, in particular, are
    expected to recur many times across a match, and nothing here should
    assume "only ever once" for the pattern as a whole."""
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    run_worker(runner, "GUTS KILLED M BY ARROW")
    assert len(ds.events) == 2


def test_does_not_re_emit_the_exact_same_line_still_on_screen():
    """A feed line can linger across more than one poll — re-detecting the
    identical (kind, line) must not re-fire it as a duplicate event."""
    runner, ds = make_runner(ROSTER)
    run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    assert len(ds.events) == 1


# ---- real feed_kill captures (2026-09-10) — see tests/fixtures/feed_kill/README --

_FEED_KILL_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "feed_kill")


def run_worker_on_real_capture(runner, filename):
    """Drives _poll_damage_feed_worker() through the REAL ocr_feed_text()
    (not mocked) against a real captured raw crop — end-to-end proof that
    the \\s* regex relaxation below actually resolves a real merged-word
    kill line, not just the regex in isolation."""
    img = cv2.imread(os.path.join(_FEED_KILL_FIX, filename))
    assert img is not None, f"missing fixture: {filename}"
    # The fixture is already cropped to _CROP_REGION's size — point that
    # constant at (0, 0, w, h) so _poll_damage_feed_worker()'s real crop
    # step is a no-op against this already-cropped image.
    h, w = img.shape[:2]
    with patch("game.screen_detection.take_screenshot", return_value=img), \
         patch("game.video_recorder._CROP_REGION", (0, 0, w, h)), \
         patch("game.ocr.save_feed_debug_images"):  # don't actually write files during a test
        runner._poll_damage_feed_worker()


def test_a_merged_no_space_kill_line_still_resolves_end_to_end():
    """feed_kill_1.png OCRs as 'TWOKILLEDTHUGZBY ARROW,' — no space around
    KILLED, since that boundary sits against the colored player name. The
    old \\s+-based pattern silently failed to match this at all even though
    every word reads correctly; \\s* must catch it."""
    runner, ds = make_runner(["TWO", "THUGZ", "PEFISS"])
    run_worker_on_real_capture(runner, "feed_kill_1.png")
    assert len(ds.events) == 1
    kind, fields = ds.events[0]
    assert kind == "feed_kill"
    assert fields["killer_slot"] == "0" and fields["victim_slot"] == "1"
    assert fields["method"].startswith("ARROW")


def test_a_second_merged_kill_line_also_resolves():
    runner, ds = make_runner(["THEAURI", "KERO"])
    run_worker_on_real_capture(runner, "feed_kill_2.png")
    assert len(ds.events) == 1
    kind, fields = ds.events[0]
    assert kind == "feed_kill"
    assert fields["killer_slot"] == "0" and fields["victim_slot"] == "1"
    assert fields["method"] == "COLD"


# ---- feed debug image capture (2026-09-10) ---------------------------------

def test_a_matched_line_triggers_a_debug_image_save():
    """save_feed_debug_images() (game/ocr.py) is called whenever a pattern
    matches, so the next live occurrence leaves behind a raw-color crop to
    inspect — this is how the still-open colored-name OCR gap (player names
    render orange/red, not white; the current preprocessing can't separate
    them from a colored background) gets fixed with real pixel data instead
    of another guess."""
    runner, ds = make_runner(ROSTER)
    with patch("game.ocr.save_feed_debug_images") as save_debug:
        run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    save_debug.assert_called_once()
    args = save_debug.call_args[0]
    assert args[0] is None  # the (patched) screenshot, passed straight through
    assert "feed_first_blood" in args[3]  # label embeds the matched kind


def test_a_debug_save_failure_does_not_break_event_emission():
    runner, ds = make_runner(ROSTER)
    with patch("game.ocr.save_feed_debug_images", side_effect=RuntimeError("disk full")):
        run_worker(runner, "STEFFKNIGHT DREW FIRST BLOOD FROM HELLCRYING")
    assert len(ds.events) == 1


def test_no_debug_save_when_nothing_matches():
    runner, ds = make_runner(ROSTER)
    with patch("game.ocr.save_feed_debug_images") as save_debug:
        run_worker(runner, "ZONE CLOSING IN 30 SECONDS")
    save_debug.assert_not_called()


def test_worker_clears_the_busy_flag_even_on_failure():
    runner, ds = make_runner(ROSTER)
    runner._feed_poll_busy = True
    with patch("game.screen_detection.take_screenshot", side_effect=RuntimeError("boom")):
        runner._poll_damage_feed_worker()
    assert runner._feed_poll_busy is False
    assert ds.events == []


# ---- _poll_damage_feed(): the non-blocking dispatch wrapper ---------------

def test_dispatch_spawns_the_worker_on_its_own_thread():
    runner, ds = make_runner(ROSTER)
    with patch("game.match_runner.threading.Thread") as MockThread:
        runner._poll_damage_feed()
    assert runner._feed_poll_busy is True
    MockThread.assert_called_once()
    _, kwargs = MockThread.call_args
    assert kwargs["target"] == runner._poll_damage_feed_worker
    assert kwargs["daemon"] is True
    MockThread.return_value.start.assert_called_once()


def test_dispatch_skips_while_a_previous_poll_is_still_running():
    runner, ds = make_runner(ROSTER)
    runner._feed_poll_busy = True
    with patch("game.match_runner.threading.Thread") as MockThread:
        runner._poll_damage_feed()
    MockThread.assert_not_called()


def test_dispatch_noops_without_a_roster():
    """No point polling before _init_player_bar() has run — nothing to
    resolve names against, so this must not even spawn the thread."""
    runner, ds = make_runner([])
    with patch("game.match_runner.threading.Thread") as MockThread:
        runner._poll_damage_feed()
    MockThread.assert_not_called()
    assert runner._feed_poll_busy is False

"""_poll_damage_feed() — damage/kill feed OCR -> keyword-matched live events (2026-09-10).

_poll_damage_feed() only decides whether to spawn a background thread (see
its own tests below); the actual OCR/pattern-matching logic lives in
_poll_damage_feed_worker(), called directly here for deterministic,
non-threaded tests.
"""
from unittest.mock import patch

import pytest

from game.match_runner import MatchRunner
from session.state import SessionState


class FakeDraftLifecycle:
    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))


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

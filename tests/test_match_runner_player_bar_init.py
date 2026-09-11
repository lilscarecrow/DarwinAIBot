"""_fire_player_bar_init() — player-bar detection + ladder roster push must
never block the B-press (2026-09-10 fix).

_init_player_bar() alone can do up to one tesseract call per card
(game/player_cards_v2.py::ocr_names, each timeout-guarded up to 10s in
game/ocr.py) — inline in run(), that was blocking the B-press on every
match, the same class of bug already fixed for on_match_start_async
(2026-09-07) and _fire_slot_map_snapshot (2026-09-09) but never applied
here. _fire_player_bar_init() dispatches the whole thing (detection + OCR +
both ladder pushes) onto its own thread instead.
"""
from unittest.mock import patch

from game.match_runner import MatchRunner
from session.state import SessionState


class FakeDraftLifecycle:
    roster_size = 0
    game_index = 1

    def __init__(self):
        self.events = []
        self.match_start_calls = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))

    def resolve_alias(self, name):
        return None

    def on_match_start(self, names):
        self.match_start_calls.append(list(names))


def make_runner():
    ds = FakeDraftLifecycle()
    runner = MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=ds)
    return runner, ds


def test_fire_dispatches_to_its_own_thread_and_returns_immediately():
    runner, ds = make_runner()
    with patch("game.match_runner.threading.Thread") as MockThread:
        runner._fire_player_bar_init()
    MockThread.assert_called_once()
    _, kwargs = MockThread.call_args
    assert kwargs["target"] == runner._init_player_bar_and_push
    assert kwargs["daemon"] is True
    MockThread.return_value.start.assert_called_once()


def test_worker_detects_then_pushes_the_resulting_roster_to_the_ladder():
    runner, ds = make_runner()
    with patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.player_cards_v2.detect_cards", return_value=[]):
        runner._init_player_bar_and_push()
    # No cards detected -> _player_names stays empty, but the push must still
    # happen (an empty roster is still a legitimate push, not a skip).
    assert ds.match_start_calls == [[]]
    assert ds.events == [("match_start", {"elapsed_ms": 0, "slots": []})]


def test_worker_never_raises_even_if_the_ladder_push_fails():
    runner, ds = make_runner()
    ds.on_match_start = lambda names: (_ for _ in ()).throw(RuntimeError("boom"))
    with patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.player_cards_v2.detect_cards", return_value=[]):
        runner._init_player_bar_and_push()  # must not raise


def test_worker_skips_the_ladder_push_when_no_lifecycle_is_wired():
    runner = MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=None)
    with patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.player_cards_v2.detect_cards", return_value=[]):
        runner._init_player_bar_and_push()  # must not raise with no self._ds


# ---- OBS "Game N" banner (2026-09-11) --------------------------------------

def test_updates_the_obs_banner_with_the_current_game_number():
    runner, ds = make_runner()
    ds.game_index = 3
    with patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.player_cards_v2.detect_cards", return_value=[]), \
         patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._init_player_bar_and_push()
    set_text.assert_called_once_with(runner._game_number_source, "Game 3")


def test_does_not_touch_obs_when_streaming_is_disabled():
    runner, ds = make_runner()
    with patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.player_cards_v2.detect_cards", return_value=[]), \
         patch("game.obs_control.is_enabled", return_value=False), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._init_player_bar_and_push()
    set_text.assert_not_called()


def test_no_obs_call_when_no_lifecycle_is_wired():
    runner = MatchRunner({}, SessionState(), lambda *a: None, draft_lifecycle=None)
    with patch("game.screen_detection.take_screenshot", return_value=None), \
         patch("game.player_cards_v2.detect_cards", return_value=[]), \
         patch("game.obs_control.is_enabled", return_value=True), \
         patch("game.obs_control.set_source_text") as set_text:
        runner._init_player_bar_and_push()  # must not raise, no self._ds to read game_index from
    set_text.assert_not_called()

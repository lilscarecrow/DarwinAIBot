"""DraftLifecycle with a fake transport: the open/roster/results/close state machine."""
import logging

import pytest

from game.ds_lifecycle import DraftLifecycle


class FakeTransport:
    def __init__(self, open_result=11, close_result=True):
        self.calls = []
        self.rosters = []
        self.open_result = open_result
        self.close_result = close_result

    def open_set_draft(self, names, base_url, token, platform="pc", twitch_channel=None, draft_id=None, roster=None):
        self.calls.append(("open", list(names), platform, twitch_channel, draft_id, base_url, token))
        self.rosters.append(list(roster or []))
        return self.open_result() if callable(self.open_result) else self.open_result

    def close_set_draft(self, draft_id, base_url, token, reason=""):
        self.calls.append(("close", draft_id, reason, base_url, token))
        return self.close_result() if callable(self.close_result) else self.close_result

    def post_results_screenshot(self, path, base_url, token, platform="pc", roster=None, draft_id=None):
        self.calls.append(("shot", path, platform, roster, draft_id))


CFG = {
    "ds_ingest_token": "tok",
    "ds_ingest_base_url": "https://ds.test",
    "ds_ingest_platform": "pc",
    "ds_ingest_twitch_channel": "scarecrow",
}


def make(cfg=CFG, **kw):
    t = FakeTransport(**kw)
    return DraftLifecycle(cfg, transport=t), t


def test_disabled_without_token_makes_no_calls():
    ds, t = make({"ds_ingest_twitch_channel": "x"})
    assert not ds.enabled
    assert ds.open_lobby() is None
    assert ds.on_match_start(["A"]) is None
    ds.post_results("/tmp/x.png")
    assert ds.close("quit") is False
    assert t.calls == []


def test_open_lobby_opens_empty_roster_with_channel():
    ds, t = make()
    assert ds.open_lobby() == 11
    assert ds.draft_id == 11
    assert t.calls == [("open", [], "pc", "scarecrow", None, "https://ds.test", "tok")]


def test_open_lobby_forwards_discord_roster():
    ds, t = make()
    ds.open_lobby(roster=["111", "222"])
    assert t.rosters == [["111", "222"]]
    # Match-start top-up re-sends the lobby's roster so the server fuzzy-matches
    # OCR names against THIS lobby, not the whole season.
    ds.on_match_start(["Alpha"])
    assert t.rosters[-1] == ["111", "222"]
    # A close forgets it; the next lobby starts clean.
    ds.close("quit")
    ds.open_lobby()
    assert t.rosters[-1] == []


def test_open_lobby_without_roster_sends_empty_list():
    ds, t = make()
    ds.open_lobby()
    assert t.rosters == [[]]


def test_open_lobby_warns_when_channel_missing(caplog):
    cfg = dict(CFG, ds_ingest_twitch_channel="")
    ds, t = make(cfg)
    with caplog.at_level(logging.WARNING):
        ds.open_lobby()
    assert t.calls[0][3] is None
    assert any("WITHOUT a twitch_channel" in r.message for r in caplog.records)


def test_open_lobby_failure_is_a_warning_not_silent(caplog):
    ds, t = make(open_result=None)
    with caplog.at_level(logging.WARNING):
        assert ds.open_lobby() is None
    assert ds.draft_id is None
    assert any("could not open a draft" in r.message for r in caplog.records)


def test_match_start_reuses_lobby_draft():
    ds, t = make()
    ds.open_lobby()
    assert ds.on_match_start(["Alpha", "", " Bravo"]) == 11
    assert t.calls[-1] == ("open", ["Alpha", "Bravo"], "pc", "scarecrow", 11, "https://ds.test", "tok")


def test_match_start_with_no_names_keeps_draft_and_warns(caplog):
    ds, t = make()
    ds.open_lobby()
    n = len(t.calls)
    with caplog.at_level(logging.WARNING):
        assert ds.on_match_start([]) == 11
        assert ds.on_match_start(["", "  "]) == 11
    assert len(t.calls) == n, "no server call when OCR returned nothing"
    assert any("OCR returned no names" in r.message for r in caplog.records)


def test_match_start_without_lobby_opens_one_even_with_no_names():
    ds, t = make()
    assert ds.on_match_start([]) == 11
    assert t.calls == [("open", [], "pc", "scarecrow", None, "https://ds.test", "tok")]
    ds2, t2 = make()
    assert ds2.on_match_start(["A"]) == 11
    assert t2.calls[0][1] == ["A"] and t2.calls[0][4] is None


def test_match_start_adopts_server_supplied_id():
    ids = iter([11, 12])
    ds, t = make(open_result=lambda: next(ids))
    ds.open_lobby()
    assert ds.on_match_start(["A"]) == 12
    assert ds.draft_id == 12


def test_match_start_failure_keeps_previous_id(caplog):
    ids = iter([11, None])
    ds, t = make(open_result=lambda: next(ids))
    ds.open_lobby()
    with caplog.at_level(logging.WARNING):
        assert ds.on_match_start(["A"]) == 11
    assert ds.draft_id == 11


def test_post_results_targets_open_draft():
    ds, t = make()
    ds.open_lobby()
    ds.post_results("/tmp/r.png", roster=["1"])
    assert t.calls[-1] == ("shot", "/tmp/r.png", "pc", ["1"], 11)


def test_post_results_without_draft_sends_none_id():
    ds, t = make()
    ds.post_results("/tmp/r.png")
    assert t.calls == [("shot", "/tmp/r.png", "pc", None, None)]


def test_close_sends_reason_and_clears_id():
    ds, t = make()
    ds.open_lobby()
    assert ds.close("quit") is True
    assert t.calls[-1] == ("close", 11, "quit", "https://ds.test", "tok")
    assert ds.draft_id is None


def test_close_without_draft_makes_no_call():
    ds, t = make()
    assert ds.close("quit") is False
    assert t.calls == []


def test_close_clears_id_even_when_server_says_no(caplog):
    ds, t = make(close_result=False)
    ds.open_lobby()
    with caplog.at_level(logging.WARNING):
        assert ds.close("idle timeout") is False
    assert ds.draft_id is None
    assert any("server sweep" in r.message for r in caplog.records)


def test_new_lobby_after_close_starts_fresh_draft():
    ids = iter([11, 12])
    ds, t = make(open_result=lambda: next(ids))
    ds.open_lobby()
    ds.close("quit")
    assert ds.open_lobby() == 12


def test_full_set_then_new_lobby_replaces_id_without_close():
    ids = iter([11, 12])
    ds, t = make(open_result=lambda: next(ids))
    ds.open_lobby()
    for _ in range(4):
        ds.post_results("/tmp/g.png")
    # 4th game landed: no close (moderator queue takes over); next /custom is a new draft.
    assert ds.open_lobby() == 12
    assert not any(c[0] == "close" for c in t.calls)


class RaisingTransport(FakeTransport):
    def open_set_draft(self, *a, **k):
        raise RuntimeError("boom")

    def close_set_draft(self, *a, **k):
        raise RuntimeError("boom")

    def post_results_screenshot(self, *a, **k):
        raise RuntimeError("boom")


def test_transport_exceptions_are_swallowed(caplog):
    ds = DraftLifecycle(CFG, transport=RaisingTransport())
    with caplog.at_level(logging.WARNING):
        assert ds.open_lobby() is None
        assert ds.on_match_start(["A"]) is None
        ds.post_results("/tmp/x.png")
        ds._draft_id = 3
        assert ds.close("x") is False
    assert ds.draft_id is None
    assert sum("transport raised" in r.message for r in caplog.records) >= 3


def test_defaults_when_config_sparse():
    ds, t = make({"ds_ingest_token": "tok"})
    ds.open_lobby()
    assert t.calls[0][2] == "pc"
    assert t.calls[0][5] == "https://darwinstalker.com"
    assert t.calls[0][3] is None

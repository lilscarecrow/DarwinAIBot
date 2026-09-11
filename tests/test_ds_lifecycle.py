"""DraftLifecycle with a fake transport: the open/roster/results/close state machine."""
import logging
import threading
import time

import pytest

from game.ds_lifecycle import DraftLifecycle


class FakeTransport:
    def __init__(self, open_result=11, close_result=True, screenshot_result=None):
        self.calls = []
        self.rosters = []
        self.slugs = []
        self.open_result = open_result
        self.close_result = close_result
        self.screenshot_result = screenshot_result

    def open_set_draft(self, names, base_url, token, platform="pc", twitch_channel=None, draft_id=None,
                       roster=None, tournament_slug=None):
        self.calls.append(("open", list(names), platform, twitch_channel, draft_id, base_url, token))
        self.rosters.append(list(roster or []))
        self.slugs.append(tournament_slug)
        return self.open_result() if callable(self.open_result) else self.open_result

    # relay transport (DraftLifecycle shares this object with its DsRelay)
    def post_events(self, draft_id, game_index, events, base_url, token):
        self.calls.append(("events", draft_id, game_index, list(events)))
        return True

    def post_log(self, entries, base_url, token):
        self.calls.append(("log", list(entries)))
        return True

    def close_set_draft(self, draft_id, base_url, token, reason=""):
        self.calls.append(("close", draft_id, reason, base_url, token))
        return self.close_result() if callable(self.close_result) else self.close_result

    def post_results_screenshot(self, path, base_url, token, platform="pc", roster=None, draft_id=None):
        self.calls.append(("shot", path, platform, roster, draft_id))
        return self.screenshot_result() if callable(self.screenshot_result) else self.screenshot_result


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


class BlockingTransport(FakeTransport):
    """A transport whose open_set_draft blocks on a gate — lets a test prove a
    caller returned before the network call finished, not just that it
    eventually finished (a fast fake transport races that check away)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = threading.Event()

    def open_set_draft(self, *a, **kw):
        self.gate.wait(2)
        return super().open_set_draft(*a, **kw)


def test_match_start_async_does_not_block_the_caller():
    t = BlockingTransport()
    ds = DraftLifecycle(CFG, transport=t)
    t.gate.set()          # let the synchronous open_lobby() through immediately
    ds.open_lobby()
    t.gate.clear()         # now block the next open_set_draft call — the async one
    start = time.monotonic()
    ds.on_match_start_async(["Alpha"])
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, "on_match_start_async must return immediately, not block on the network call"
    assert len(t.calls) == 1, "the async call must not have reached the transport yet"
    t.gate.set()
    for _ in range(100):
        if len(t.calls) > 1:
            break
        time.sleep(0.02)
    assert t.calls[-1] == ("open", ["Alpha"], "pc", "scarecrow", 11, "https://ds.test", "tok")


def test_match_start_async_noop_when_disabled():
    ds, t = make({"ds_ingest_twitch_channel": "x"})
    before = set(threading.enumerate())
    ds.on_match_start_async(["Alpha"])
    assert set(threading.enumerate()) == before, "must not spawn a thread when ingest is disabled"
    assert t.calls == []


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


def test_post_results_returns_the_transport_body_for_prediction_resolution():
    body = {"draft_id": 11, "game_index": 1, "ocr_error": None, "placements": [{"rank": 1, "name": "pefiss"}]}
    ds, t = make(screenshot_result=body)
    ds.open_lobby()
    assert ds.post_results("/tmp/r.png") == body


def test_post_results_returns_none_when_transport_returns_nothing():
    ds, t = make()  # default screenshot_result=None
    ds.open_lobby()
    assert ds.post_results("/tmp/r.png") is None


def test_post_results_returns_none_when_disabled():
    ds, t = make({"ds_ingest_twitch_channel": "x"})
    assert ds.post_results("/tmp/r.png") is None
    assert t.calls == []


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


# ---- game_index seeded from the server's next_game_index (2026-09-11) -----
#
# A bot restart reconnecting into a draft that already has games recorded
# (e.g. a crash mid-set) used to always reset game_index to 1 — wrong if the
# server's draft already has games 1-2 recorded, mistagging the next batch
# of live events. open-draft's reply can now carry next_game_index (the
# server's own "next unrecorded slot" computation); open_lobby() seeds from
# it instead of assuming 1 whenever the draft id actually changed.

def test_game_index_seeded_from_next_game_index_on_a_new_draft():
    reply = {"draft_id": 12, "created": True, "rows": 0, "lobby": [], "expected_names": [], "unlinked": [], "next_game_index": 3}
    ds, t = make(open_result=reply)
    ds.open_lobby()
    assert ds.game_index == 3


def test_game_index_defaults_to_1_when_next_game_index_is_absent():
    """An older server reply (or one with no game data yet) doesn't carry
    next_game_index at all — falls back to the old behavior."""
    reply = {"draft_id": 12, "created": True, "rows": 0, "lobby": [], "expected_names": [], "unlinked": []}
    ds, t = make(open_result=reply)
    ds.open_lobby()
    assert ds.game_index == 1


def test_game_index_defaults_to_1_for_a_bare_id_result():
    """Older transports/tests return a plain int, not a dict — no
    next_game_index to read, same fallback."""
    ds, t = make(open_result=11)
    ds.open_lobby()
    assert ds.game_index == 1


def test_next_game_index_does_not_affect_reopening_the_same_draft():
    """Only a genuinely NEW draft id resets game_index at all — reopening the
    same one (e.g. /start after /custom) must not let a stale
    next_game_index clobber a count post_results() has already advanced."""
    reply_1 = {"draft_id": 12, "created": True, "rows": 0, "lobby": [], "expected_names": [], "unlinked": [], "next_game_index": 1}
    ds, t = make(open_result=lambda: reply_1)
    ds.open_lobby()
    ds.post_results("/tmp/g1.png")
    assert ds.game_index == 2
    ds.open_lobby()  # same draft_id — even a (deliberately stale) next_game_index of 1 must be ignored
    assert ds.game_index == 2, "same draft id — must not reset"


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


# ---- tournament slug ---------------------------------------------------------

def test_tournament_slug_forwarded_only_when_given():
    ds, t = make()
    ds.open_lobby()
    ds.open_lobby(tournament_slug="hotdog-hoedown")
    ds.open_lobby(tournament_slug="")
    assert t.slugs == [None, "hotdog-hoedown", None]


# ---- game index + live events + log relay ---------------------------------------

def test_open_reply_dict_feeds_lobby_snapping_and_unlinked():
    reply = {
        "draft_id": 11, "created": True, "rows": 2, "roster_resolved": 1,
        "lobby": [{"discord_id": "d1", "player": "Caution", "persona": "Robocop", "names": ["Caution", "Robocop"]}],
        "expected_names": ["Caution", "Robocop"],
        "unlinked": [{"discord_id": "d2", "names": ["zombie"]}],
    }
    ds, t = make(open_result=reply)
    assert ds.open_lobby(roster=["d1", {"id": "d2", "names": ["zombie"]}]) == 11
    assert ds.draft_id == 11
    assert ds.expected_names == ["Caution", "Robocop"]
    assert ds.unlinked == [{"discord_id": "d2", "names": ["zombie"]}]
    assert ds.snap_names(["Rob0cop", "‘saibu"]) == ["Caution", "‘saibu"]
    # The roster went out with names, the screenshot goes out with ids only.
    assert t.rosters[-1] == ["d1", {"id": "d2", "names": ["zombie"]}]
    ds.post_results("/tmp/g1.png", roster=ds._roster)
    shot = [c for c in t.calls if c[0] == "shot"][-1]
    assert shot[3] == ["d1", "d2"]
    ds.close("quit")
    assert ds.unlinked == [] and ds.snap_names(["Rob0cop"]) == ["Rob0cop"]


def test_roster_accepts_member_objects():
    class M:
        def __init__(self, id, name, global_name=None, nick=None):
            self.id, self.name, self.global_name, self.nick = id, name, global_name, nick
    ds, t = make()
    ds.open_lobby(roster=[M(1, "luczer_", "SlyK"), M(2, "plain"), "3"])
    assert t.rosters[-1] == [
        {"id": "1", "names": ["SlyK", "luczer_"]},
        {"id": "2", "names": ["plain"]},
        "3",
    ]
    assert ds.roster_size == 3


def test_reopening_the_same_draft_keeps_the_game_index():
    # /custom runs once per game; the server answers with the session's open
    # draft. The counter must carry on, or every game streams as game 1.
    ds, t = make()
    ds.open_lobby()
    ds.post_results("/tmp/g1.png")
    assert ds.game_index == 2
    ds.open_lobby()                      # lobby for game 2 → same draft 11
    assert ds.game_index == 2
    ds.event("match_start", elapsed_ms=0)
    ds.relay.flush(2.0)
    ev_calls = [c for c in t.calls if c[0] == "events"]
    assert ev_calls and ev_calls[-1][2] == 2
    ds.post_results("/tmp/g2.png")
    ds.open_lobby()
    assert ds.game_index == 3
    # A different draft (previous one closed server-side) is a new set: back to 1.
    t.open_result = 12
    ds.open_lobby()
    assert ds.draft_id == 12 and ds.game_index == 1


def test_game_index_tracks_lobby_results_close():
    ds, t = make()
    assert ds.game_index == 0
    ds.open_lobby()
    assert ds.game_index == 1
    ds.post_results("/tmp/g1.png")
    assert ds.game_index == 2
    ds.post_results("/tmp/g2.png")
    assert ds.game_index == 3
    ds.close("quit")
    assert ds.game_index == 0


def test_event_shape_splits_top_level_from_data():
    ds, t = make()
    ds.open_lobby()
    ds.event("eliminated", slot=3, player="Bael", elapsed_ms=12345, alive=6)
    ds.event("match_end", elapsed_ms=99)
    ds.event("say", text="gg", by="lo")
    ds.relay.flush()
    ev_calls = [c for c in t.calls if c[0] == "events"]
    assert len(ev_calls) == 1
    _, draft_id, game_index, events = ev_calls[0]
    assert draft_id == 11 and game_index == 1
    e0, e1, e2 = events
    assert e0["kind"] == "eliminated" and e0["slot"] == 3 and e0["player"] == "Bael"
    assert e0["elapsed_ms"] == 12345 and e0["data"] == {"alive": 6} and isinstance(e0["at"], int)
    assert e1 == {"kind": "match_end", "at": e1["at"], "elapsed_ms": 99}
    assert e2["data"] == {"text": "gg", "by": "lo"} and "slot" not in e2


def test_events_carry_the_game_they_happened_in():
    ds, t = make()
    ds.open_lobby()
    ds.event("match_start", elapsed_ms=0, slots=["A"])
    ds.post_results("/tmp/g1.png")        # flushes game 1, then moves to game 2
    ds.event("match_start", elapsed_ms=0, slots=["A"])
    ds.relay.flush()
    ev = [(c[2], [e["kind"] for e in c[3]]) for c in t.calls if c[0] == "events"]
    assert ev == [(1, ["match_start"]), (2, ["match_start"])]
    # and the game-1 events were posted BEFORE the screenshot
    kinds = [c[0] for c in t.calls]
    assert kinds.index("events") < kinds.index("shot")


def test_close_flushes_events_before_close_draft():
    ds, t = make()
    ds.open_lobby()
    ds.event("aborted", reason="force stopped")
    ds.log("warn", "warning", "something odd")
    ds.close("quit")
    kinds = [c[0] for c in t.calls]
    assert kinds.index("events") < kinds.index("close")
    assert kinds.index("log") < kinds.index("close")
    assert ds.draft_id is None and ds.game_index == 0


def test_log_relay_entries_shape():
    ds, t = make()
    ds.log("error", "error", "boom", {"logger": "x"})
    ds.relay.flush()
    log_calls = [c for c in t.calls if c[0] == "log"]
    assert len(log_calls) == 1
    e = log_calls[0][1][0]
    assert (e["level"], e["kind"], e["message"], e["fields"]) == ("error", "error", "boom", {"logger": "x"})


def test_events_and_logs_noop_when_disabled():
    ds, t = make({"ds_ingest_twitch_channel": "x"})
    ds.event("say", text="hi")
    ds.log("warn", "warning", "w")
    ds.relay.flush()
    assert t.calls == []
    assert ds.relay.pending() == (0, 0)


def test_events_without_open_draft_are_dropped_not_sent():
    ds, t = make()
    ds.event("status", last="a", next="b")
    ds.relay.flush()
    assert [c for c in t.calls if c[0] == "events"] == []
    assert ds.relay.dropped_events == 1


def test_claim_nudge_names_unlinked_with_claim_site():
    reply = {
        "draft_id": 12, "created": True,
        "unlinked": [{"discord_id": "d2", "names": ["zombie"]}, {"names": ["no-id"]}, {"discord_id": "d3"}],
    }
    ds, t = make(open_result=reply)
    ds.open_lobby(roster=["d1", "d2", "d3"])
    nudge = ds.claim_nudge()
    assert nudge["mentions"] == "<@d2> <@d3>"
    assert nudge["text"].startswith("Sign in with Steam at https://ds.test and claim your handle")
    ds.close("quit")
    assert ds.claim_nudge() is None


def test_claim_nudge_uses_configured_site_and_none_when_all_linked():
    ds, t = make(open_result={"draft_id": 13, "created": True, "unlinked": [{"discord_id": "d9"}]})
    ds._config["ds_ingest_base_url"] = "http://localhost:3999/"
    ds.open_lobby(roster=["d9"])
    assert "at http://localhost:3999 and" in ds.claim_nudge()["text"]
    ds, t = make({"ds_ingest_token": "tok"}, open_result={"draft_id": 15, "created": True, "unlinked": [{"discord_id": "d9"}]})
    ds.open_lobby(roster=["d9"])
    assert "at https://darwinstalker.com and" in ds.claim_nudge()["text"]
    ds, t = make(open_result={"draft_id": 14, "created": True, "unlinked": []})
    ds.open_lobby(roster=["d1"])
    assert ds.claim_nudge() is None


# ---- draft rows as the snap anchor (2026-09-10, draft 287) --------------------

def test_open_reply_draft_players_anchor_nameplate_snapping():
    """The scorecard's own rows — not only Discord-linked members — are what a
    player-bar read snaps to. FrozenNQ/Lou/thugz had no Discord link, so they
    were never in `lobby`; the server now hands them over as `draft_players`."""
    reply = {
        "draft_id": 11, "created": False, "rows": 4, "roster_resolved": 0,
        "lobby": [{"discord_id": "d1", "player": "kero", "persona": "kero", "names": ["kero"]}],
        "expected_names": ["kero", "FrozenNQ", "Lou", "thugz", "thugztiago"],
        "unlinked": [{"discord_id": "d2", "names": ["thugztiago"]}],
        "draft_players": [
            {"player": "kero", "player_id": 59, "games": 1, "names": ["kero"]},
            {"player": "FrozenNQ", "player_id": 355, "games": 1, "names": ["FrozenNQ"]},
            {"player": "Lou", "player_id": 10351, "games": 1, "names": ["Lou"]},
            {"player": "thugz", "player_id": 106, "games": 1, "names": ["thugz", "thugztiago"]},
        ],
        "skipped_unresolved": ["FrozenNO"],
    }
    ds, t = make(open_result=reply)
    assert ds.open_lobby(roster=["d1", {"id": "d2", "names": ["thugztiago"]}]) == 11
    assert [p["player"] for p in ds.draft_players] == ["kero", "FrozenNQ", "Lou", "thugz"]
    assert ds.known_players == {"kero", "FrozenNQ", "Lou", "thugz"}
    # Exact fold, fuzzy (one glyph), fuzzy (an alias), and a read nobody matches.
    assert ds.snap_names(["kero", "FrozenNO", "thugztiag", "zzqq"]) == ["kero", "FrozenNQ", "thugz", "zzqq"]
    # The match-start push sends the snapped names, so the server sees the row's spelling.
    ds.on_match_start(ds.snap_names(["FrozenNO", "thuaz"]))
    assert t.calls[-1][1] == ["FrozenNQ", "thugz"]
    ds.close("quit")
    assert ds.draft_players == [] and ds.snap_names(["FrozenNO"]) == ["FrozenNO"]


def test_results_placements_become_snap_targets_for_the_next_match():
    """Game 1's scorecard names (the screenshot reply's `placements`) are the
    roster from then on: the next player-bar read snaps to them even before
    the next open-draft reply arrives."""
    shot = {"draft_id": 11, "game_index": 1, "ocr_error": None, "placements": [
        {"rank": 1, "name": "Remix", "player_id": 89},
        {"rank": 2, "name": "Phillpeace", "player_id": None},
        {"rank": 3, "name": "Lou", "player_id": 10351},
    ]}
    ds, t = make(open_result={"draft_id": 11, "created": True, "rows": 0, "lobby": [], "expected_names": [], "unlinked": []},
                 screenshot_result=shot)
    ds.open_lobby()
    assert ds.snap_names(["Philipeace"]) == ["Philipeace"], "nothing known yet: verbatim"
    body = ds.post_results("/tmp/g1.png")
    assert body["placements"][0]["name"] == "Remix"
    assert ds.known_players == {"Remix", "Phillpeace", "Lou"}
    assert ds.snap_names(["Rem1x", "Philipeace", "Tou"]) == ["Remix", "Phillpeace", "Lou"]
    # A later open-draft reply that lists rows replaces the set (a pruned row stops being a target).
    t.open_result = {"draft_id": 11, "created": False, "rows": 1, "lobby": [], "expected_names": ["Remix"], "unlinked": [],
                     "draft_players": [{"player": "Remix", "player_id": 89, "games": 1, "names": ["Remix"]}]}
    ds.on_match_start(["Rem1x"])
    assert ds.known_players == {"Remix"}


def test_older_server_reply_without_draft_players_still_snaps_from_lobby():
    reply = {"draft_id": 11, "created": True, "rows": 1, "roster_resolved": 1,
             "lobby": [{"discord_id": "d1", "player": "Caution", "persona": None, "names": ["Caution", "Robocop"]}],
             "expected_names": ["Caution", "Robocop"], "unlinked": []}
    ds, t = make(open_result=reply)
    ds.open_lobby(roster=["d1"])
    assert ds.draft_players == []
    assert ds.snap_names(["Rob0cop"]) == ["Caution"]


# ---- resolve_alias() — single-name exact lookup (2026-09-11) --------------
#
# Added for MatchRunner's damage-feed killer/victim resolution: a live
# incident showed a clean, correctly-OCR'd read ("CONNOR") that wasn't this
# match's already-resolved slot display name for that player ("Mojo") — but
# "connor" WAS one of that player's other known ladder aliases the whole
# time. resolve_alias() finds that directly (exact fold match), unlike
# find_winning_slot()'s fuzzy score against slot_map, which matched an
# unrelated player instead.

def test_resolve_alias_finds_a_known_alias_not_the_display_name():
    reply = {
        "draft_id": 11, "created": True, "rows": 1, "roster_resolved": 1,
        "lobby": [{"discord_id": "d1", "player": "Mojo", "persona": "connor", "names": ["Mojo", "connor"]}],
        "expected_names": ["Mojo"], "unlinked": [],
    }
    ds, t = make(open_result=reply)
    ds.open_lobby(roster=["d1"])
    assert ds.resolve_alias("CONNOR") == "Mojo"


def test_resolve_alias_returns_none_for_an_unknown_name():
    ds, t = make(open_result={"draft_id": 11, "created": True, "rows": 0, "lobby": [], "expected_names": [], "unlinked": []})
    ds.open_lobby(roster=[])
    assert ds.resolve_alias("totallyunknown") is None


def test_resolve_alias_never_uses_the_fuzzy_pass():
    """Unlike find_winning_slot(), a clean read that merely resembles a
    player's name (rather than matching a registered alias exactly) must
    stay unresolved here — that's find_winning_slot()'s job, with its own
    confidence floor, not this method's."""
    reply = {
        "draft_id": 11, "created": True, "rows": 1, "roster_resolved": 1,
        "lobby": [{"discord_id": "d1", "player": "Coen", "persona": None, "names": ["Coen"]}],
        "expected_names": ["Coen"], "unlinked": [],
    }
    ds, t = make(open_result=reply)
    ds.open_lobby(roster=["d1"])
    assert ds.resolve_alias("CONNOR") is None

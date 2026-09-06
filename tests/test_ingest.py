"""game/ingest.py against a mocked HTTP layer: exact wire shapes and failure handling."""
import logging

import pytest
import requests

from game import ingest

BASE = "https://ds.test"
TOKEN = "tok"


def _open_url():
    return f"{BASE}/api/ingest/open-draft"


def test_open_sends_canonical_players_key(requests_mock):
    m = requests_mock.post(_open_url(), json={"draft_id": 7, "created": True, "rows": 2})
    got = ingest.open_set_draft(["Alpha", " Bravo ", ""], BASE, TOKEN, twitch_channel="chan")
    assert got == 7
    body = m.last_request.json()
    assert body == {"platform": "pc", "players": ["Alpha", "Bravo"], "twitch_channel": "chan"}
    assert m.last_request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_open_with_empty_roster_still_posts(requests_mock):
    m = requests_mock.post(_open_url(), json={"draft_id": 9, "created": True, "rows": 0})
    assert ingest.open_set_draft([], BASE, TOKEN) == 9
    assert m.called
    assert m.last_request.json()["players"] == []


@pytest.mark.parametrize("channel", [None, ""])
def test_open_omits_twitch_channel_when_unset(requests_mock, channel):
    m = requests_mock.post(_open_url(), json={"draft_id": 1})
    ingest.open_set_draft(["A"], BASE, TOKEN, twitch_channel=channel)
    assert "twitch_channel" not in m.last_request.json()


def test_open_forwards_draft_id_and_platform(requests_mock):
    m = requests_mock.post(_open_url(), json={"draft_id": 42})
    ingest.open_set_draft(["A"], BASE, TOKEN, platform="xbox", draft_id=42)
    body = m.last_request.json()
    assert body["draft_id"] == 42
    assert body["platform"] == "xbox"


def test_open_omits_draft_id_when_none(requests_mock):
    m = requests_mock.post(_open_url(), json={"draft_id": 1})
    ingest.open_set_draft(["A"], BASE, TOKEN)
    assert "draft_id" not in m.last_request.json()


@pytest.mark.parametrize("status", [400, 401, 422, 500])
def test_open_4xx_5xx_returns_none_and_warns(requests_mock, caplog, status):
    requests_mock.post(_open_url(), status_code=status, json={"error": "nope"})
    with caplog.at_level(logging.WARNING):
        assert ingest.open_set_draft(["A"], BASE, TOKEN) is None
    assert any(f"HTTP {status}" in r.message for r in caplog.records)


def test_open_network_error_returns_none(requests_mock, caplog):
    requests_mock.post(_open_url(), exc=requests.ConnectionError("down"))
    with caplog.at_level(logging.WARNING):
        assert ingest.open_set_draft(["A"], BASE, TOKEN) is None
    assert any("request failed" in r.message for r in caplog.records)


def test_open_trailing_slash_base_url(requests_mock):
    m = requests_mock.post(_open_url(), json={"draft_id": 1})
    ingest.open_set_draft(["A"], BASE + "/", TOKEN)
    assert m.called


# ---- close ----------------------------------------------------------------

def _close_url():
    return f"{BASE}/api/ingest/close-draft"


def test_close_round_trip_discarded(requests_mock, caplog):
    m = requests_mock.post(_close_url(), json={"draft_id": 5, "closed": True, "discarded": True})
    with caplog.at_level(logging.INFO):
        assert ingest.close_set_draft(5, BASE, TOKEN, reason="quit") is True
    assert m.last_request.json() == {"draft_id": 5, "reason": "quit"}
    assert any("discarded=True" in r.message for r in caplog.records)


def test_close_kept_for_review(requests_mock):
    requests_mock.post(_close_url(), json={"draft_id": 5, "closed": True, "discarded": False})
    assert ingest.close_set_draft(5, BASE, TOKEN) is True


def test_close_already_closed_is_still_ok(requests_mock):
    requests_mock.post(_close_url(), json={"draft_id": 5, "closed": False, "status": "approved"})
    assert ingest.close_set_draft(5, BASE, TOKEN) is True


def test_close_omits_empty_reason_and_truncates_long(requests_mock):
    m = requests_mock.post(_close_url(), json={"draft_id": 5, "closed": True, "discarded": True})
    ingest.close_set_draft(5, BASE, TOKEN, reason="")
    assert "reason" not in m.last_request.json()
    ingest.close_set_draft(5, BASE, TOKEN, reason="x" * 500)
    assert len(m.last_request.json()["reason"]) == 200


def test_close_404_returns_false(requests_mock, caplog):
    requests_mock.post(_close_url(), status_code=404, json={"error": "no such draft"})
    with caplog.at_level(logging.WARNING):
        assert ingest.close_set_draft(5, BASE, TOKEN) is False
    assert any("HTTP 404" in r.message for r in caplog.records)


def test_close_network_error_returns_false(requests_mock):
    requests_mock.post(_close_url(), exc=requests.Timeout("slow"))
    assert ingest.close_set_draft(5, BASE, TOKEN) is False


# ---- screenshot (unchanged behaviour, pinned) -------------------------------

def test_screenshot_sends_draft_id_form_field(requests_mock, tmp_path):
    png = tmp_path / "r.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    m = requests_mock.post(f"{BASE}/api/ingest/screenshot", json={"draft_id": 3, "game_index": 1})
    ingest.post_results_screenshot(str(png), BASE, TOKEN, roster=["1", "2"], draft_id=3)
    body = m.last_request.body
    text = body.decode("latin-1") if isinstance(body, bytes) else str(body)
    assert 'name="draft_id"' in text and "\r\n3\r\n" in text
    assert 'name="roster"' in text

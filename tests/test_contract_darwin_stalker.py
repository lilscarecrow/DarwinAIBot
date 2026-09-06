"""
Cross-repo contract test: the REAL game/ingest.py against a REAL darwin-stalker.

Skipped unless both env vars are set:

    DS_BASE_URL=http://127.0.0.1:3111 DS_INGEST_TOKEN=<token> pytest tests/test_contract_darwin_stalker.py -v

Point it ONLY at a local darwin-stalker on a scratch database (see the server
repo's verify recipe; mint a token with `darwin-stalker ingest-token new <label>`).
It creates and closes real drafts — never run it against darwinstalker.com.
This is the test that would have caught the `player_names`/`players` mismatch.
"""
import os
import struct
import zlib

import pytest
import requests

from game import ingest

BASE = os.environ.get("DS_BASE_URL")
TOKEN = os.environ.get("DS_INGEST_TOKEN")
PLATFORM = os.environ.get("DS_INGEST_PLATFORM", "pc")

pytestmark = pytest.mark.skipif(
    not (BASE and TOKEN), reason="set DS_BASE_URL and DS_INGEST_TOKEN to run against a local darwin-stalker"
)


def _live_drafts():
    r = requests.get(f"{BASE.rstrip('/')}/api/live", params={"platform": PLATFORM}, timeout=10)
    r.raise_for_status()
    return r.json().get("drafts") or []


def _draft_with_channel(drafts, channel):
    return [d for d in drafts if d.get("twitch_channel") == channel]


def _tiny_png(path, w=200, h=100):
    """A valid, boring PNG (Pillow not required)."""
    raw = b"".join(b"\x00" + b"\x20\x20\x20" * w for _ in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def test_open_roster_close_round_trip():
    channel = "contracttest"
    draft_id = ingest.open_set_draft([], BASE, TOKEN, platform=PLATFORM, twitch_channel=channel)
    assert isinstance(draft_id, int), "open-draft with an empty roster must succeed"
    try:
        mine = _draft_with_channel(_live_drafts(), channel)
        assert mine, "a channel-bearing bot draft must appear on /api/live even with zero players"
        assert mine[0]["players"] == []
        assert mine[0]["n_games"] == 0

        again = ingest.open_set_draft(["Alpha", "Bravo"], BASE, TOKEN, platform=PLATFORM,
                                      twitch_channel=channel, draft_id=draft_id)
        assert again == draft_id, "re-opening with draft_id must target the same draft"
        mine = _draft_with_channel(_live_drafts(), channel)
        names = sorted(p["player"] for p in mine[0]["players"])
        assert names == ["Alpha", "Bravo"]
    finally:
        assert ingest.close_set_draft(draft_id, BASE, TOKEN, reason="contract test") is True
    assert not _draft_with_channel(_live_drafts(), channel), "closed empty draft must leave the live list"
    # Idempotent: closing again is a harmless 200.
    assert ingest.close_set_draft(draft_id, BASE, TOKEN, reason="again") is True


def test_screenshot_then_close_keeps_draft_for_review(tmp_path):
    channel = "contracttest2"
    draft_id = ingest.open_set_draft(["Charlie"], BASE, TOKEN, platform=PLATFORM, twitch_channel=channel)
    assert isinstance(draft_id, int)
    png = tmp_path / "results.png"
    _tiny_png(str(png))
    # Uploads a screenshot into the draft; OCR of a blank image yields no rows,
    # but the stored asset means the server must NOT discard the draft on close.
    ingest.post_results_screenshot(str(png), BASE, TOKEN, platform=PLATFORM, draft_id=draft_id)
    assert ingest.close_set_draft(draft_id, BASE, TOKEN, reason="contract test") is True
    # The embed must be gone either way (channel cleared or draft discarded).
    assert not _draft_with_channel(_live_drafts(), channel)

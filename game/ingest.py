"""
Client for the ds.xdos.ai scrim ladder ingestion API.
See SHOW_DIRECTOR_HANDOFF.md for the full API contract.
"""
import json
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
_OPEN_DRAFT_TIMEOUT_SECONDS = 5


def post_results_screenshot(
    screenshot_path: str,
    base_url: str,
    token: str,
    platform: str = "pc",
    roster: Optional[list[str]] = None,
    draft_id: Optional[int] = None,
) -> None:
    """
    POST the raw end-of-match results screenshot to /api/ingest/screenshot.

    Everything sent lands in an unpublished draft for a human moderator to verify —
    this call is fire-and-forget from our side: failures are logged and swallowed
    rather than retried.

    roster: optional list of Discord ID strings for players known to be in this
    match (collected from scrim signup reactions). When supplied, the server
    narrows its OCR prompt and fuzzy-name candidate pool to those players. Sent
    as a JSON-encoded string in the multipart form so the field is omitted
    entirely when roster is None/empty — old servers ignore the unknown field,
    new servers treat a missing field as "no roster known".

    draft_id: optional draft id from a prior open_set_draft() call. When
    supplied, sent as a form field so the server targets that existing draft
    instead of creating a new one. Omitted entirely when None.
    """
    url = f"{base_url.rstrip('/')}/api/ingest/screenshot"
    headers = {"Authorization": f"Bearer {token}"}
    data = {"platform": platform}
    if roster:
        data["roster"] = json.dumps(roster)
    if draft_id is not None:
        data["draft_id"] = draft_id

    try:
        with open(screenshot_path, "rb") as f:
            files = {"screenshot": (os.path.basename(screenshot_path), f, "image/png")}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("ds.xdos.ai ingest request failed: %s", e)
        return

    if resp.status_code == 200:
        body = resp.json()
        logger.info(
            "ds.xdos.ai ingest ok: draft_id=%s game_index=%s ocr_error=%s",
            body.get("draft_id"), body.get("game_index"), body.get("ocr_error"),
        )
    else:
        logger.warning("ds.xdos.ai ingest failed: HTTP %d — %s", resp.status_code, resp.text[:300])


def open_set_draft(
    player_names: list[str],
    base_url: str,
    token: str,
    platform: str = "pc",
    twitch_channel: Optional[str] = None,
) -> Optional[int]:
    """
    POST to /api/ingest/open-draft to pre-open a draft for the upcoming match.

    Called before the match starts (before the B press) so the server has a
    draft ready to receive the end-of-match screenshot via draft_id. Like
    post_results_screenshot, this is fire-and-forget: failures are logged and
    swallowed rather than retried, and never raise.

    player_names: OCR'd lobby nameplates. Blank/whitespace-only entries are
    filtered out before sending. If nothing remains after filtering, the POST
    is skipped entirely and None is returned.

    twitch_channel: optional Twitch channel name for the director's stream.
    Included in the JSON payload only when set — omitted (not sent as null)
    otherwise, mirroring the roster omit-when-None pattern above.

    Returns the draft_id from the response on success, None on any failure.
    """
    names = [n for n in player_names if n.strip()]
    if not names:
        return None

    url = f"{base_url.rstrip('/')}/api/ingest/open-draft"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"platform": platform, "player_names": names}
    if twitch_channel:
        payload["twitch_channel"] = twitch_channel

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_OPEN_DRAFT_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("ds.xdos.ai open-draft request failed: %s", e)
        return None

    if resp.status_code == 200:
        body = resp.json()
        draft_id = body.get("draft_id")
        logger.info("ds.xdos.ai open-draft ok: draft_id=%s", draft_id)
        return draft_id
    else:
        logger.warning("ds.xdos.ai open-draft failed: HTTP %d — %s", resp.status_code, resp.text[:300])
        return None

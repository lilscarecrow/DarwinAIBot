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


def post_results_screenshot(
    screenshot_path: str,
    base_url: str,
    token: str,
    platform: str = "pc",
    roster: Optional[list[str]] = None,
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
    """
    url = f"{base_url.rstrip('/')}/api/ingest/screenshot"
    headers = {"Authorization": f"Bearer {token}"}
    data = {"platform": platform}
    if roster:
        data["roster"] = json.dumps(roster)

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

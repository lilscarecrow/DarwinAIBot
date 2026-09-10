"""
Client for the darwinstalker.com scrim ladder ingestion API.

Five calls, all fire-and-forget: they log and swallow every failure, never
retry, and never raise. The bot must keep running a match even if the ladder
is unreachable. The full contract (shapes, status codes, what the LIVE tab
shows when) is in docs/DS_LIFECYCLE_HANDOFF.md.

Only game/ds_lifecycle.py should call these directly — the cog and the match
runner go through DraftLifecycle so the draft id is tracked in one place.
"""
import json
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
_OPEN_DRAFT_TIMEOUT_SECONDS = 5
_CLOSE_DRAFT_TIMEOUT_SECONDS = 5
_RELAY_TIMEOUT_SECONDS = 5

# The two relay calls log through the relay's logger, which DsLogHandler
# ignores: a failing log POST must not warn its way into another log POST.
_relay_logger = logging.getLogger("game.ds_relay.ingest")


def post_results_screenshot(
    screenshot_path: str,
    base_url: str,
    token: str,
    platform: str = "pc",
    roster: Optional[list[str]] = None,
    draft_id: Optional[int] = None,
) -> Optional[dict]:
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

    Returns the server's parsed JSON body on a 200 (draft_id, game_index,
    ocr_error, and — when ocr_error is None — placements: [{rank, name,
    player_id}, ...] in finish order, used to resolve the "who wins" Twitch
    prediction), or None on any failure. Still fire-and-forget in spirit: a
    None return is never retried, just means the caller (DraftLifecycle.post_results)
    has nothing to resolve the prediction with this game.
    """
    url = f"{base_url.rstrip('/')}/api/ingest/screenshot"
    headers = {"Authorization": f"Bearer {token}"}
    data = {"platform": platform}
    if roster:
        data["roster"] = json.dumps(roster_ids(roster))   # the screenshot endpoint takes ids only
    if draft_id is not None:
        data["draft_id"] = draft_id

    try:
        with open(screenshot_path, "rb") as f:
            files = {"screenshot": (os.path.basename(screenshot_path), f, "image/png")}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=_TIMEOUT_SECONDS)

        if resp.status_code == 200:
            body = resp.json()
            logger.info(
                "darwinstalker ingest ok: draft_id=%s game_index=%s ocr_error=%s",
                body.get("draft_id"), body.get("game_index"), body.get("ocr_error"),
            )
            return body if isinstance(body, dict) else None
        else:
            logger.warning("darwinstalker ingest failed: HTTP %d — %s", resp.status_code, resp.text[:300])
            return None
    except Exception as e:
        logger.warning("darwinstalker ingest request failed: %s", e)
        return None


def roster_entries(roster: Optional[list]) -> list:
    """Normalize a roster for the wire: ids stay strings, `{"id", "names"}`
    dicts keep their (non-empty, deduplicated) names. Blank ids are dropped."""
    out = []
    for r in roster or []:
        if isinstance(r, dict):
            rid = str(r.get("id") or "").strip()
            if not rid:
                continue
            names = []
            for n in [r.get("name")] + list(r.get("names") or []):
                n = str(n).strip() if n else ""
                if n and n not in names:
                    names.append(n)
            out.append({"id": rid, "names": names} if names else rid)
        else:
            rid = str(r).strip()
            if rid:
                out.append(rid)
    return out


def roster_ids(roster: Optional[list]) -> list[str]:
    """Just the ids of a roster in either shape."""
    return [e["id"] if isinstance(e, dict) else e for e in roster_entries(roster)]


def open_set_draft(
    player_names: list[str],
    base_url: str,
    token: str,
    platform: str = "pc",
    twitch_channel: Optional[str] = None,
    draft_id: Optional[int] = None,
    roster: Optional[list] = None,
    tournament_slug: Optional[str] = None,
) -> Optional[dict]:
    """
    POST to /api/ingest/open-draft: open (or refresh) the draft for this lobby.

    Returns the server's reply as a dict on success (`draft_id`, `created`,
    `rows`, `roster_resolved`, and since 2026-09-07 `lobby` — each known
    member with every name they may appear as — `expected_names` and
    `unlinked`), None on any failure. Fire-and-forget: never raises.

    tournament_slug: the ladder tournament this lobby belongs to (config
    ds_ingest_tournament_slug, only while tournament_mode is on). Sent only
    when truthy; the server answers 400 "unknown tournament" for a slug it
    does not know, which lands in the log like any other open failure.

    roster: the lobby's Discord members (the scrim signup reactors), each a
    bare id string or `{"id": ..., "names": [nick, display name, username]}`.
    The server pre-seeds every id that is linked to a player on the ladder
    with that player's canonical name, links an unlinked id inline when one
    of its names matches exactly one of the draft's players, and lists the
    rest under `unlinked`. Sent as "roster" only when non-empty, capped at 20.

    ALWAYS sends the request when called — an empty roster is a legitimate
    "lobby forming" open, sent as `"players": []`. (Earlier versions skipped the
    POST silently when OCR returned no names, which meant the draft never
    existed until the first results screenshot and the Twitch embed never lit.)
    Deciding whether to call at all is DraftLifecycle's job, not this function's.

    player_names: lobby nameplates from OCR (blank entries are dropped) or [].
    twitch_channel: the director's Twitch channel — included only when truthy,
        omitted (never sent as null) otherwise.
    draft_id: the draft opened earlier this session, so the server refreshes
        THAT draft (adds names, re-sets the channel) instead of creating
        another. Omitted when None; the server then reuses this token's most
        recent open draft or creates a fresh one.

    """
    names = [n.strip() for n in player_names if n and n.strip()]

    url = f"{base_url.rstrip('/')}/api/ingest/open-draft"
    headers = {"Authorization": f"Bearer {token}"}
    payload: dict = {"platform": platform, "players": names}
    if twitch_channel:
        payload["twitch_channel"] = twitch_channel
    if draft_id is not None:
        payload["draft_id"] = draft_id
    entries = roster_entries(roster)
    if entries:
        payload["roster"] = entries[:20]
    if tournament_slug:
        payload["tournament_slug"] = str(tournament_slug).strip()

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_OPEN_DRAFT_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            body = resp.json()
            if not isinstance(body, dict) or body.get("draft_id") is None:
                logger.warning("darwinstalker open-draft: reply without draft_id: %s", str(body)[:200])
                return None
            logger.info(
                "darwinstalker open-draft ok: draft_id=%s created=%s rows=%s known=%s unlinked=%s twitch_channel=%s",
                body.get("draft_id"), body.get("created"), body.get("rows"),
                len(body.get("lobby") or []), len(body.get("unlinked") or []), twitch_channel or "(none)",
            )
            return body
        logger.warning(
            "darwinstalker open-draft failed: HTTP %d — %s", resp.status_code, resp.text[:300]
        )
        return None
    except Exception as e:
        logger.warning("darwinstalker open-draft request failed: %s", e)
        return None


def close_set_draft(
    draft_id: int,
    base_url: str,
    token: str,
    reason: str = "",
) -> bool:
    """
    POST to /api/ingest/close-draft: tell the ladder this lobby is over.

    The server discards the draft if it holds no game data and no screenshot
    (`discarded: true`), otherwise keeps it for moderator review and just clears
    the Twitch channel so the LIVE embed drops (`discarded: false`). Closing a
    draft that is already published/rejected is harmless (`closed: false`).

    Returns True when the server answered 200, False otherwise. Fire-and-forget:
    never raises. A False here needs no retry — the server's hourly sweep
    discards abandoned empty drafts and clears stale channels on its own.
    """
    url = f"{base_url.rstrip('/')}/api/ingest/close-draft"
    headers = {"Authorization": f"Bearer {token}"}
    payload: dict = {"draft_id": draft_id}
    if reason:
        payload["reason"] = reason[:200]

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_CLOSE_DRAFT_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("closed"):
                logger.info(
                    "darwinstalker close-draft ok: draft_id=%s discarded=%s reason=%s",
                    body.get("draft_id"), body.get("discarded"), reason or "(none)",
                )
            else:
                logger.info(
                    "darwinstalker close-draft: draft_id=%s already %s — nothing to do",
                    body.get("draft_id"), body.get("status"),
                )
            return True
        logger.warning(
            "darwinstalker close-draft failed: HTTP %d — %s", resp.status_code, resp.text[:300]
        )
        return False
    except Exception as e:
        logger.warning("darwinstalker close-draft request failed: %s", e)
        return False


def post_events(
    draft_id: Optional[int],
    game_index: Optional[int],
    events: list[dict],
    base_url: str,
    token: str,
) -> bool:
    """
    POST a batch (≤100) of live match events to /api/ingest/events.

    Each event: {"kind", "elapsed_ms"?, "at"?, "slot"?, "player"?, "data"?}
    (see docs/DS_LIFECYCLE_HANDOFF.md "Live match events" for the kinds).
    draft_id / game_index are omitted when None (the server then targets this
    token's open draft / the next game). Called only by DsRelay's thread.
    Returns True on 200. Never raises.
    """
    url = f"{base_url.rstrip('/')}/api/ingest/events"
    headers = {"Authorization": f"Bearer {token}"}
    payload: dict = {"events": list(events)[:100]}
    if draft_id is not None:
        payload["draft_id"] = draft_id
    if game_index is not None:
        payload["game_index"] = game_index
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_RELAY_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            return True
        _relay_logger.warning(
            "darwinstalker events failed: HTTP %d — %s", resp.status_code, resp.text[:300]
        )
        return False
    except Exception as e:
        _relay_logger.warning("darwinstalker events request failed: %s", e)
        return False


def post_log(entries: list[dict], base_url: str, token: str) -> bool:
    """
    POST a batch (≤50) of the bot's own log lines to /api/ingest/log. They show
    up on the ladder's /admin/observability as `bot.<kind>` events.

    Each entry: {"level": "info"|"warn"|"error", "kind", "message" (≤500), "at"?, "fields"?}.
    Called only by DsRelay's thread. Returns True on 200. Never raises.
    """
    url = f"{base_url.rstrip('/')}/api/ingest/log"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"entries": list(entries)[:50]}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_RELAY_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            return True
        _relay_logger.warning(
            "darwinstalker log relay failed: HTTP %d — %s", resp.status_code, resp.text[:300]
        )
        return False
    except Exception as e:
        _relay_logger.warning("darwinstalker log relay request failed: %s", e)
        return False

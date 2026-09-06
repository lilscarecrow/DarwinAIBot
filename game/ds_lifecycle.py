"""
DraftLifecycle — the one place the bot talks to the darwinstalker.com ladder.

A "draft" on the ladder is the unpublished set for one lobby: it is opened when
the lobby is created, gains a roster when the first match starts, gains a game
each time a results screenshot is uploaded, and is closed when the session ends.
The cog and the match runner call the four methods below and nothing else; the
draft id lives here so every call targets the same draft.

Pure Python on purpose: no Discord, no screen, no game. The HTTP calls come in
through `transport` (defaults to game.ingest) so tests inject a fake. Every
method is synchronous and blocking (it does an HTTP round-trip) — call it from
an executor, never directly on the event loop. Nothing in here ever raises:
the ladder is best-effort and must never stop a match.

Contract and verification steps: docs/DS_LIFECYCLE_HANDOFF.md.
"""
import logging
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://darwinstalker.com"


def _default_transport():
    from game import ingest

    return SimpleNamespace(
        open_set_draft=ingest.open_set_draft,
        close_set_draft=ingest.close_set_draft,
        post_results_screenshot=ingest.post_results_screenshot,
    )


class DraftLifecycle:
    def __init__(self, config: dict, transport=None):
        self._config = config or {}
        self._transport = transport if transport is not None else _default_transport()
        self._draft_id: Optional[int] = None

    # ---- config -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Ingest is on iff a bearer token is configured."""
        return bool(self._config.get("ds_ingest_token"))

    @property
    def draft_id(self) -> Optional[int]:
        return self._draft_id

    def _args(self) -> dict:
        return {
            "base_url": self._config.get("ds_ingest_base_url") or _DEFAULT_BASE_URL,
            "token": self._config.get("ds_ingest_token"),
        }

    def _platform(self) -> str:
        return self._config.get("ds_ingest_platform") or "pc"

    def _twitch_channel(self) -> Optional[str]:
        ch = self._config.get("ds_ingest_twitch_channel")
        ch = ch.strip() if isinstance(ch, str) else None
        return ch or None

    # ---- lifecycle ----------------------------------------------------------

    def open_lobby(self, names: Optional[list[str]] = None) -> Optional[int]:
        """Lobby created (/custom succeeded): open the draft now, roster or not.

        This is what lights the Twitch embed on the LIVE tab while the lobby is
        still filling. Always replaces any remembered draft id with the one the
        server returns — a new lobby is a new draft.
        """
        if not self.enabled:
            logger.debug("ds ingest disabled (no ds_ingest_token) — not opening a draft")
            return None
        try:
            new_id = self._transport.open_set_draft(
                list(names or []),
                platform=self._platform(),
                twitch_channel=self._twitch_channel(),
                **self._args(),
            )
        except Exception as e:  # transport promised not to raise; belt and braces
            logger.warning("ds open_lobby: transport raised %s", e)
            new_id = None
        if new_id is None:
            logger.warning(
                "ds open_lobby: could not open a draft (see the open-draft line above); "
                "the results screenshot will create one later but the Twitch embed stays dark"
            )
            return None
        self._draft_id = new_id
        if not self._twitch_channel():
            logger.warning(
                "ds open_lobby: draft %s opened WITHOUT a twitch_channel — set "
                "ds_ingest_twitch_channel in config.json and restart if you want the embed",
                new_id,
            )
        return new_id

    def on_match_start(self, names: list[str]) -> Optional[int]:
        """Match about to start: push the OCR'd lobby roster onto the open draft.

        No names (OCR failed) is NOT an error and NOT silent: the draft keeps
        whatever roster it has and a WARNING says so. If no draft is open yet
        (auto-start path, or open_lobby failed) one is opened now regardless.
        """
        if not self.enabled:
            logger.debug("ds ingest disabled (no ds_ingest_token) — skipping match-start roster")
            return None
        clean = [n.strip() for n in (names or []) if n and n.strip()]
        if self._draft_id is None:
            logger.info("ds on_match_start: no draft open yet — opening one now (%d names)", len(clean))
            return self.open_lobby(clean)
        if not clean:
            logger.warning(
                "ds on_match_start: lobby OCR returned no names; draft %s keeps its current roster",
                self._draft_id,
            )
            return self._draft_id
        try:
            new_id = self._transport.open_set_draft(
                clean,
                platform=self._platform(),
                twitch_channel=self._twitch_channel(),
                draft_id=self._draft_id,
                **self._args(),
            )
        except Exception as e:
            logger.warning("ds on_match_start: transport raised %s", e)
            new_id = None
        if new_id is None:
            logger.warning(
                "ds on_match_start: roster push failed; keeping draft %s", self._draft_id
            )
            return self._draft_id
        if new_id != self._draft_id:
            logger.info("ds on_match_start: server moved us from draft %s to %s", self._draft_id, new_id)
        self._draft_id = new_id
        return new_id

    def post_results(self, png_path: str, roster: Optional[list[str]] = None) -> None:
        """Match over: upload the results screenshot into the open draft."""
        if not self.enabled:
            logger.debug("ds ingest disabled (no ds_ingest_token) — not uploading results")
            return
        try:
            self._transport.post_results_screenshot(
                png_path,
                platform=self._platform(),
                roster=roster,
                draft_id=self._draft_id,
                **self._args(),
            )
        except Exception as e:
            logger.warning("ds post_results: transport raised %s", e)

    def close(self, reason: str = "session reset") -> bool:
        """Session over (/quit, abort, idle timeout, error reset): close the draft.

        The server discards it if it is still empty, otherwise keeps the games
        for a moderator and drops the Twitch channel. The remembered id is
        cleared whether or not the call succeeded: a close that never reached
        the server is left to the server's hourly sweep (empty bot drafts are
        auto-discarded, stale channels cleared), and the next lobby must start
        from a fresh draft either way.
        """
        if self._draft_id is None:
            logger.debug("ds close: no draft open — nothing to close")
            return False
        draft_id, self._draft_id = self._draft_id, None
        if not self.enabled:
            logger.debug("ds ingest disabled (no ds_ingest_token) — not closing draft %s", draft_id)
            return False
        try:
            ok = bool(self._transport.close_set_draft(draft_id, reason=reason, **self._args()))
        except Exception as e:
            logger.warning("ds close: transport raised %s", e)
            ok = False
        if not ok:
            logger.warning(
                "ds close: draft %s not closed (reason=%s); the server sweep will clean it up",
                draft_id, reason,
            )
        return ok

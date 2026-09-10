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
import threading
import time
from types import SimpleNamespace
from typing import Optional

from game.ds_relay import DsRelay
from game.name_snap import NameSnapper

logger = logging.getLogger(__name__)

# Event fields that ride at the top level of an event; anything else event()
# receives goes under "data".
_EVENT_TOP_LEVEL = ("elapsed_ms", "slot", "player")

_DEFAULT_BASE_URL = "https://darwinstalker.com"


def _default_transport():
    from game import ingest

    return SimpleNamespace(
        open_set_draft=ingest.open_set_draft,
        close_set_draft=ingest.close_set_draft,
        post_results_screenshot=ingest.post_results_screenshot,
        post_events=ingest.post_events,
        post_log=ingest.post_log,
    )


def _normalize_roster(roster) -> list:
    """Roster members as sent on the wire: `"id"` or `{"id", "names"}`.
    Accepts ids, dicts, or objects with .id/.name/.global_name/.nick."""
    out = []
    for r in roster or []:
        if isinstance(r, dict):
            rid = str(r.get("id") or "").strip()
            names = [str(n).strip() for n in ([r.get("name")] + list(r.get("names") or [])) if n and str(n).strip()]
        elif hasattr(r, "id"):
            rid = str(getattr(r, "id") or "").strip()
            names = [str(n).strip() for n in (getattr(r, "nick", None), getattr(r, "global_name", None), getattr(r, "name", None)) if n and str(n).strip()]
        else:
            rid, names = str(r).strip(), []
        if not rid:
            continue
        dedup = []
        for n in names:
            if n not in dedup:
                dedup.append(n)
        out.append({"id": rid, "names": dedup} if dedup else rid)
    return out


def _roster_ids(roster) -> list[str]:
    return [e["id"] if isinstance(e, dict) else str(e) for e in _normalize_roster(roster)]


class DraftLifecycle:
    def __init__(self, config: dict, transport=None):
        self._config = config or {}
        self._transport = transport if transport is not None else _default_transport()
        self._draft_id: Optional[int] = None
        # The lobby's Discord IDs from open_lobby, re-sent at match start so the
        # server's fuzzy pool for OCR names is this lobby, not the whole season.
        self._roster: list = []
        # What the ladder told us about this lobby on the last open-draft
        # reply: each known member with every name they may appear as
        # (`lobby`), the flat OCR snap set (`expected_names`), and the ids
        # nobody is linked to (`unlinked`, with the Discord names we sent).
        self._lobby: list[dict] = []
        self._expected_names: list[str] = []
        self._unlinked: list[dict] = []
        # The draft's own rows as the ladder last reported them: open-draft's
        # `draft_players` (every row with every handle its identity may
        # appear as) and the screenshot reply's `placements` (the scorecard
        # names game N was filed under). This is the anchoring truth the
        # player-bar OCR snaps to once a results screen has been read — and
        # the ONLY truth for a player nobody's Discord is linked to, who is
        # never in `lobby` (2026-09-10, draft 287: the bar read "Tou ——",
        # "thuaz", "FrozenNO" for the scorecard's "Lou", "thugz", "FrozenNQ"
        # every match, and the ladder showed 14 rows for a 10-player lobby).
        self._draft_players: list[dict] = []
        self._snapper = NameSnapper(None)
        # 1-based game the next events belong to: 1 when a draft is first
        # opened, +1 after every results upload, 0 when no lobby is open.
        # /custom runs once per GAME and the server keeps every lobby of the
        # session on the same draft, so open_lobby only restarts the count
        # when the server hands back a different draft (see open_lobby).
        self._game_index: int = 0
        # Background sender for live events + relayed log lines. Created here,
        # started on first use (only when enabled). Shares an injected transport
        # when it offers post_events/post_log, so tests see every call.
        relay_transport = (
            self._transport
            if hasattr(self._transport, "post_events") and hasattr(self._transport, "post_log")
            else None
        )
        self._relay = DsRelay(
            get_draft_id=lambda: self._draft_id,
            get_args=self._args,
            transport=relay_transport,
        )

    # ---- config -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Ingest is on iff a bearer token is configured."""
        return bool(self._config.get("ds_ingest_token"))

    @property
    def roster_size(self) -> int:
        """Signup reactors known for this lobby (0 when none) — the V2 card
        detector's expected-count hint."""
        return len(self._roster)

    @property
    def draft_id(self) -> Optional[int]:
        return self._draft_id

    @property
    def lobby(self) -> list[dict]:
        return list(self._lobby)

    @property
    def expected_names(self) -> list[str]:
        return list(self._expected_names)

    @property
    def unlinked(self) -> list[dict]:
        """Roster members the ladder has no link for: [{discord_id, names}]."""
        return list(self._unlinked)

    @property
    def draft_players(self) -> list[dict]:
        """The draft's rows as the ladder last reported them:
        [{player, player_id, games, names}] (see __init__)."""
        return list(self._draft_players)

    @property
    def known_players(self) -> set[str]:
        """Every name the snapper can resolve a read TO: the lobby's linked
        members plus the draft's own rows. What a slot's `linked` flag means."""
        out = {str(m.get("player") or "").strip() for m in self._lobby}
        out |= {str(m.get("player") or "").strip() for m in self._draft_players}
        out.discard("")
        return out

    def _rebuild_snapper(self) -> None:
        self._snapper = NameSnapper(self._lobby + self._draft_players)

    def _merge_draft_players(self, entries: list[dict]) -> None:
        """Fold `entries` ({player, names?, player_id?, games?}) into the
        draft-row snap set, keyed by player name (case-insensitive); names
        union. Then rebuild the snapper."""
        for e in entries:
            if not isinstance(e, dict):
                continue
            player = str(e.get("player") or "").strip()
            if not player:
                continue
            names = [str(n).strip() for n in (e.get("names") or []) if n and str(n).strip()]
            if player not in names:
                names.insert(0, player)
            cur = next((d for d in self._draft_players if d["player"].lower() == player.lower()), None)
            if cur is None:
                self._draft_players.append({
                    "player": player,
                    "player_id": e.get("player_id"),
                    "games": e.get("games"),
                    "names": names,
                })
            else:
                for n in names:
                    if n not in cur["names"]:
                        cur["names"].append(n)
                if e.get("player_id") is not None:
                    cur["player_id"] = e.get("player_id")
                if e.get("games") is not None:
                    cur["games"] = e.get("games")
        self._rebuild_snapper()

    def claim_nudge(self) -> Optional[dict]:
        """What to tell the roster members the ladder cannot place, or None
        when everyone is known: `mentions` (space-joined <@id> pings, ≤10)
        and `text` (where to claim). The only thing that grows Steam↔Discord
        coverage is the player claiming their handle, so this has to land
        where the PLAYERS read — the lobby-code ping — not only in the
        director's channel.
        """
        ids = [str(u.get("discord_id")) for u in self._unlinked[:10] if u.get("discord_id")]
        if not ids:
            return None
        site = (self._config.get("ds_ingest_base_url") or "https://darwinstalker.com").rstrip("/")
        return {
            "mentions": " ".join(f"<@{i}>" for i in ids),
            "text": (
                f"Sign in with Steam at {site} and claim your handle "
                "(or link Discord on your profile) so your results land on your profile."
            ),
        }

    def snap_names(self, reads: list[str]) -> list[str]:
        """Player-bar OCR reads → the ladder's names for this lobby where one
        player matches unambiguously (game/name_snap.py): the linked members'
        handles (`lobby`) and the draft's own rows (`draft_players`, fed by
        every open-draft reply and every results upload); everything else
        verbatim."""
        try:
            return self._snapper.snap_all(list(reads))
        except Exception as e:
            logger.debug("name snap failed: %s", e)
            return list(reads)

    def _take_reply(self, result) -> Optional[int]:
        """open_set_draft's result → draft id; remember the lobby view if the
        reply carried one. Accepts a bare id (older transports, tests)."""
        if result is None:
            return None
        if isinstance(result, dict):
            draft_id = result.get("draft_id")
            if draft_id is None:
                return None
            if "lobby" in result or "unlinked" in result:
                self._lobby = [m for m in (result.get("lobby") or []) if isinstance(m, dict)]
                self._expected_names = [str(n) for n in (result.get("expected_names") or [])]
                self._unlinked = [u for u in (result.get("unlinked") or []) if isinstance(u, dict)]
                self._rebuild_snapper()
                logger.info(
                    "ds lobby: %d known player(s), %d expected name(s), %d unlinked",
                    len(self._lobby), len(self._expected_names), len(self._unlinked),
                )
            if isinstance(result.get("draft_players"), list):
                # The server's view of the rows REPLACES ours: a row it pruned
                # (a shell game 1 did not report) must stop being a snap target.
                self._draft_players = []
                self._merge_draft_players(result["draft_players"])
                logger.info("ds draft: %d row(s) on the draft to snap nameplates to", len(self._draft_players))
            skipped = result.get("skipped_unresolved") or []
            if skipped:
                logger.warning(
                    "ds open-draft: %d nameplate read(s) matched no scorecard row and were NOT added: %s",
                    len(skipped), ", ".join(str(n) for n in skipped),
                )
            return int(draft_id)
        return int(result)

    @property
    def game_index(self) -> int:
        return self._game_index

    @property
    def relay(self) -> DsRelay:
        return self._relay

    def _relay_started(self) -> DsRelay:
        self._relay.start()
        return self._relay

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

    # ---- live events + log relay --------------------------------------------

    def event(self, kind: str, **fields) -> None:
        """Queue one live match event for the current game (never blocks, never raises).

        `elapsed_ms`, `slot`, `player` ride at the top level; every other
        keyword lands under "data". Dropped when ingest is disabled or no
        draft is open at flush time (the relay counts those).
        """
        if not self.enabled:
            return
        try:
            ev: dict = {"kind": str(kind), "at": int(time.time())}
            data = {}
            for k, v in fields.items():
                if k in _EVENT_TOP_LEVEL:
                    if v is not None:
                        ev[k] = v
                else:
                    data[k] = v
            if data:
                ev["data"] = data
            self._relay_started().enqueue_event(self._game_index, ev)
        except Exception as e:
            logger.debug("ds event dropped: %s", e)

    def log(self, level: str, kind: str, message: str, fields: Optional[dict] = None) -> None:
        """Queue one of the bot's own log lines for the ladder's event stream."""
        if not self.enabled:
            return
        try:
            self._relay_started().enqueue_log(level, kind, message, fields)
        except Exception:
            pass

    # ---- lifecycle ----------------------------------------------------------

    def open_lobby(
        self,
        names: Optional[list[str]] = None,
        roster: Optional[list[str]] = None,
        tournament_slug: Optional[str] = None,
    ) -> Optional[int]:
        """Lobby created (/custom succeeded): open the draft now, roster or not.

        This is what lights the Twitch embed on the LIVE tab while the lobby is
        still filling. Always replaces any remembered draft id with the one the
        server returns. The server answers with this token's open draft when
        it has one — a set is one draft, its games are its lobbies — so the
        game counter restarts at 1 only when the id actually changes. (Until
        2026-09-07 every /custom reset it to 1, and the ladder showed a whole
        set as "Game 1 in progress" with three games' events folded together.)

        roster: the lobby's Discord IDs (signup reactors). The ladder pre-seeds
        the linked ones by canonical name, so the card fills in before any OCR
        runs; unlinked ids are skipped server-side.
        tournament_slug: the ladder tournament to tag the draft with (only
        while tournament mode is on); forwarded only when given.
        """
        if not self.enabled:
            logger.debug("ds ingest disabled (no ds_ingest_token) — not opening a draft")
            return None
        self._roster = _normalize_roster(roster)
        kwargs = {}
        if tournament_slug:
            kwargs["tournament_slug"] = str(tournament_slug).strip()
        try:
            new_id = self._take_reply(self._transport.open_set_draft(
                list(names or []),
                platform=self._platform(),
                twitch_channel=self._twitch_channel(),
                roster=list(self._roster),
                **kwargs,
                **self._args(),
            ))
        except Exception as e:  # transport promised not to raise; belt and braces
            logger.warning("ds open_lobby: transport raised %s", e)
            new_id = None
        if roster:
            logger.info("ds open_lobby: sent %d discord ids for roster pre-seed", len(roster))
        if new_id is None:
            logger.warning(
                "ds open_lobby: could not open a draft (see the open-draft line above); "
                "the results screenshot will create one later but the Twitch embed stays dark"
            )
            return None
        if new_id != self._draft_id:
            self._game_index = 1
        self._draft_id = new_id
        self._relay_started()
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
            new_id = self._take_reply(self._transport.open_set_draft(
                clean,
                platform=self._platform(),
                twitch_channel=self._twitch_channel(),
                draft_id=self._draft_id,
                roster=list(self._roster),
                **self._args(),
            ))
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

    def on_match_start_async(self, names: list[str]) -> None:
        """Fire-and-forget wrapper around on_match_start() for the match-start
        call site (game/match_runner.py), which runs on the match thread right
        before the B-press — the one place this roster push must never be
        allowed to block. Found live (2026-09-07): the plain, awaited call
        added a real network round-trip (up to _OPEN_DRAFT_TIMEOUT_SECONDS)
        between the match countdown and the actual game start, on every match.

        Same fire-and-forget convention as announce()/ds_ingest/OBS elsewhere
        in this codebase, just on a daemon thread instead of asyncio.ensure_future
        since MatchRunner isn't on the event loop. No return value — nothing at
        the call site used the draft id anyway (event() reads self._draft_id
        fresh via a closure at flush time, so a brief staleness window while
        this thread is still in flight is harmless)."""
        if not self.enabled:
            return
        threading.Thread(
            target=self.on_match_start,
            args=(names,),
            daemon=True,
            name="DsOnMatchStart",
        ).start()

    def post_results(self, png_path: str, roster: Optional[list[str]] = None) -> Optional[dict]:
        """Match over: upload the results screenshot into the open draft.

        Returns the server's response body (see ingest.post_results_screenshot's
        docstring — draft_id/game_index/ocr_error, plus placements when OCR
        succeeded) so the caller can resolve a "who wins" Twitch prediction
        against it, or None when ingest is disabled/failed/there's nothing to
        report. Still fire-and-forget in spirit — a None here is never retried.
        """
        if not self.enabled:
            logger.debug("ds ingest disabled (no ds_ingest_token) — not uploading results")
            return None
        # The game's last events (match_end, eliminations) must be on the draft
        # before the results move the counter to the next game.
        self._relay.flush(2.0)
        result = None
        try:
            result = self._transport.post_results_screenshot(
                png_path,
                platform=self._platform(),
                roster=_roster_ids(roster) if roster else roster,
                draft_id=self._draft_id,
                **self._args(),
            )
        except Exception as e:
            logger.warning("ds post_results: transport raised %s", e)
        self._game_index = (self._game_index or 1) + 1
        if isinstance(result, dict) and isinstance(result.get("placements"), list):
            # The scorecard's names are now the roster: the next match's
            # player-bar reads snap to THESE before anything else.
            self._merge_draft_players([
                {"player": p.get("name"), "player_id": p.get("player_id")}
                for p in result["placements"] if isinstance(p, dict)
            ])
        return result if isinstance(result, dict) else None

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
            self._game_index = 0
            return False
        # Land the lobby's last events (and any queued log lines) on the draft
        # BEFORE it is closed; afterwards there is no draft to attach them to.
        self._relay.flush(2.0)
        draft_id, self._draft_id = self._draft_id, None
        self._roster = []
        self._lobby, self._expected_names, self._unlinked = [], [], []
        self._draft_players = []
        self._snapper = NameSnapper(None)
        self._game_index = 0
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

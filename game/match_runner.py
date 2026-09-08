import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from zones.zone_logic import valid_closeable_zones, ZoneState
from zones.strategy_factory import get_strategy
from session.state import SessionState

logger = logging.getLogger(__name__)

# Cards that target a zone coordinate at runtime rather than a fixed drop_target
_ZONE_TARGETED_CARDS = frozenset({"lava_zone", "nuclear_blast", "open_zone", "spawn_electronic"})

# Cards that target a player card slot at the top of the screen
_PLAYER_TARGETED_CARDS = frozenset({"expose", "favorite_player", "give_leather", "give_wood",
                                     "man_hunt", "speed_boost", "warm_up"})

# Card tray layout, calibrated at 1920×1080 (the only resolution this bot supports —
# see "Future Enhancements: Multi-resolution support" in CLAUDE.md). Moved out of
# config.json (2026-09-07): these have proven stable across the UE5 update and every
# session since, and — unlike the pixel-region calibration data that's genuinely
# machine/version-specific — there is nothing here that would ever differ between
# two people running this bot at the one supported resolution.
_CARD_TRAY_CENTER_X = 966
_CARD_TRAY_CARD_Y = 943
_CARD_TRAY_CARD_WIDTH = 76

# Fixed drop target per "plain" card type (not zone-targeted, not player-targeted,
# not zone_close) — every one of these currently drops dead center of the screen.
# Kept as a per-card-type dict rather than one shared constant so a future card
# that needs a different target is just as easy to add here as it was in config.
_CARD_DROP_TARGETS: dict[str, tuple[int, int]] = {
    "electromania": (960, 540),
    "beach_party": (960, 540),
    "blood_moon": (960, 540),
    "anti_grav_storm": (960, 540),
    "telepathy": (960, 540),
}

# Zone-close static drop target — the game auto-picks the zone from here. Live path,
# calibrated at 1920×1080. Moved out of config.json (2026-09-07), same rationale as
# the card tray layout above.
_ZONE_CLOSE_DROP_TARGET = (1750, 1000)

# Director-points OCR crop — 2-digit numerator only (not the "/10"). Calibrated at
# 1920×1080. Moved out of config.json (2026-09-07).
_DIRECTOR_POINTS_REGION = (808, 1002, 20, 24)

# Director-points pip pixel-sampling calibration — dormant while director_points_use_pips
# (still a real config toggle) is false, kept for if pip reading is ever revisited.
# Moved out of config.json (2026-09-07).
_DIRECTOR_POINTS_PIPS = {"x_start": 862, "y": 1012, "spacing": 26, "count": 10}

# Zone map sample points and per-zone drop coordinates for the legacy per-zone
# selection path (_attempt_zone_close_legacy() / _attempt_zone_close_bypass() /
# _update_zone_states_from_screenshot()) — dormant, kept for reference/rollback, see
# those methods' docstrings. Moved out of config.json (2026-09-07): recalibrating this
# legacy path now means editing these dicts directly rather than running
# calibrate.py/calibrate_zone_colors.py, which still target config.json and are no
# longer wired to this data — acceptable since this path hasn't been needed since the
# static drop-area replaced it (2026-08-30).
_ZONE_MAP_SAMPLE_POINTS: dict[int, list[tuple[int, int]]] = {
    1: [(824, 231), (714, 341), (934, 341), (824, 451)],
    2: [(1081, 221), (971, 331), (1191, 331), (1081, 441)],
    3: [(716, 430), (606, 540), (826, 540), (716, 650)],
    4: [(959, 430), (849, 540), (1069, 540), (959, 650)],
    5: [(1178, 430), (1068, 540), (1288, 540), (1178, 650)],
    6: [(841, 622), (731, 732), (951, 732), (841, 842)],
    7: [(1084, 622), (974, 732), (1194, 732), (1084, 842)],
}
_ZONE_DROP_COORDINATES: dict[int, tuple[int, int]] = {
    1: (824, 341),
    2: (1081, 331),
    3: (716, 540),
    4: (959, 540),
    5: (1178, 540),
    6: (841, 732),
    7: (1084, 732),
}


@dataclass
class CardEvent:
    name: str
    card_type: str
    trigger_seconds: int
    play_time_seconds: int
    deck_position: Optional[int]
    drop_target: Optional[tuple[int, int]]
    points_cost: Optional[int] = None
    pending: bool = field(default=False, compare=False)
    done: bool = field(default=False, compare=False)


class MatchRunner:
    """
    Runs the full in-match automation sequence in a background thread.

    Call run() from run_in_executor. Call stop() from the Discord /end command
    to abort early — the match loop checks the stop flag between every action.
    """

    def __init__(
        self,
        config: dict,
        session: SessionState,
        on_action_update: Callable[[str, str], None],
        skip_start: bool = False,
        profile: Optional[dict] = None,
        draft_lifecycle=None,
    ):
        self._config = config
        # DraftLifecycle (game/ds_lifecycle.py) owned by the cog: the one place
        # that talks to the darwinstalker ladder. None = ingest not wired.
        self._ds = draft_lifecycle
        self._session = session
        self._on_action_update = on_action_update
        self._stop = threading.Event()
        self._skip_start = skip_start
        self._profile = profile
        self._bypass = config.get("ahk_bypass_mode", False)
        # Master toggle for the tray-pixel and zone-map verification/retry logic (kept
        # intact, not deleted) — card plays have proven reliable enough that the extra
        # screenshots and retries are no longer needed day-to-day. Distinct from
        # ahk_bypass_mode: that's a dry-run mode that never sends real input; this
        # still plays every card for real, it just trusts the play worked instead of
        # verifying it with a before/after pixel check.
        self._verify_plays = config.get("verify_card_plays", True)
        # Anti-cheat minimap cover — see _fire_card_event's sibling logic in run() for the
        # show, and the main loop below for the timed hide. One-shot flag so the hide
        # only fires once per match even though the loop polls elapsed time repeatedly.
        self._minimap_cover_source = config.get("obs_minimap_cover_source", "Map Cover")
        self._minimap_cover_seconds = config.get("obs_minimap_cover_seconds", 120)
        self._minimap_uncovered = False
        # Tournament mode (toggled via Discord's /tournament, config key tournament_mode):
        # the minimap cover stays up for the entire match instead of revealing at
        # _minimap_cover_seconds — see the main loop below, which skips the timed hide
        # entirely when this is set.
        self._tournament_mode = bool(config.get("tournament_mode", False))
        self._strategy = get_strategy(config.get("zone_selection_strategy", "weighted_outer"))
        self._zone_states: dict[int, str] = {i: ZoneState.OPEN for i in range(1, 8)}
        self._deck_played: set[int] = set()
        self._player_slot_xs: list[int] = []
        self._player_names: list[str] = []
        self._player_alive: list[bool] = []
        self._first_blood_logged: bool = False
        # V2 card detector state (game/player_cards_v2.py). In "v2" mode these
        # feed _player_slot_xs / _player_alive; in "shadow" mode they run
        # alongside V1 and only report (detector_v2 / eliminated_v2 events).
        self._v2_xs: list[int] = []
        self._v2_alive: list[bool] = []
        self._v2_names: list[str] = []
        # monotonic clock at match start; every live event carries elapsed_ms from it
        self._match_started_at: Optional[float] = None
        # Latest confirmed director-points reading — see _update_points_reading().
        # Sampled continuously throughout the match (main loop, every
        # screen_poll_interval_seconds) as well as reactively in _wait_for_points(),
        # so a card that needs to check affordability usually already has a recent
        # confirmed value instead of needing a fresh read at that exact moment.
        # Invalidated (set back to None) immediately after every card play attempt in
        # _fire_card_event(), since a real decrease is expected right then.
        self._last_confirmed_points: Optional[int] = None
        self._last_points_sample_time: float = 0.0
        from game.deck_utils import deck_layout_from_state
        live_layout = deck_layout_from_state()
        self._deck_layout: list[str] = live_layout if live_layout else config.get("deck_layout", [])
        if live_layout:
            logger.info("Deck layout loaded from state.json (%d cards)", len(live_layout))
        else:
            logger.warning("state.json unavailable — falling back to config deck_layout")

    def stop(self):
        """Signal the match loop to exit at the next checkpoint."""
        logger.info("MatchRunner stop requested")
        self._stop.set()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> str:
        """
        Full match sequence. Blocking — run via run_in_executor.
        Returns a Discord-formatted result string.
        """
        if self._stop.is_set():
            return "Match aborted before start."

        # Capture player roster from lobby nameplates before the match countdown begins.
        # The bar layout is identical in the lobby and in-match; this is the most stable
        # moment to read names and establish slot order.
        self._init_player_bar()

        # Push the OCR'd roster onto the ladder draft opened at /custom (or open
        # one now if none is). Empty OCR is logged, never silently skipped.
        # Fire-and-forget (see on_match_start_async's docstring) — this used to
        # be a plain awaited call and its network round-trip delayed the B-press
        # below on every match (found live 2026-09-07).
        if self._ds is not None:
            self._ds.on_match_start_async(self._player_names)
            self._ds.event("match_start", elapsed_ms=0, slots=[n or None for n in self._player_names])

        from game import tts

        if self._skip_start:
            # Game auto-started — B press is skipped but the 5s in-game countdown still runs
            self._update("Waiting for match countdown", "Starting card timers")
            tts.speak("Match is starting. Good luck.", broadcast=False)
            if self._stop.wait(5):
                return "Match aborted during countdown."
        else:
            # Press B to start the match
            self._update("Pressing B to start", "Waiting for countdown")
            self._press("b")
            # Announce immediately after B press — players are still in the lobby
            tts.speak("Match is starting. Good luck.", broadcast=False)

            # 5-second sync delay to align with in-game countdown
            if self._stop.wait(5):
                return "Match aborted during countdown."

        if self._profile is not None:
            _profile = self._profile
            logger.info("Profile: %s (pre-resolved at lobby creation)", _profile["display_name"])
        else:
            from game.profiles import resolve_profile
            _active_key = self._config.get("active_profile", "standard")
            _profile = resolve_profile(_active_key)
            if _active_key == "randomizer":
                logger.info("Profile selected by randomizer: %s", _profile["display_name"])
            else:
                logger.info("Profile: %s", _profile["display_name"])
        _first = min(_profile["card_plays"], key=lambda p: p["play_time_seconds"])
        _m, _s = divmod(_first["play_time_seconds"], 60)
        _first_label = f"{_first['card'].replace('_', ' ').title()} at {_m}:{_s:02d}"
        self._update("Match in progress", _first_label)
        start_time = time.monotonic()
        self._match_started_at = start_time

        card_schedule = self._build_card_schedule(_profile)
        phrases = self._build_tts_phrases(card_schedule)
        profile_announce = f"Using profile: {_profile['display_name']}"
        phrases.append(profile_announce)
        tts.precache_async(phrases)
        if not self._stop.wait(5):
            # Default the Director's camera to player 1's POV as soon as the match is
            # underway, so viewers see live gameplay immediately without needing a mod
            # to run /pov or !pov first. Same key as /pov's "1" choice.
            self._press("1")
            self._announce_card_lineup(profile_announce)
        poll_interval = self._config.get("screen_poll_interval_seconds", 12)

        # recording_enabled: false skips creating/starting the recorder entirely — no
        # local file gets written, and recording_path stays None throughout, so
        # discord_bot.py's `if recording_path:` upload check downstream naturally
        # never fires either. One toggle covers both "no local recording" and "no
        # upload attempt" since there's nothing to upload without a file.
        recorder = None
        if self._config.get("recording_enabled", True):
            from game.video_recorder import VideoRecorder
            recorder = VideoRecorder(self._config)
            recorder.start()
        recording_path = None

        try:
            # ------------------------------------------------------------------
            # Main match loop
            # ------------------------------------------------------------------
            while not self._stop.is_set():
                elapsed = time.monotonic() - start_time

                # Fire any card events whose time has arrived
                for event in card_schedule:
                    if not event.done and elapsed >= event.trigger_seconds:
                        self._fire_card_event(event, card_schedule)

                # Anti-cheat: uncover the minimap once the covered window has elapsed.
                # One-shot — self._minimap_uncovered stops this from firing again on
                # every subsequent loop iteration for the rest of the match. Skipped
                # entirely in tournament mode — the cover stays up the whole match.
                if not self._tournament_mode and not self._minimap_uncovered and elapsed >= self._minimap_cover_seconds:
                    from game import obs_control
                    if obs_control.is_enabled():
                        obs_control.set_source_visible(self._minimap_cover_source, False)
                    self._minimap_uncovered = True

                # Poll for match end (placement badge on screen)
                if self._match_has_ended():
                    logger.info("Match end detected at %.1fs elapsed", elapsed)
                    self._emit("match_end", elapsed_ms=int(elapsed * 1000))
                    break

                # Poll the player bar all match: first blood once, and every
                # elimination as a live event for the ladder's LIVE card.
                if self._player_slot_xs or self._v2_xs:
                    from game.screen_detection import take_screenshot as _take_ss
                    self._poll_player_bar(_take_ss())

                # Continuously sample director points throughout the match, not just
                # reactively when a card is about to fire — a small, steady cost (one
                # screenshot + OCR read every poll_interval) that means _wait_for_points()
                # usually already has a recent confirmed value to check against instead
                # of needing a fresh, possibly-failed read at the exact moment a card
                # needs to check affordability. Same ratchet-up guard as everywhere else
                # (see _update_points_reading) — a bad read here is disregarded, not
                # trusted, so this can only ever raise confidence, never lower it.
                now_ts = time.monotonic()
                if now_ts - self._last_points_sample_time >= poll_interval:
                    from game.screen_detection import take_screenshot as _take_ss2
                    sample = self._read_points(_take_ss2())
                    self._update_points_reading(sample, "background poll")
                    self._last_points_sample_time = now_ts

                # Sleep until the next card trigger, but no longer than poll_interval
                now = time.monotonic()
                now_elapsed = now - start_time
                pending_times = [
                    e.trigger_seconds - now_elapsed
                    for e in card_schedule if not e.done
                ]
                sleep_time = min(poll_interval, min(pending_times)) if pending_times else poll_interval
                self._stop.wait(max(0.1, sleep_time))
        finally:
            if recorder is not None:
                recording_path = recorder.stop()

        if self._stop.is_set():
            self._emit("aborted", reason="force stopped")
            return "Match ended early (force stopped)."

        # ------------------------------------------------------------------
        # Capture and return results
        # ------------------------------------------------------------------
        from game import tts
        tts.speak("Match over.", broadcast=False)
        self._update("Reading results screen", "Post to Discord")
        return self._capture_results(), recording_path

    # ------------------------------------------------------------------
    # Player bar
    # ------------------------------------------------------------------

    def _init_player_bar(self):
        """
        Snapshot the player bar from the lobby screen before the match starts.
        Detects slot count, x-positions, and OCRs player names in slot order.
        Called once; results are reused for first-blood tracking during the match.
        """
        from game.screen_detection import take_screenshot, detect_player_slot_xs
        from game.ocr import ocr_player_names

        screenshot = take_screenshot()
        mode = self._detector_mode()
        if mode != "v1":
            self._init_player_bar_v2(screenshot, drive=(mode == "v2"))
            if mode == "v2":
                return
        self._player_slot_xs = detect_player_slot_xs(screenshot, self._config)
        if not self._player_slot_xs:
            logger.warning(
                "Player bar snapshot: no slots detected — player tracking disabled. "
                "Set player_count in config if auto-detect fails."
            )
            return

        self._player_names = self._snap_names(
            ocr_player_names(screenshot, self._player_slot_xs, self._config)
        )
        self._player_alive = [True] * len(self._player_slot_xs)

        logger.info("Player bar snapshot — %d players:", len(self._player_slot_xs))
        for i, (name, x) in enumerate(zip(self._player_names, self._player_slot_xs)):
            logger.info("  slot %d  x=%-4d  %s", i + 1, x, name or "(unread)")

    # ------------------------------------------------------------------
    # V2 card detector (config player_bar_detector: "v1" | "v2" | "shadow")
    # ------------------------------------------------------------------

    def _snap_names(self, names: list[str]) -> list[str]:
        """Nameplate OCR → the ladder's canonical names for this lobby, where
        one player matches unambiguously (DraftLifecycle.snap_names, fed by the
        open-draft reply). Verbatim when no lobby is known."""
        if self._ds is None or not names:
            return list(names)
        try:
            snapped = self._ds.snap_names(list(names))
        except Exception as e:
            logger.debug("name snap unavailable: %s", e)
            return list(names)
        for before, after in zip(names, snapped):
            if before != after:
                logger.info("  nameplate %r snapped to ladder name %r", before, after)
        return snapped

    def _detector_mode(self) -> str:
        mode = str(self._config.get("player_bar_detector", "v1")).strip().lower()
        return mode if mode in ("v1", "v2", "shadow") else "v1"

    def _init_player_bar_v2(self, screenshot, drive: bool) -> None:
        """Run the geometry detector on the lobby snapshot. drive=True makes it
        the source of slots/names/alive for this match; drive=False (shadow)
        records what it saw for an A/B against V1 without touching the match."""
        from game import player_cards_v2 as v2
        expected = None
        if self._ds is not None:
            try:
                expected = self._ds.roster_size or None
            except Exception:
                expected = None
        try:
            cards = v2.detect_cards(screenshot, self._config, expected=expected)
        except Exception as e:
            logger.warning("Player bar v2: detection failed: %s", e)
            cards = []
        names: list[str] = []
        if cards:
            try:
                names = v2.ocr_names(screenshot, cards, self._config)
            except Exception as e:
                logger.debug("Player bar v2: name OCR failed: %s", e)
                names = []
        if len(names) != len(cards):
            names = ["" for _ in cards]
        self._v2_xs = [c.x for c in cards]
        self._v2_alive = [c.alive for c in cards]
        names = self._snap_names(names)
        self._v2_names = names
        n_alive = sum(1 for c in cards if c.alive)
        logger.info("Player bar v2 (%s): %d cards, %d alive", "driving" if drive else "shadow", len(cards), n_alive)
        for c in cards:
            logger.info("  v2 slot %d  x=%-4d  %s  %s", c.index + 1, c.x, "alive" if c.alive else "DEAD", names[c.index] or "(unread)")
        self._emit("detector_v2", elapsed_ms=0, drive=drive, n=len(cards), alive=n_alive,
                   xs=self._v2_xs, names=[nm or None for nm in names])
        if not drive:
            return
        if not cards:
            logger.warning("Player bar v2: no cards detected — player tracking disabled for this match")
            return
        self._player_slot_xs = list(self._v2_xs)
        self._player_names = list(names)
        self._player_alive = list(self._v2_alive)
        logger.info("Player bar snapshot (v2) — %d players:", len(self._player_slot_xs))
        for i, (name, x) in enumerate(zip(self._player_names, self._player_slot_xs)):
            logger.info("  slot %d  x=%-4d  %s", i + 1, x, name or "(unread)")

    def _poll_player_bar_v2_shadow(self, screenshot) -> None:
        """Shadow mode: re-read alive/dead at V2's card columns and emit
        eliminated_v2 for every flip, so the ladder's event stream shows what
        V2 would have reported next to what V1 did."""
        if not self._v2_xs:
            return
        from game import player_cards_v2 as v2
        try:
            new = v2.cards_alive(screenshot, self._v2_xs, self._config)
        except Exception as e:
            logger.debug("Player bar v2 shadow poll failed: %s", e)
            return
        alive_after = sum(1 for a in new if a)
        for i, (was, now) in enumerate(zip(self._v2_alive, new)):
            if was and not now:
                name = self._v2_names[i] if i < len(self._v2_names) and self._v2_names[i] else f"slot {i + 1}"
                self._emit("eliminated_v2", slot=i, player=name, alive=alive_after)
        self._v2_alive = new

    def _poll_player_bar(self, screenshot):
        """
        Check alive/eliminated status for each player slot.
        On the first death (first blood), logs the victim and attempts to OCR
        the kill notification text to identify the killer.
        """
        mode = self._detector_mode()
        if mode == "shadow":
            self._poll_player_bar_v2_shadow(screenshot)
        if not self._player_slot_xs:
            return

        if mode == "v2":
            from game import player_cards_v2 as v2
            new_alive = v2.cards_alive(screenshot, self._player_slot_xs, self._config)
        else:
            from game.screen_detection import sample_player_alive
            new_alive = [
                sample_player_alive(screenshot, x, self._config)
                for x in self._player_slot_xs
            ]

        prev_dead = sum(1 for a in self._player_alive if not a)
        curr_dead = sum(1 for a in new_alive if not a)
        newly_dead = [
            i for i, (was, now) in enumerate(zip(self._player_alive, new_alive))
            if was and not now
        ]

        if not self._first_blood_logged and prev_dead == 0 and curr_dead >= 1:
            victim_name = (
                self._player_names[newly_dead[0]]
                if self._player_names and newly_dead
                else f"slot {newly_dead[0] if newly_dead else '?'}"
            )
            from game.screen_detection import take_screenshot as _take_ss
            from game.ocr import ocr_kill_notification
            notif = ocr_kill_notification(_take_ss(), self._config)
            logger.info(
                "FIRST BLOOD — victim: %s | kill notification: %r",
                victim_name, notif,
            )
            self._first_blood_logged = True
            if newly_dead:
                self._emit("first_blood", slot=newly_dead[0], player=victim_name, notification=notif)

        # Every alive→dead flip is an elimination event; `alive` counts down as
        # this poll's flips are applied in slot order.
        alive_after = sum(1 for a in self._player_alive if a)
        for i in newly_dead:
            alive_after -= 1
            self._emit("eliminated", slot=i, player=self._slot_name(i), alive=alive_after)

        self._player_alive = new_alive

    def _slot_name(self, i: int) -> str:
        if self._player_names and i < len(self._player_names) and self._player_names[i]:
            return self._player_names[i]
        return f"slot {i + 1}"

    def _elapsed_ms(self) -> int:
        if self._match_started_at is None:
            return 0
        return int((time.monotonic() - self._match_started_at) * 1000)

    def _emit(self, kind: str, **fields) -> None:
        """Queue a live event for the ladder (no-op without a lifecycle; never raises)."""
        if self._ds is None:
            return
        try:
            fields.setdefault("elapsed_ms", self._elapsed_ms())
            self._ds.event(kind, **fields)
        except Exception as e:
            logger.debug("live event %s dropped: %s", kind, e)

    # ------------------------------------------------------------------
    # Card schedule
    # ------------------------------------------------------------------

    def _build_card_schedule(self, profile: dict) -> list[CardEvent]:
        card_plays = sorted(profile.get("card_plays", []), key=lambda p: p["play_time_seconds"])

        lead = self._config.get("card_play_lead_time_seconds", 0)

        assigned: set[int] = set()
        events = []
        for play in card_plays:
            card_type = play["card"]
            t = max(0, play["play_time_seconds"] - lead)
            m, s = divmod(play["play_time_seconds"], 60)  # display uses original time

            all_positions = self._positions_for_card_type(card_type)
            deck_pos = next((p for p in all_positions if p not in assigned), None)
            if deck_pos is None:
                logger.warning("No unassigned deck slot for '%s' at %d:%02d — skipping", card_type, m, s)
            else:
                assigned.add(deck_pos)

            drop_target = _CARD_DROP_TARGETS.get(card_type)

            from game.deck_utils import CARD_POINT_COSTS
            events.append(CardEvent(
                name=f"{card_type.replace('_', ' ').title()} at {m}:{s:02d}",
                card_type=card_type,
                trigger_seconds=t,
                play_time_seconds=play["play_time_seconds"],
                deck_position=deck_pos,
                drop_target=drop_target,
                points_cost=CARD_POINT_COSTS.get(card_type),
            ))
        return events

    def _read_points(self, screenshot) -> int | None:
        """
        Read current director points conservatively.
        Pips are always decremented by 1 to guard against a partially-filled pip being
        counted as full. OCR is used for cross-validation: if it agrees with pip-1, we
        log as confirmed; if it's exactly one higher, the last pip is trusted as genuinely
        full. On any other disagreement we take the lower of the two, not pip-1 unconditionally
        — pip reads can be biased either direction (a partially-filled pip undercounts, but a
        single bright background pixel at a pip's sample coordinate overcounts), so only the
        lower reading is guaranteed safe against a false "enough points" pass, whichever source
        it came from.
        Falls back to whichever source is available.

        director_points_use_pips (config, default True) gates the pip path entirely — set to
        False to read OCR alone. Added 2026-09-04: single-pixel brightness sampling at some pip
        coordinates was catching bright background elements and over-reading, causing cards to
        be attempted before enough points actually existed. The pip-reading code and the
        director_points_pips calibration itself are both left in place (same "disconnected, not
        deleted" precedent as zone_close's legacy per-zone logic) in case pip reading is
        revisited later (e.g. multi-pixel voting instead of single-pixel sampling).
        """
        from game.ocr import count_director_point_pips, read_director_points
        pip_count = None
        ocr_count = None

        pips_cfg = _DIRECTOR_POINTS_PIPS if self._config.get("director_points_use_pips", True) else None
        if pips_cfg:
            raw = count_director_point_pips(screenshot, pips_cfg)
            if raw is not None:
                pip_count = max(0, raw - 1)

        ocr_count = read_director_points(screenshot, _DIRECTOR_POINTS_REGION)

        if pip_count is not None:
            if ocr_count is not None:
                if ocr_count == pip_count:
                    logger.debug("Points confirmed: %d (pips and OCR agree)", pip_count)
                elif ocr_count == pip_count + 1:
                    logger.debug("Points: pip-1=%d ocr=%d — OCR confirms last pip full, trusting OCR", pip_count, ocr_count)
                    return ocr_count
                else:
                    safe = min(pip_count, ocr_count)
                    logger.debug(
                        "Points: pip-1=%d ocr=%d — discrepancy, using the lower reading (%d)",
                        pip_count, ocr_count, safe,
                    )
                    return safe
            return pip_count

        return ocr_count

    def _update_points_reading(self, current: Optional[int], context: str) -> Optional[int]:
        """Merge a fresh points read into self._last_confirmed_points with a ratchet-up
        guard: a failed read (None), or one that comes back lower than the last
        confirmed value, is treated as noise (a transient OCR misread) and disregarded
        rather than trusted — the confirmed value only ever moves up from an actual
        higher reading. It never moves down on its own, because points only decrease
        when this bot plays a card, and that path (_fire_card_event) explicitly
        invalidates the confirmed value first so the next read — whatever it is — is
        trusted as the new baseline instead of being compared against a now-stale one.

        Returns the resulting confirmed value (possibly unchanged).
        """
        if current is None:
            logger.debug("Points read failed (%s) — keeping last known value (%s)", context, self._last_confirmed_points)
        elif self._last_confirmed_points is None or current >= self._last_confirmed_points:
            self._last_confirmed_points = current
        else:
            logger.debug(
                "Points read %d (%s) is lower than last known good %d — disregarding as a misread",
                current, context, self._last_confirmed_points,
            )
        return self._last_confirmed_points

    def _wait_for_points(self, needed: int, card_name: str,
                         broadcast_open: bool = False, card_label: str = "") -> bool:
        """Block until the director has enough points.

        Never fails open on a bad read — "ready" is only ever declared off
        self._last_confirmed_points (see _update_points_reading), which is fed both by
        this loop's own reads and by the main loop's continuous background sampling, so
        a card usually already has a recent confirmed value to check against instead of
        needing a fresh read at the exact moment it fires. Found live: a single failed
        read used to make this return immediately as if points were sufficient, and the
        card would then be attempted without actually having enough.

        If broadcast_open and we actually need to wait, announces 'Waiting on points for X'
        async and immediately closes the broadcast so the 90s cooldown starts ticking.
        Returns True if the broadcast was closed (caller should try to reopen when ready).
        """
        if needed == 0:
            return False
        from game.screen_detection import take_screenshot
        closed_broadcast = False
        while not self._stop.is_set():
            current = self._read_points(take_screenshot())
            confirmed = self._update_points_reading(current, card_name)

            if confirmed is not None and confirmed >= needed:
                logger.info("Points ready for '%s': %d/%d", card_name, confirmed, needed)
                return closed_broadcast

            logger.info("Waiting for points: have %s, need %d for '%s'", confirmed, needed, card_name)
            if not closed_broadcast and card_label:
                from game import tts as _tts
                _tts.speak_cable(f"Waiting on points for {card_label}")
                if broadcast_open:
                    _tts.queue_close_broadcast()
                closed_broadcast = True
            self._stop.wait(2.0)
        return closed_broadcast

    def _fire_card_event(self, event: CardEvent, all_events: list[CardEvent]):
        event.done = True
        logger.info("Firing card event: %s", event.name)
        # Player-targeted cards pick a screen coordinate, not a slot, so the
        # event names the card only; the target slot is unknown to the bot.
        self._emit("card_play", card=event.card_type, name=event.name)

        from game import tts
        next_event = next((e for e in all_events if not e.done), None)
        card_label = tts.card_announce(event.card_type)

        if event.card_type == "zone_close":
            from game.deck_utils import CARD_POINT_COSTS
            cost = CARD_POINT_COSTS.get("zone_close", 0)
            broadcast_open = tts.try_open_broadcast()
            if cost:
                if self._wait_for_points(cost, "zone_close",
                                         broadcast_open=broadcast_open, card_label=card_label):
                    broadcast_open = tts.try_open_broadcast()
            if self._stop.is_set():
                return
            zone_success = self._attempt_zone_close()
            if not self._stop.is_set():
                if zone_success:
                    ann = self._next_card_announce(next_event)
                    if ann:
                        tts.speak_cable(ann)
                if broadcast_open:
                    tts.queue_close_broadcast()
        elif event.deck_position is None:
            logger.warning("Card event '%s' has no deck position assigned — skipping", event.name)
        elif event.card_type in _ZONE_TARGETED_CARDS:
            import random
            zone_id = random.choice(list(_ZONE_DROP_COORDINATES.keys()))
            target = _ZONE_DROP_COORDINATES[zone_id]
            logger.info("Zone-targeted card '%s' → zone %s at %s", event.name, zone_id, target)
            broadcast_open = tts.try_open_broadcast()
            if event.points_cost is not None:
                if self._wait_for_points(event.points_cost, event.name,
                                         broadcast_open=broadcast_open, card_label=card_label):
                    broadcast_open = tts.try_open_broadcast()
            if not self._stop.is_set():
                tts.speak_cable(f"Deploying {card_label}")
                self._play_tray_card(event, target, card_label, next_event, broadcast_open)
        elif event.card_type in _PLAYER_TARGETED_CARDS:
            player_coords = self._config.get("player_target_coordinates") or []
            if not player_coords:
                logger.warning("Card event '%s': player_target_coordinates not calibrated — skipping", event.name)
            else:
                import random
                target = tuple(random.choice(player_coords))
                logger.info("Player-targeted card '%s' → player slot at %s", event.name, target)
                broadcast_open = tts.try_open_broadcast()
                if event.points_cost is not None:
                    if self._wait_for_points(event.points_cost, event.name,
                                             broadcast_open=broadcast_open, card_label=card_label):
                        broadcast_open = tts.try_open_broadcast()
                if not self._stop.is_set():
                    tts.speak_cable(f"Deploying {card_label}")
                    self._play_tray_card(event, target, card_label, next_event, broadcast_open)
        elif not event.drop_target:
            logger.warning("Card event '%s' has no drop_target configured — skipping", event.name)
        else:
            broadcast_open = tts.try_open_broadcast()
            if event.points_cost is not None:
                if self._wait_for_points(event.points_cost, event.name,
                                         broadcast_open=broadcast_open, card_label=card_label):
                    broadcast_open = tts.try_open_broadcast()
            if not self._stop.is_set():
                tts.speak_cable(f"Deploying {card_label}")
                self._play_tray_card(event, event.drop_target, card_label, next_event, broadcast_open)

        next_label = next_event.name if next_event else "Match end polling"
        self._update(f"Played {event.name}", next_label)

    def _play_tray_card(
        self,
        event: CardEvent,
        target: tuple[int, int],
        card_label: str,
        next_event: Optional[CardEvent],
        broadcast_open: bool,
    ) -> None:
        """
        Shared play/verify/announce logic for zone-targeted, player-targeted, and plain
        (fixed drop_target) cards — the only difference between those three cases is
        how `target` was computed by the caller.

        Three modes, checked in order:
        - self._bypass (ahk_bypass_mode): dry run, logs and waits for Enter, never
          touches the game.
        - not self._verify_plays (verify_card_plays: false): plays once and trusts it
          worked — no before/after screenshots, no retry loop.
        - default: the original before/after tray-pixel verify with up to 2 attempts.
        """
        from game import tts
        from game.card_actions import play_card

        slot_coord = self._deck_pos_to_screen(event.deck_position)
        played = False

        if self._bypass:
            play_card(slot_coordinate=slot_coord, target_coordinate=target,
                      card_name=event.name, bypass_mode=True)
            self._deck_played.add(event.deck_position)
            played = True
        elif not self._verify_plays:
            play_card(slot_coordinate=slot_coord, target_coordinate=target, card_name=event.name)
            self._deck_played.add(event.deck_position)
            played = True
        else:
            from game.card_actions import shift_down, shift_up
            from game.screen_detection import take_screenshot, save_error_screenshot
            # Hold shift once for the entire attempt block:
            # before-screenshot → play → after-screenshot → [retry plays] → release.
            shift_down()
            time.sleep(0.25)
            before = take_screenshot()
            for attempt in range(1, 3):
                if attempt > 1:
                    tts.speak_cable("Retrying")
                play_card(slot_coordinate=slot_coord, target_coordinate=target,
                          card_name=event.name, keep_shift=True)
                time.sleep(0.4)
                after = take_screenshot()
                if self._verify_card_removed(slot_coord, before, after):
                    self._deck_played.add(event.deck_position)
                    played = True
                    break
                logger.warning("Card '%s' not verified in tray (attempt %d/2)", event.name, attempt)
                if self._stop.is_set():
                    break
            if not played and not self._stop.is_set():
                logger.error("Card '%s' failed to play after 2 attempts", event.name)
                save_error_screenshot(f"card_play_failed_{event.name.replace(' ', '_').replace(':', '_')}")
            shift_up()

        if not self._stop.is_set():
            # A real points decrease is likely now (a play attempt was just made,
            # whether or not it verified) — invalidate the confirmed reading so the
            # next read, whatever it is, becomes the new trusted baseline instead of
            # being compared against a now-stale pre-attempt value.
            self._last_confirmed_points = None
            if played:
                ann = self._next_card_announce(next_event)
                if ann:
                    tts.speak_cable(ann)
            else:
                tts.speak_cable(f"Sorry, failed to deploy {card_label}")
            if broadcast_open:
                tts.queue_close_broadcast()

    def _build_tts_phrases(self, card_schedule: list[CardEvent]) -> list[str]:
        """Return every TTS phrase this match might speak, for pre-caching."""
        from game import tts as _tts
        phrases = [
            "Match is starting. Good luck.",
            "Retrying",
            "Match over.",
            "Deploying Zone Close",
            "Waiting on points for Zone Close",
            "Sorry, no zones available to close",
            "Sorry, failed to close a zone",
        ]
        for i in range(1, 8):
            phrases.append(f"Closing zone {i}")

        for i, event in enumerate(card_schedule):
            label = _tts.card_announce(event.card_type)
            if event.card_type != "zone_close":
                phrases.append(f"Deploying {label}")
                phrases.append(f"Waiting on points for {label}")
                phrases.append(f"Sorry, failed to deploy {label}")
            next_ev = card_schedule[i + 1] if i + 1 < len(card_schedule) else None
            if next_ev:
                next_label = _tts.card_announce(next_ev.card_type)
                m, s = divmod(next_ev.play_time_seconds, 60)
                time_str = str(m) if s == 0 else f"{m} {s}"
                phrases.append(f"Next card is {next_label} at {time_str}")

        return phrases

    def _build_lineup_text(self, card_schedule: list[CardEvent]) -> str:
        """Build the full card lineup as a single TTS-friendly string."""
        from game import tts as _tts
        parts = []
        for event in sorted(card_schedule, key=lambda e: e.play_time_seconds):
            label = _tts.card_announce(event.card_type)
            m, s = divmod(event.play_time_seconds, 60)
            time_str = str(m) if s == 0 else f"{m} {s}"
            parts.append(f"{label} at {time_str}")
        return ", ".join(parts)

    def _announce_card_lineup(self, text: str):
        """Announce the card lineup at match start. Plays over proximity audio regardless;
        also broadcasts globally if the cooldown allows."""
        from game import tts
        if not text or not tts.is_enabled():
            return
        broadcast_open = tts.try_open_broadcast()
        tts.speak_cable(text)
        if broadcast_open:
            tts.queue_close_broadcast()

    def _next_card_announce(self, next_event: Optional[CardEvent]) -> Optional[str]:
        """Format a 'Next card is X at Y' announcement for TTS."""
        if next_event is None:
            return None
        from game import tts
        m, s = divmod(next_event.play_time_seconds, 60)
        name = tts.card_announce(next_event.card_type)
        time_str = str(m) if s == 0 else f"{m} {s}"
        return f"Next card is {name} at {time_str}"

    # ------------------------------------------------------------------
    # Zone closes
    # ------------------------------------------------------------------

    def _attempt_zone_close(self) -> bool:
        """
        Drag the zone_close card to a single static drop area (zone_close_auto_drop_target
        in config.json) that the game itself resolves to a random valid zone — the bot no
        longer reads the zone map or picks a zone itself. Always "succeeds" once the drag
        completes: there is no tray-pixel verification here regardless of verify_card_plays,
        since the drop area handles zone validity on the game's side. Returns False only if
        no zone_close card remains in the deck or the drop target isn't calibrated yet.

        The old per-zone selection path (zone-map reading, ZoneState tracking, the
        pluggable zone_selection_strategy system in zones/) is left in place, disconnected,
        as _attempt_zone_close_legacy() below — not called from here, kept for reference/
        rollback in case the new drop area needs to be abandoned.
        """
        from game import tts
        from game.card_actions import play_card

        deck_pos = self._next_available_deck_pos(self._positions_for_card_type("zone_close"))
        if deck_pos is None:
            logger.info("Zone close: no ZoneClose cards remaining")
            return False

        target = _ZONE_CLOSE_DROP_TARGET

        tts.speak_cable("Deploying Zone Close")
        slot_coord = self._deck_pos_to_screen(deck_pos)

        play_card(
            slot_coordinate=slot_coord,
            target_coordinate=tuple(target),
            card_name="zone_close",
            bypass_mode=self._bypass,
        )
        self._deck_played.add(deck_pos)
        # See the matching comment in _play_tray_card() — a real points decrease is
        # likely now, so invalidate the confirmed reading rather than let a later wait
        # trust a now-stale pre-play value.
        self._last_confirmed_points = None
        # No "Closing a zone" TTS confirmation here (2026-09-06) — this drag is never
        # verified regardless of verify_card_plays (the drop area handles zone validity
        # on the game's side, see the docstring above), so that line always spoke as if
        # confirmed even though nothing was actually checked. "Deploying Zone Close"
        # above still announces the attempt; nothing here claims it worked.
        self._update("Closed a zone", "Continue match")
        return True

    def _attempt_zone_close_legacy(self) -> bool:
        """
        Every 30s: grab the zone_close card to reveal the big zone map, read zone states
        from multiple sample points per tile, pick the best zone, then play or cancel.
        In bypass mode, uses cached zone states (no grab possible without a real game).
        Returns True if a zone was successfully closed, False otherwise.

        Superseded by _attempt_zone_close() above (2026-08-30) — kept, not deleted, in
        case the new static drop-area approach needs to be rolled back. Not called from
        anywhere; _fire_card_event() calls _attempt_zone_close() instead.
        """
        from game import tts

        deck_pos = self._next_available_deck_pos(self._positions_for_card_type("zone_close"))
        if deck_pos is None:
            logger.info("Zone close: no ZoneClose cards remaining")
            return False

        tts.speak_cable("Deploying Zone Close")

        slot_coord = self._deck_pos_to_screen(deck_pos)

        if self._bypass:
            return self._attempt_zone_close_bypass(slot_coord, deck_pos)

        import random
        import pyautogui as _pag
        from game.card_actions import grab_card, complete_drag, shift_down, shift_up
        from game.screen_detection import take_screenshot, save_error_screenshot

        # Hold shift and take the before-reference (tray visible, card in slot),
        # then immediately grab the card — zone map appears on mouseDown.
        # Shift stays held from this point through all drags and retries.
        shift_down()
        time.sleep(0.25)
        before_shift = take_screenshot()
        grab_card(slot_coord, shift_already_held=True)
        time.sleep(0.35)
        self._update_zone_states_from_screenshot(take_screenshot())

        valid = valid_closeable_zones(self._zone_states)
        if not valid:
            logger.info("Zone close: no valid zones — releasing card")
            _pag.mouseUp()
            shift_up()
            tts.speak_cable("Sorry, no zones available to close")
            return False

        zones_to_try = list(valid_closeable_zones(self._zone_states))
        random.shuffle(zones_to_try)
        zone_drop_coords = _ZONE_DROP_COORDINATES

        if not self._verify_plays:
            # verify_card_plays: false — single attempt, no pixel-delta check, no
            # retry across zones. Picks one valid zone (still via the per-zone
            # zone_drop_coordinates) and trusts the drag worked.
            for zone_id in zones_to_try:
                raw_target = zone_drop_coords.get(zone_id)
                if not raw_target:
                    continue
                complete_drag(target_coordinate=tuple(raw_target), card_name=f"close_zone_{zone_id}")
                self._zone_states[zone_id] = ZoneState.CLOSING
                self._deck_played.add(deck_pos)
                tts.speak_cable(f"Closing zone {zone_id}")
                self._update(f"Closed zone {zone_id}", "Continue match")
                return True
            logger.warning("Zone close: no candidate zone had a calibrated drop coordinate")
            _pag.mouseUp()
            shift_up()
            tts.speak_cable("Sorry, failed to close a zone")
            return False

        center_x = _CARD_TRAY_CENTER_X
        card_width = _CARD_TRAY_CARD_WIDTH
        card_y = _CARD_TRAY_CARD_Y
        tray_configured = True  # tray layout is now a hardcoded constant, always available

        for i, zone_id in enumerate(zones_to_try):
            if self._stop.is_set():
                _pag.mouseUp()
                break

            raw_target = zone_drop_coords.get(zone_id)
            if not raw_target:
                logger.warning("Zone %d skipped — drop coordinate not calibrated", zone_id)
                continue  # card still grabbed; drag to next valid zone

            # Drag to the zone; keep_shift=True so shift stays held for verification
            complete_drag(target_coordinate=tuple(raw_target), card_name=f"close_zone_{zone_id}",
                          keep_shift=True)
            time.sleep(0.8)  # zone map animation clears
            after_shift = take_screenshot()  # shift still held — tray visible

            verified = True
            if tray_configured:
                slot_x, slot_y = slot_coord
                bgr_before = before_shift[slot_y, slot_x]
                bgr_after = after_shift[slot_y, slot_x]
                delta = int(sum(abs(int(a) - int(b)) for a, b in zip(bgr_before, bgr_after)))
                logger.info(
                    "Zone close verify: slot=(%d,%d) before=%s after=%s delta=%d — %s",
                    slot_x, slot_y,
                    tuple(int(v) for v in bgr_before),
                    tuple(int(v) for v in bgr_after),
                    delta,
                    "verified" if delta > 80 else "unchanged",
                )
                verified = delta > 80

            if verified:
                self._zone_states[zone_id] = ZoneState.CLOSING
                self._deck_played.add(deck_pos)
                tts.speak_cable(f"Closing zone {zone_id}")
                self._update(f"Closed zone {zone_id}", "Continue match")
                shift_up()
                return True

            logger.warning("Zone %d close not verified — trying next zone", zone_id)

            if self._stop.is_set() or i == len(zones_to_try) - 1:
                break

            # Shift still held — re-grab for the next retry without releasing
            time.sleep(0.2)
            grab_card(slot_coord, shift_already_held=True)
            time.sleep(0.2)

        logger.error("Zone close failed for all %d candidate zones", len(zones_to_try))
        _pag.mouseUp()  # ensure card is released if loop exited via skip/stop
        save_error_screenshot("zone_close_failed")  # shift still held = tray visible
        shift_up()
        tts.speak_cable("Sorry, failed to close a zone")
        return False

    def _attempt_zone_close_bypass(self, slot_coord: tuple, deck_pos: int) -> bool:
        """Bypass-mode zone close: use cached zone states, log the action, pause."""
        from game.card_actions import play_card
        from game import tts

        valid = valid_closeable_zones(self._zone_states)
        if not valid:
            logger.info("[BYPASS] Zone close: no valid zones")
            return False

        zone_id = self._strategy.select_zone(valid, self._zone_states)
        if zone_id is None:
            return False

        raw_target = _ZONE_DROP_COORDINATES.get(zone_id)
        if not raw_target:
            logger.warning("[BYPASS] Zone %d close skipped — drop coordinate not calibrated", zone_id)
            return False

        play_card(
            slot_coordinate=slot_coord,
            target_coordinate=tuple(raw_target),
            card_name=f"close_zone_{zone_id}",
            bypass_mode=True,
        )
        self._zone_states[zone_id] = ZoneState.CLOSING
        self._deck_played.add(deck_pos)
        tts.speak_cable(f"Closing zone {zone_id}")
        self._update(f"Closed zone {zone_id}", "Continue match")
        return True

    def _update_zone_states_from_screenshot(self, screenshot):
        """Vote across multiple sample points per zone tile to determine each zone's state."""
        thresholds = self._config.get("zone_color_thresholds", {})
        map_points = _ZONE_MAP_SAMPLE_POINTS

        if not all(thresholds.get(k) for k in ("open", "closed", "closing")):
            logger.debug("Zone map detection skipped — zone_color_thresholds not calibrated")
            return

        for zone_id, points in map_points.items():
            if not points:
                continue
            if self._zone_states.get(zone_id) == ZoneState.CLOSED:
                continue
            state = self._vote_zone_state(screenshot, points, thresholds)
            if state != self._zone_states.get(zone_id):
                logger.info("Zone %d: %s → %s", zone_id, self._zone_states.get(zone_id), state)
            self._zone_states[zone_id] = state

    # Threshold for _verify_card_removed's single-pixel delta check. Lowered from 40
    # (2026-08-28): log analysis across ~130k lines / months of matches showed a
    # recurring false-negative band at delta 17-39 — always on the first tray-card
    # play of the match or the first play right after a new card naturally unlocks
    # (Electromania at 2:30, Beach Party at 4:00, Telepathy at 4:30/10:00), where the
    # tray recenters and the slot pixel shifts to a different-but-similar card color
    # instead of going stark. A missed verification here never gets the card's
    # deck_position added to self._deck_played, so every later _deck_pos_to_screen()
    # call computes one card-width off — the "tray out of sync" failure cascade
    # (next card's play AND its verification both land on the wrong slot, usually
    # reading near-zero delta since nothing meaningful is at that wrong pixel).
    # Genuine non-plays across the same log are all delta <= 15 (mostly 0-3, one
    # ambiguous 10); genuine clean detections are all delta >= 41. 16 sits in the
    # untouched gap between the two, so this only reclassifies the confirmed
    # false-negative band and leaves every previously-correct call unchanged.
    _TRAY_VERIFY_DELTA_THRESHOLD = 16

    def _verify_card_removed(self, slot_coord: tuple, before_screenshot, after_screenshot) -> bool:
        """
        Compare the pixel at slot_coord between two shift-held screenshots.
        Before: the specific card should be visible at that position.
        After play: the slot is empty or re-centered to a different card — clear delta.
        After failed play: same card returns — delta near zero.
        """
        x_check, y_check = slot_coord
        bgr_before = before_screenshot[y_check, x_check]
        bgr_after = after_screenshot[y_check, x_check]
        delta = int(sum(abs(int(a) - int(b)) for a, b in zip(bgr_before, bgr_after)))

        threshold = self._TRAY_VERIFY_DELTA_THRESHOLD
        logger.info(
            "Tray verify: slot=(%d,%d) before=%s after=%s delta=%d — %s",
            x_check, y_check,
            tuple(int(v) for v in bgr_before),
            tuple(int(v) for v in bgr_after),
            delta,
            "verified" if delta > threshold else "unchanged",
        )
        return delta > threshold

    def _positions_for_card_type(self, card_type: str) -> list[int]:
        """Return all visual deck positions that contain cards of the given type."""
        return [i for i, c in enumerate(self._deck_layout) if c == card_type]

    def _deck_pos_to_screen(self, deck_pos: int) -> tuple[int, int]:
        """Convert a visual deck position to current screen (x, y) in 1920×1080."""
        remaining = [i for i in range(len(self._deck_layout)) if i not in self._deck_played]
        visual_index = remaining.index(deck_pos)
        n = len(remaining)
        center_x = _CARD_TRAY_CENTER_X
        card_width = _CARD_TRAY_CARD_WIDTH
        card_y = _CARD_TRAY_CARD_Y
        first_x = center_x - (n - 1) / 2 * card_width
        x = round(first_x + visual_index * card_width)
        return (x, card_y)

    def _next_available_deck_pos(self, positions: list[int]) -> Optional[int]:
        """Return the first unplayed deck position from the given list, or None."""
        for pos in positions:
            if pos not in self._deck_played:
                return pos
        return None

    def _vote_zone_state(self, screenshot, points: list, thresholds: dict) -> str:
        """
        Sample each point in the list and return the majority zone state.
        Minority points covered by player icons won't flip the result.
        """
        from game.screen_detection import color_within_threshold

        closed_thresh = tuple(thresholds["closed"])
        closing_thresh = tuple(thresholds["closing"])
        counts = {ZoneState.CLOSED: 0, ZoneState.CLOSING: 0, ZoneState.OPEN: 0}

        for x, y in points:
            bgr = screenshot[y, x]
            color = (int(bgr[2]), int(bgr[1]), int(bgr[0]))
            if color_within_threshold(color, closed_thresh):
                counts[ZoneState.CLOSED] += 1
            elif color_within_threshold(color, closing_thresh):
                counts[ZoneState.CLOSING] += 1
            else:
                counts[ZoneState.OPEN] += 1

        return max(counts, key=counts.get)

    # ------------------------------------------------------------------
    # Match end detection
    # ------------------------------------------------------------------

    def _match_has_ended(self) -> bool:
        from game.screen_detection import poll_for_match_end
        if not poll_for_match_end(badge_template_path="templates/placement_badge.png", threshold=0.88):
            return False
        # Confirm with a second check 2 seconds later — rules out transient HUD false positives
        self._stop.wait(2.0)
        return poll_for_match_end(badge_template_path="templates/placement_badge.png", threshold=0.88)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _capture_results(self) -> str:
        import cv2
        import datetime
        from pathlib import Path
        from game.screen_detection import take_screenshot

        screenshot = take_screenshot()
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_dir = Path("screenshots/results")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = str(out_dir / f"results_{ts}.png")
        cv2.imwrite(path, screenshot)
        logger.info("Results screenshot saved: %s", path)
        return path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _press(self, key: str):
        from game.card_actions import press_key
        press_key(key, bypass_mode=self._bypass)

    def _update(self, last: str, next_: str):
        self._on_action_update(last, next_)
        logger.info("Match action — last: %s | next: %s", last, next_)
        self._emit("status", last=last, next=next_)

    def _save_error_screenshot(self, label: str):
        from game.screen_detection import save_error_screenshot
        save_error_screenshot(label)
